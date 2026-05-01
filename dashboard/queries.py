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
    """Carrega els noms complets des de contracts.json, amb fallback per tickers no resolts."""
    from core.config import DATA_DIR

    # Supplementary / override names.
    # Used for: (a) tickers not in contracts.json, (b) IBKR names that have
    # awkward casing or truncation (e.g. "LVMH MOET HENNESSY LOUIS VUI").
    _FALLBACK_NAMES: dict[str, str] = {
        # Tickers not resolved via IBKR (stocks_aggressive universe)
        "TSLA":    "Tesla Inc",
        "AMD":     "Advanced Micro Devices",
        "PLTR":    "Palantir Technologies",
        # New US additions
        "AVGO":    "Broadcom Inc",
        "MU":      "Micron Technology",
        "QCOM":    "Qualcomm",
        "LLY":     "Eli Lilly & Co",
        "COST":    "Costco Wholesale",
        "BKNG":    "Booking Holdings",
        "UBER":    "Uber Technologies",
        "SHOP":    "Shopify Inc",
        "CRM":     "Salesforce Inc",
        "NOW":     "ServiceNow Inc",
        # New EU additions
        "IFX.DE":  "Infineon Technologies AG",
        "RMS.PA":  "Hermès International",
        "OR.PA":   "L'Oréal SA",
        "RHM.DE":  "Rheinmetall AG",
        "BNP.PA":  "BNP Paribas",
        "AIR.PA":  "Airbus SE",
        "ALV.DE":  "Allianz SE",
        "BMW.DE":  "BMW Group",
        "SAP.DE":  "SAP SE",
        "SIE.DE":  "Siemens AG",
        "BAYN.DE": "Bayer AG",
        # Brand-name overrides (IBKR names have awkward casing / truncation)
        "MC.PA":   "LVMH Moët Hennessy",
        "SXR8.DE": "iShares Core S&P 500 UCITS",
        "SXRV.DE": "iShares NASDAQ 100 UCITS",
        "EXSA.DE": "iShares Euro Stoxx 600 UCITS",
        "XDWD.DE": "Xtrackers MSCI World UCITS",
        "QDVE.DE": "iShares S&P 500 IT Sector",
        "QDVH.DE": "iShares S&P 500 Financials",
        "ZPRR.DE": "SPDR Russell 2000 UCITS",
        "BTCE.DE": "WisdomTree Physical Bitcoin",
        "ZETH.DE": "21Shares Ethereum ETP",
        "ISJP.DE": "iShares MSCI Japan Small Cap",
        "SXR1.DE": "iShares Core MSCI Pacific ex-JP",
        "TTE.PA":  "TotalEnergies SE",
        "NESN.SW": "Nestlé SA",
        "NOVN.SW": "Novartis AG",
        "ASML.AS": "ASML Holding NV",
    }

    path = DATA_DIR / "contracts.json"
    names: dict[str, str] = {}
    data: dict = {}

    # Layer 1: auto-generate names from contracts.json (yf ticker + IBKR local symbol).
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        for ticker, entry in data.items():
            raw = entry.get("long_name") or ticker
            nice = _title_name(raw)
            names[ticker] = nice
            # Also index by IBKR local symbol so that IBKR portfolio/executions
            # tables (which use bare symbols like "AIR", "BNP", "TTE") resolve too.
            ibkr_sym = entry.get("local_symbol") or entry.get("symbol")
            if ibkr_sym and ibkr_sym not in names:
                names[ibkr_sym] = nice

    # Layer 2: apply hand-curated overrides — these WIN over auto-generated names.
    # Also propagate each override to the corresponding IBKR local symbol so that
    # IBKR-sourced tables use the same corrected name.
    for yf_ticker, nice_name in _FALLBACK_NAMES.items():
        names[yf_ticker] = nice_name
        entry = data.get(yf_ticker, {})
        ibkr_sym = entry.get("local_symbol") or entry.get("symbol")
        if ibkr_sym:
            names[ibkr_sym] = nice_name

    return names


def _title_name(s: str) -> str:
    """Convert an ALL-CAPS company name to Title Case with sensible exceptions."""
    # Preserve common abbreviations / legal suffixes correctly
    _KEEP = {
        "AG": "AG", "SE": "SE", "NV": "NV", "SA": "SA", "PLC": "PLC",
        "INC": "Inc", "CORP": "Corp", "CO": "Co", "LLC": "LLC",
        "ETF": "ETF", "UCITS": "UCITS", "USD": "USD", "EUR": "EUR",
        "ACC": "Acc", "DIS": "Dis", "REG": "Reg", "CL": "Cl",
        "S&P": "S&P", "MSCI": "MSCI", "SPDR": "SPDR",
    }
    words = []
    for word in s.split():
        upper = word.upper().rstrip("-")
        if upper in _KEEP:
            words.append(_KEEP[upper] + word[len(upper):])
        else:
            words.append(word.capitalize())
    return " ".join(words)


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


@st.cache_data(ttl=60)
def _closed_positions() -> pd.DataFrame:
    """Compute closed-position P&L from the trades ledger.

    For each (bot_id, ticker) that has at least one SELL trade, reconstructs
    individual round-trip rows using FIFO matching: each SELL is paired against
    the weighted-average entry price of all preceding BUYs for that bot/ticker.
    """
    names = _asset_names()
    with get_session() as s:
        all_trades = (
            s.query(Trade)
            .order_by(Trade.bot_id, Trade.ticker, Trade.timestamp)
            .all()
        )
    # Filter cancelled trades in Python — avoids crashing on DB schemas where
    # the status column was added by migration but may not exist on older envs.
    all_trades = [
        t for t in all_trades
        if getattr(t, "status", "filled") != "cancelled"
    ]

    from collections import defaultdict

    # Group by (bot_id, ticker)
    groups: dict[tuple, list] = defaultdict(list)
    for t in all_trades:
        groups[(t.bot_id, t.ticker)].append(t)

    rows = []
    for (bot_id, ticker), trades in groups.items():
        sells = [t for t in trades if t.side == "SELL"]
        if not sells:
            continue

        buys = [t for t in trades if t.side == "BUY"]
        total_buy_qty = sum(t.qty for t in buys)
        total_buy_cost = sum(t.qty * t.price_eur for t in buys)
        total_buy_fees = sum(t.fee_eur for t in buys)
        avg_entry = total_buy_cost / total_buy_qty if total_buy_qty else 0.0

        first_entry = min((t.timestamp for t in buys), default=None)

        for sell in sells:
            sell_qty = sell.qty
            sell_rev = sell_qty * sell.price_eur
            # Allocate buy-side fees proportional to sold qty
            alloc_buy_fee = (total_buy_fees * sell_qty / total_buy_qty) if total_buy_qty else 0.0
            pl = sell_rev - avg_entry * sell_qty - sell.fee_eur - alloc_buy_fee
            pl_pct = (sell.price_eur / avg_entry - 1) * 100 if avg_entry else 0.0
            entry_date = first_entry.date() if first_entry else None
            exit_date = sell.timestamp.date()
            rows.append({
                "bot_id":          bot_id,
                "ticker":          ticker,
                "nom":             names.get(ticker, ticker),
                "data_entrada":    entry_date,
                "data_sortida":    exit_date,
                "dies":            (exit_date - entry_date).days if entry_date else "—",
                "quantitat":       int(sell_qty),
                "preu_entrada":    f"€{avg_entry:,.2f}",
                "preu_sortida":    f"€{sell.price_eur:,.2f}",
                "P&L €":          f"€{pl:+,.2f}",
                "P&L %":          f"{pl_pct:+.1f}%",
            })

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("data_sortida", ascending=False).reset_index(drop=True)
    return df


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
                    "tipus": "🕐 Auto" if (getattr(r.RunLog, "triggered_by", None) or "auto") == "auto" else "👤 Manual",
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


def _fetch_prices_eur(tickers: tuple[str, ...]) -> dict[str, float]:
    """Fetch end-of-day close prices in EUR for a list of tickers.

    Not decorated with @st.cache_data — market_data._CACHE already deduplicates
    yfinance downloads within the process.  The st.cache_data layer caused
    stale USD prices to persist across hot-reloads when only market_data.py
    was edited (Streamlit reruns app.py but keeps imported modules in
    sys.modules, so the cached wrong result kept being returned).
    """
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
