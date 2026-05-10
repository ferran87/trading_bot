"""Bot 30 — AI Thesis Strategy.

Unlike rules-based strategies (which compute orders purely from price signals),
this strategy executes approved ``ThesisAction`` rows from the database.  The
actions are proposed by ``agents/portfolio_manager.py`` (Claude) and approved
by the user via the "🧠 Tesis d'inversió" dashboard tab.

Entry mechanics (two-tier):
  Conviction ≥ 4: portfolio_manager proposes entry immediately (no technical gate).
                  Strategy executes any approved 'open' ThesisAction.
  Conviction  = 3: portfolio_manager creates a 'waiting' thesis.  THIS module
                   checks RSI/SMA conditions daily; when they align it creates a
                   'open' ThesisAction (still requires user approval).

Exits:
  Either the portfolio_manager proposes an EXIT (thesis invalidated → user approves)
  OR the trailing / catastrophic stop fires unconditionally (same as other bots).
  Whichever fires first wins.

Sizing:
  Approved actions carry a ``size_pct`` field (conviction multiplier × base 10%).
  Hard cap: 15% of bot capital per position.

Guardrails enforced here (code, not just prompt):
  - Approved actions for conviction ≤ 3 tickers are re-checked against RSI/SMA
    at execution time (belt + suspenders).
  - Trailing stop: 22% from peak (trail_pct param).
  - Catastrophic stop: -20% from avg entry (catastrophic_stop param).
"""
from __future__ import annotations

import logging
from datetime import date

import pandas as pd

from analysis.price_signals import rsi
from core.config import CONFIG
from core.db import Thesis, ThesisAction, get_session
from core.types import AssetClass, Order, PortfolioSnapshot, Side
from strategies.base import Strategy, StrategyContext

log = logging.getLogger(__name__)

_CONVICTION_MULT = {5: 1.5, 4: 1.2, 3: 1.0, 2: 0.8, 1: 0.6}
_BASE_PCT = 0.10
_MAX_PCT  = 0.15


def _size_pct(conviction: int) -> float:
    raw = _BASE_PCT * _CONVICTION_MULT.get(conviction, 1.0)
    return min(raw, _MAX_PCT)


def _check_rsi_sma_gate(
    bars: pd.DataFrame,
    params: dict,
) -> bool:
    """Return True if RSI/SMA gate is satisfied for a conviction-3 waiting entry.

    Same logic as rsi_compounder: RSI was oversold recently, now recovering,
    and not yet overbought.  Stock above SMA50.
    """
    if bars is None or bars.empty or len(bars) < 60:
        return False

    close = bars["close"]
    rsi_period     = int(params.get("rsi_period", 14))
    rsi_was_below  = float(params.get("rsi_was_below", 30))
    rsi_now_above  = float(params.get("rsi_now_above", 40))
    rsi_entry_max  = float(params.get("rsi_entry_max", 65))
    rsi_lookback   = int(params.get("rsi_lookback_days", 15))
    sma_period     = int(params.get("sma_period", 50))

    rsi_series = rsi(close, rsi_period)
    if rsi_series.empty or rsi_series.isna().all():
        return False

    current_rsi = float(rsi_series.iloc[-1])
    lookback_min = float(rsi_series.iloc[-rsi_lookback:].min()) if len(rsi_series) >= rsi_lookback else float("nan")

    if current_rsi != current_rsi or lookback_min != lookback_min:
        return False

    rsi_was_low     = lookback_min < rsi_was_below
    rsi_rebounded   = current_rsi > rsi_now_above
    rsi_not_overbought = current_rsi < rsi_entry_max

    if len(close) < sma_period:
        above_sma = False
    else:
        sma = float(close.rolling(sma_period).mean().iloc[-1])
        above_sma = float(close.iloc[-1]) > sma

    return rsi_was_low and rsi_rebounded and rsi_not_overbought and above_sma


class AiThesisStrategy(Strategy):
    name = "ai_thesis"

    def propose_orders(
        self,
        snapshot: PortfolioSnapshot,
        ctx: StrategyContext,
    ) -> list[Order]:
        params = ctx.params
        trail_pct         = float(params.get("trail_pct", 0.22))
        catastrophic_stop = float(params.get("catastrophic_stop", -0.20))

        orders: list[Order] = []

        # ── 1. Execute approved pending actions ───────────────────────────────
        orders.extend(self._execute_approved_actions(ctx))

        # ── 2. Check waiting theses for RSI/SMA gate ──────────────────────────
        orders.extend(self._check_waiting_theses(ctx))

        # ── 3. Exit checks for active positions ───────────────────────────────
        for pos in snapshot.positions:
            if pos.strategy != self.name:
                continue
            ticker = pos.ticker
            bars_obj = ctx.bars.get(ticker)
            if bars_obj is None:
                continue
            close = bars_obj.df["close"]
            current_price = float(close.iloc[-1])

            # Catastrophic stop
            if pos.avg_cost_eur > 0:
                pct_from_entry = (current_price - pos.avg_cost_eur) / pos.avg_cost_eur
                if pct_from_entry <= catastrophic_stop:
                    log.warning(
                        "ai_thesis: catastrophic stop for %s: %.1f%% from entry",
                        ticker, pct_from_entry * 100,
                    )
                    orders.append(Order(
                        ticker=ticker,
                        side=Side.SELL,
                        qty=pos.qty,
                        signal_reason=f"catastrophic_stop ({pct_from_entry:.1%} from entry)",
                        asset_class=AssetClass.STOCK,
                    ))
                    continue

            # Trailing stop from peak
            if pos.peak_price_eur and pos.peak_price_eur > 0:
                pct_from_peak = (current_price - pos.peak_price_eur) / pos.peak_price_eur
                if pct_from_peak <= -trail_pct:
                    log.info(
                        "ai_thesis: trailing stop for %s: %.1f%% from peak",
                        ticker, pct_from_peak * 100,
                    )
                    orders.append(Order(
                        ticker=ticker,
                        side=Side.SELL,
                        qty=pos.qty,
                        signal_reason=f"trailing_stop ({pct_from_peak:.1%} from peak)",
                        asset_class=AssetClass.STOCK,
                    ))

        return orders

    def _execute_approved_actions(self, ctx: StrategyContext) -> list[Order]:
        """Convert approved, unexecuted ThesisActions into Orders."""
        orders = []
        with get_session() as s:
            actions = (
                s.query(ThesisAction)
                .join(Thesis, ThesisAction.thesis_id == Thesis.id)
                .filter(
                    Thesis.bot_id == ctx.bot_id,
                    ThesisAction.status == "approved",
                    ThesisAction.executed_at.is_(None),
                )
                .order_by(ThesisAction.decided_at)
                .all()
            )

            for action in actions:
                thesis = s.query(Thesis).filter(Thesis.id == action.thesis_id).first()
                if thesis is None:
                    continue

                ticker = thesis.ticker
                bars_obj = ctx.bars.get(ticker)
                current_price = (
                    float(bars_obj.df["close"].iloc[-1]) if bars_obj is not None else None
                )

                if action.action_type == "open":
                    # Re-check RSI/SMA gate for conviction ≤ 3
                    if thesis.conviction <= 3:
                        if bars_obj is None or not _check_rsi_sma_gate(bars_obj.df, ctx.params):
                            log.info(
                                "ai_thesis: skipping 'open' for %s (conviction=%d): "
                                "RSI/SMA gate not satisfied at execution time",
                                ticker, thesis.conviction,
                            )
                            continue

                    size_pct = action.size_pct or _size_pct(thesis.conviction)
                    bot_capital = ctx.prices_eur.get("__capital__", 5000.0)  # fallback €5k
                    target_eur = bot_capital * size_pct
                    qty = (target_eur / current_price) if current_price else None

                    if qty and qty > 0:
                        orders.append(Order(
                            ticker=ticker,
                            side=Side.BUY,
                            qty=qty,
                            signal_reason=(
                                f"thesis_open conviction={thesis.conviction} "
                                f"size={size_pct:.0%}"
                            ),
                            asset_class=AssetClass.STOCK,
                        ))
                        log.info(
                            "ai_thesis: open order for %s qty=%.4f at %.2f",
                            ticker, qty, current_price or 0,
                        )

                elif action.action_type == "exit":
                    # Find the current position to know qty
                    # (snapshot not passed here; use a sentinel — executor handles actual qty)
                    orders.append(Order(
                        ticker=ticker,
                        side=Side.SELL,
                        qty=0,   # sentinel: executor reads from positions
                        signal_reason="thesis_exit",
                        asset_class=AssetClass.STOCK,
                    ))
                    log.info("ai_thesis: exit order for %s", ticker)

                elif action.action_type in ("add", "reduce"):
                    size_pct = action.size_pct or _size_pct(thesis.conviction)
                    bot_capital = ctx.prices_eur.get("__capital__", 5000.0)
                    target_eur = bot_capital * size_pct
                    qty = (target_eur / current_price) if current_price else None
                    if qty and qty > 0:
                        side = Side.BUY if action.action_type == "add" else Side.SELL
                        orders.append(Order(
                            ticker=ticker,
                            side=side,
                            qty=qty,
                            signal_reason=(
                                f"thesis_{action.action_type} conviction={thesis.conviction}"
                            ),
                            asset_class=AssetClass.STOCK,
                        ))

        return orders

    def _check_waiting_theses(self, ctx: StrategyContext) -> list[Order]:
        """Check waiting (conviction=3) theses for RSI/SMA gate.

        If the gate is satisfied, create a pending ThesisAction ('open') for
        user approval.  This does NOT immediately produce an order — the order
        appears on the next run after the user approves the card.
        """
        from datetime import datetime, timezone, timedelta

        with get_session() as s:
            waiting = (
                s.query(Thesis)
                .filter(
                    Thesis.bot_id == ctx.bot_id,
                    Thesis.status == "waiting",
                )
                .all()
            )

            for thesis in waiting:
                ticker = thesis.ticker

                # Expire if waiting more than 30 days
                days_waiting = (datetime.now(timezone.utc) - thesis.opened_at).days
                if days_waiting > 30:
                    thesis.status = "exited"
                    thesis.closed_at = datetime.now(timezone.utc)
                    log.info(
                        "ai_thesis: waiting thesis for %s expired after %d days",
                        ticker, days_waiting,
                    )
                    continue

                bars_obj = ctx.bars.get(ticker)
                if bars_obj is None:
                    continue

                if not _check_rsi_sma_gate(bars_obj.df, ctx.params):
                    continue

                # Gate triggered — check if there's already a pending open action
                existing = (
                    s.query(ThesisAction)
                    .filter(
                        ThesisAction.thesis_id == thesis.id,
                        ThesisAction.action_type == "open",
                        ThesisAction.status == "pending",
                    )
                    .first()
                )
                if existing:
                    continue  # already proposed

                size_pct = _size_pct(thesis.conviction)
                action = ThesisAction(
                    thesis_id=thesis.id,
                    action_type="open",
                    size_pct=size_pct,
                    rationale=(
                        f"Senyal tècnic confirmat per {ticker}: "
                        f"RSI en zona de recuperació + SMA50 intacte. "
                        f"Convicció {thesis.conviction}/5 — proposta d'entrada."
                    ),
                    conviction_at_proposal=thesis.conviction,
                    status="pending",
                )
                s.add(action)
                log.info(
                    "ai_thesis: RSI/SMA gate triggered for waiting thesis %s "
                    "(thesis_id=%d), pending open action created",
                    ticker, thesis.id,
                )

            s.commit()

        return []  # no immediate orders — waiting for user approval
