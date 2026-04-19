"""Guardrail layer. Every Order passes through `check()` before the
broker sees it.

Rules (from PROJECT_PLAN.md):

  1. Single-position cap       : 20% stocks, 35% ETFs, 10% crypto.
                                 Measured as (new position market value) /
                                 (total equity after trade).
  2. Portfolio floor           : €500. If breached the bot is flagged and
                                 ALL proposed orders are rejected.
  3. Daily trade limit         : max 5 trades per bot per day.
  4. Fee-aware skip            : skip if round-trip fees > 25% of
                                 expected profit. Only applied when
                                 order.expected_profit_eur > 0.

SELL orders are always allowed to pass (subject to rules 2 and 3) — we
never want the risk layer blocking an exit.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

from sqlalchemy.orm import Session

from core.broker import estimate_fee_eur, venue_for
from core.config import CONFIG
from core.portfolio import Portfolio
from core.types import AssetClass, Order, PortfolioSnapshot, Side


Decision = Literal["approved", "rejected"]


@dataclass(frozen=True)
class RiskResult:
    decision: Decision
    reason: str = ""

    @property
    def approved(self) -> bool:
        return self.decision == "approved"


def _cap_for(asset_class: AssetClass) -> float:
    g = CONFIG.settings["guardrails"]
    if asset_class is AssetClass.ETF:
        return float(g["position_cap_etf"])
    if asset_class is AssetClass.CRYPTO:
        return float(g["position_cap_crypto"])
    return float(g["position_cap_stock"])


def _asset_class_from_watchlist(ticker: str) -> AssetClass:
    venue_map: dict = CONFIG.watchlists.get("venue", {})
    entry = venue_map.get(ticker, {})
    klass = entry.get("class", "stock")
    return AssetClass(klass)


def resolve_asset_class(order: Order) -> AssetClass:
    """Prefer what the strategy set; fall back to watchlists.yaml."""
    if order.asset_class is not None:
        return order.asset_class
    return _asset_class_from_watchlist(order.ticker)


def check(
    session: Session,
    order: Order,
    snapshot: PortfolioSnapshot,
    today: date,
) -> RiskResult:
    """Top-level guardrail check."""

    g = CONFIG.settings["guardrails"]

    # Rule 2: portfolio floor. Applies to both sides — if the book is below
    # the floor the bot has already failed and we freeze it. Exits also
    # frozen: the plan explicitly says "the bot stops trading".
    floor = float(g["portfolio_floor_eur"])
    if snapshot.total_eur < floor:
        return RiskResult(
            "rejected",
            f"portfolio floor breached: total €{snapshot.total_eur:.2f} < €{floor:.2f}",
        )

    # Rule 3: daily trade limit.
    limit = int(g["max_trades_per_day"])
    placed = Portfolio.trades_today(session, order.bot_id, today)
    if placed >= limit:
        return RiskResult(
            "rejected",
            f"daily trade limit reached: {placed}/{limit}",
        )

    # SELLs bypass the rest (can't make an exit worse by capping it).
    if order.side is Side.SELL:
        # Sanity: can we actually sell what we think we hold?
        pos = snapshot.positions.get(order.ticker)
        if pos is None or pos.qty + 1e-9 < order.qty:
            return RiskResult(
                "rejected",
                f"SELL {order.ticker} qty {order.qty} exceeds held "
                f"{0 if pos is None else pos.qty}",
            )
        return RiskResult("approved")

    # --- BUY-only checks below ---

    asset_class = resolve_asset_class(order)

    # Rule 1: position cap.
    cap = _cap_for(asset_class)
    new_notional = order.qty * order.ref_price_eur
    existing = snapshot.positions.get(order.ticker)
    current_pos_value = existing.market_value_eur if existing else 0.0

    fee = estimate_fee_eur(order.ticker, order.qty, order.ref_price_eur)

    # Cap is measured vs. pre-trade total equity so strategies can size
    # naturally as `equity * cap`. Fee is accounted for separately via the
    # cash check below.
    if snapshot.total_eur <= 0:
        return RiskResult("rejected", f"non-positive equity €{snapshot.total_eur:.2f}")

    position_value_after = current_pos_value + new_notional
    pct = position_value_after / snapshot.total_eur
    if pct > cap + 1e-9:
        return RiskResult(
            "rejected",
            f"position cap breached: {asset_class.value} {order.ticker} "
            f"{pct*100:.2f}% > {cap*100:.2f}% cap",
        )

    # Rule 4: fee-aware skip. Only meaningful when the strategy provides an
    # expected profit target. Round-trip = 2 * one-way fee.
    if order.expected_profit_eur > 0:
        ratio_max = float(g["fee_profit_ratio_max"])
        round_trip_fee = 2.0 * fee
        ratio = round_trip_fee / order.expected_profit_eur
        if ratio > ratio_max:
            return RiskResult(
                "rejected",
                f"fee/profit {ratio*100:.1f}% > cap {ratio_max*100:.1f}% "
                f"(round-trip fee €{round_trip_fee:.2f}, target profit "
                f"€{order.expected_profit_eur:.2f})",
            )

    # Cash check: do we actually have enough?
    cost = new_notional + fee
    if cost > snapshot.cash_eur + 1e-9:
        return RiskResult(
            "rejected",
            f"insufficient cash: need €{cost:.2f}, have €{snapshot.cash_eur:.2f}",
        )

    return RiskResult("approved")


__all__ = ["check", "RiskResult", "resolve_asset_class", "venue_for"]
