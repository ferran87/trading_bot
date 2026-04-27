"""Streamlit-cached reads from SQLite and market helpers."""
from __future__ import annotations

import json
import logging

import pandas as pd
import streamlit as st

log = logging.getLogger(__name__)

from core.db import Bot, EquitySnapshot, Position, RunLog, Trade, get_session


@st.cache_data(ttl=30)
def _load_bots() -> pd.DataFrame:
    from core.config import CONFIG
    bot_cfgs = {b["id"]: b for b in CONFIG.strategies.get("bots", [])}
    with get_session() as s:
        rows = s.query(Bot).order_by(Bot.id).all()
        return pd.DataFrame(
            [
                {
                    "id": b.id,
                    "name": b.name,
                    "strategy": b.strategy,
                    "initial_eur": b.initial_capital_eur,
                    "enabled": bool(b.enabled),
                    "owner": b.owner or f"Bot {b.id}",
                    "trading_mode": getattr(b, "trading_mode", "paper"),
                    "ibkr_port": bot_cfgs.get(b.id, {}).get("ibkr_port"),
                    "ibkr_port_paper": bot_cfgs.get(b.id, {}).get("ibkr_port_paper"),
                }
                for b in rows
            ]
        )


def _set_trading_mode(bot_id: int, mode: str) -> None:
    """Toggle a bot between 'paper' and 'live' mode and clear the bots cache."""
    with get_session() as s:
        bot = s.query(Bot).filter(Bot.id == bot_id).one()
        bot.trading_mode = mode
        s.commit()
    _load_bots.clear()


def _set_bot_enabled(bot_id: int, enabled: bool) -> None:
    """Enable or disable a bot (used for live trading toggle) and clear the bots cache."""
    with get_session() as s:
        bot = s.query(Bot).filter(Bot.id == bot_id).one()
        bot.enabled = 1 if enabled else 0
        s.commit()
    _load_bots.clear()


def _set_owner_active_strategies(
    owner: str,
    active_strategies: list[str],
    *,
    reset_live: bool = True,
) -> None:
    """Enable paper bots matching active_strategies, disable others for this owner.

    If reset_live=True (default), all live bots for this owner are disabled so the
    user must explicitly re-enable live trading after changing the strategy.
    """
    with get_session() as s:
        bots = s.query(Bot).filter(Bot.owner == owner).all()
        for bot in bots:
            if bot.trading_mode == "paper":
                bot.enabled = 1 if bot.strategy in active_strategies else 0
            elif bot.trading_mode == "live" and reset_live:
                bot.enabled = 0
        s.commit()
    _load_bots.clear()


def _set_owner_mode_strategies(
    owner: str,
    mode: str,
    active_strategies: list[str],
) -> None:
    """Set which strategies are active for the given trading mode.

    Paper: enables matching paper bots, disables the rest (immediate effect).
    Live:  disables ALL live bots (safety — user must explicitly re-enable via
           the live toggle after choosing the new strategy).
    """
    with get_session() as s:
        bots = (
            s.query(Bot)
            .filter(Bot.owner == owner, Bot.trading_mode == mode)
            .all()
        )
        for bot in bots:
            if mode == "paper":
                bot.enabled = 1 if bot.strategy in active_strategies else 0
            else:
                bot.enabled = 0  # always disables all live bots on strategy change
        s.commit()
    _load_bots.clear()


def _set_owner_live_enabled(
    owner: str,
    enabled: bool,
    active_strategies: list[str],
) -> None:
    """Enable or disable live bots for this owner, filtered by active_strategies."""
    with get_session() as s:
        bots = (
            s.query(Bot)
            .filter(Bot.owner == owner, Bot.trading_mode == "live")
            .all()
        )
        for bot in bots:
            if bot.strategy in active_strategies:
                bot.enabled = 1 if enabled else 0
            else:
                bot.enabled = 0
        s.commit()
    _load_bots.clear()


@st.cache_data(ttl=60)
def _reconcile_cached(bot_ids: tuple[int, ...], ibkr_port: int) -> list[dict]:
    """Cached wrapper around the reconciliation agent."""
    try:
        from agents.reconciliation import reconcile_positions, format_report
        discrepancies = reconcile_positions(list(bot_ids), ibkr_port)
        return [
            {
                "ticker":     d.ticker,
                "sqlite_qty": d.sqlite_qty,
                "ibkr_qty":   d.ibkr_qty,
                "diff":       d.diff,
                "severity":   d.severity,
            }
            for d in discrepancies
        ]
    except Exception as exc:
        return [{"ticker": "ERROR", "severity": "ERROR", "diff": 0,
                 "sqlite_qty": 0, "ibkr_qty": 0, "error": str(exc)}]


@st.cache_data(ttl=30)
def _equity_history() -> pd.DataFrame:
    with get_session() as s:
        rows = s.query(EquitySnapshot).order_by(EquitySnapshot.snap_date).all()
        df = pd.DataFrame(
            [
                {
                    "bot_id": r.bot_id,
                    "date": r.snap_date,
                    "cash": r.cash_eur,
                    "positions": r.positions_value_eur,
                    "total": r.total_eur,
                }
                for r in rows
            ]
        )
        if not df.empty:
            df["date"] = pd.to_datetime(df["date"]).dt.normalize()
        return df


@st.cache_data(ttl=300)
def _asset_names() -> dict[str, str]:
    """Carrega els noms complets des de contracts.json."""
    from core.config import DATA_DIR

    path = DATA_DIR / "contracts.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        ticker: entry.get("long_name", ticker).title()
        for ticker, entry in data.items()
    }


@st.cache_data(ttl=30)
def _open_positions() -> pd.DataFrame:
    names = _asset_names()
    with get_session() as s:
        positions = s.query(Position).all()
        # Fetch first BUY trade per (bot_id, ticker) for the entry signal reason.
        from sqlalchemy import func
        first_buy_ids = (
            s.query(func.min(Trade.id))
            .filter(Trade.side == "BUY")
            .group_by(Trade.bot_id, Trade.ticker)
            .subquery()
        )
        entry_trades = s.query(Trade).filter(Trade.id.in_(first_buy_ids)).all()
        entry_reason: dict[tuple, str] = {
            (t.bot_id, t.ticker): t.signal_reason for t in entry_trades
        }
        return pd.DataFrame(
            [
                {
                    "bot_id": p.bot_id,
                    "ticker": p.ticker,
                    "nom": names.get(p.ticker, p.ticker),
                    "quantitat": p.qty,
                    "preu_entrada_eur": round(p.avg_entry_eur, 2),
                    "cost_eur": round(p.qty * p.avg_entry_eur, 2),
                    "data_entrada": p.entry_date,
                    "senyal_entrada": entry_reason.get((p.bot_id, p.ticker), "—"),
                }
                for p in positions
            ]
        )


@st.cache_data(ttl=30)
def _trades(limit: int = 500) -> pd.DataFrame:
    names = _asset_names()
    with get_session() as s:
        rows = (
            s.query(Trade).order_by(Trade.timestamp.desc()).limit(limit).all()
        )
        return pd.DataFrame(
            [
                {
                    "bot_id": t.bot_id,
                    "data": t.timestamp,
                    "ticker": t.ticker,
                    "nom": names.get(t.ticker, t.ticker),
                    "operació": t.side,
                    "quantitat": t.qty,
                    "preu_eur": round(t.price_eur, 2),
                    "total_eur": round(t.qty * t.price_eur, 2),
                    "comissió_eur": round(t.fee_eur, 2),
                    "senyal": t.signal_reason,
                    "estat": getattr(t, "status", "filled"),  # "filled" | "pending"
                }
                for t in rows
            ]
        )


@st.cache_data(ttl=60)
def _run_logs(limit: int = 100) -> pd.DataFrame:
    with get_session() as s:
        rows = (
            s.query(RunLog, Bot.name)
            .join(Bot, RunLog.bot_id == Bot.id)
            .order_by(RunLog.timestamp.desc())
            .limit(limit)
            .all()
        )
        return pd.DataFrame(
            [
                {
                    "bot_id": r.RunLog.bot_id,
                    "bot": r.name,
                    "data_execució": r.RunLog.timestamp,
                    "data_mercat": r.RunLog.run_date,
                    "compres": r.RunLog.n_buys,
                    "vendes": r.RunLog.n_sells,
                    "rebutjades": r.RunLog.n_rejected,
                    "decisió": r.RunLog.summary,
                    "explicació": r.RunLog.explanation or "",
                }
                for r in rows
            ]
        )


@st.cache_data(ttl=60)
def _ibkr_account_eur(port: int) -> dict[str, float] | None:
    """Fetch cash, invested and total equity in EUR directly from IBKR Gateway.

    Returns dict with keys: cash_eur, invested_eur, total_eur.
    Returns None if the Gateway is unreachable.
    """
    try:
        from ib_async import IB
        ib = IB()
        ib.connect("127.0.0.1", port, clientId=50, timeout=5)
        account = ib.managedAccounts()[0]
        ib.sleep(2)
        vals = ib.accountValues(account)
        ib.disconnect()

        def _get(tag: str) -> float | None:
            for ccy in ("EUR", "BASE"):
                for v in vals:
                    if v.tag == tag and v.currency == ccy:
                        return float(v.value)
            return None

        cash = _get("TotalCashValue")
        total = _get("NetLiquidation")
        if cash is None or total is None:
            return None
        invested = max(total - cash, 0.0)
        return {"cash_eur": cash, "invested_eur": invested, "total_eur": total}
    except Exception:
        return None


@st.cache_data(ttl=60)
def _ibkr_cash_eur(port: int) -> float | None:
    """Fetch TotalCashValue in EUR directly from IBKR Gateway. Returns None if unavailable."""
    result = _ibkr_account_eur(port)
    return result["cash_eur"] if result else None


@st.cache_data(ttl=300)
def _fetch_prices_eur(tickers: tuple[str, ...]) -> dict[str, float]:
    """Fetch end-of-day close prices in EUR for a list of tickers."""
    from analysis import market_data

    bars = market_data.prefetch_since(list(tickers), 5)
    return market_data.last_prices_eur(bars)


# ── IBKR live portfolio & executions ──────────────────────────────────────────

_IBKR_SENTINEL = 1.7976931348623157e+308  # value IBKR uses for "not yet reported"


@st.cache_data(ttl=30)
def _ibkr_portfolio(port: int) -> pd.DataFrame:
    """Fetch ALL live positions from the IBKR account (including manual ones).

    Returns a DataFrame with columns:
      ticker, qty, avg_cost_native, market_price_native, market_value_native,
      unrealized_pnl_native, realized_pnl_native, contract_currency

    All *_native values are in the account's base currency (USD for US paper
    accounts). Returns an empty DataFrame if the Gateway is unreachable.
    """
    try:
        from ib_async import IB
        ib = IB()
        ib.connect("127.0.0.1", port, clientId=51, timeout=5)
        account = ib.managedAccounts()[0]
        ib.sleep(2)
        items = ib.portfolio(account)
        ib.disconnect()
        rows = []
        for item in items:
            if float(item.position) == 0:
                continue
            rows.append({
                "ticker":                item.contract.localSymbol or item.contract.symbol,
                "qty":                   float(item.position),
                "avg_cost_native":       float(item.averageCost),
                "market_price_native":   float(item.marketPrice),
                "market_value_native":   float(item.marketValue),
                "unrealized_pnl_native": float(item.unrealizedPNL),
                "realized_pnl_native":   float(item.realizedPNL),
                "contract_currency":     item.contract.currency,
            })
        return pd.DataFrame(rows)
    except Exception as exc:
        log.warning("_ibkr_portfolio: port=%d: %s", port, exc)
        return pd.DataFrame()


@st.cache_data(ttl=60)
def _ibkr_executions(port: int) -> pd.DataFrame:
    """Fetch recent execution fills from IBKR with actual commissions charged.

    Returns a DataFrame with columns:
      time, ticker, side, qty, price, commission, comm_currency, realized_pnl

    Commission and realized_pnl are None when IBKR hasn't confirmed them yet.
    Returns an empty DataFrame if the Gateway is unreachable.
    """
    try:
        from ib_async import IB
        ib = IB()
        ib.connect("127.0.0.1", port, clientId=52, timeout=5)
        ib.sleep(1)
        fills = ib.reqExecutions()
        ib.sleep(1)
        ib.disconnect()
        rows = []
        for fill in fills:
            cr = fill.commissionReport
            comm = float(cr.commission) if cr and cr.commission != _IBKR_SENTINEL else None
            rpnl = float(cr.realizedPNL) if cr and cr.realizedPNL != _IBKR_SENTINEL else None
            rows.append({
                "time":          fill.execution.time,
                "ticker":        fill.contract.localSymbol or fill.contract.symbol,
                "side":          fill.execution.side,
                "qty":           float(fill.execution.shares),
                "price":         float(fill.execution.price),
                "commission":    comm,
                "comm_currency": cr.currency if cr else None,
                "realized_pnl":  rpnl,
            })
        return pd.DataFrame(rows)
    except Exception as exc:
        log.warning("_ibkr_executions: port=%d: %s", port, exc)
        return pd.DataFrame()
