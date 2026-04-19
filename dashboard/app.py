"""Streamlit dashboard.

Run with: `streamlit run dashboard/app.py`

Phase 1: KPIs, equity-curve chart, open positions, trade log, guardrail
status. Wired only to Bot 1 today because Bots 2 and 3 are disabled in
strategies.yaml — the dashboard renders whatever `bots` rows are in the DB.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config import CONFIG  # noqa: E402
from core.db import Bot, EquitySnapshot, Position, Trade, get_session  # noqa: E402
from core.portfolio import Portfolio  # noqa: E402


st.set_page_config(page_title="Trading Bot Dashboard", layout="wide")
st.title("Trading Bot — Paper Trading Dashboard")
st.caption(
    f"Backend: **{CONFIG.broker_backend}** · Base: **{CONFIG.settings['base_currency']}** · "
    f"DB: `{CONFIG.db_path}`"
)


@st.cache_data(ttl=30)
def _load_bots() -> pd.DataFrame:
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
                }
                for b in rows
            ]
        )


@st.cache_data(ttl=30)
def _equity_history() -> pd.DataFrame:
    with get_session() as s:
        rows = s.query(EquitySnapshot).order_by(EquitySnapshot.snap_date).all()
        return pd.DataFrame(
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


@st.cache_data(ttl=30)
def _open_positions() -> pd.DataFrame:
    with get_session() as s:
        rows = s.query(Position).all()
        return pd.DataFrame(
            [
                {
                    "bot_id": p.bot_id,
                    "ticker": p.ticker,
                    "qty": p.qty,
                    "avg_entry_eur": p.avg_entry_eur,
                    "entry_date": p.entry_date,
                }
                for p in rows
            ]
        )


@st.cache_data(ttl=30)
def _trades(limit: int = 500) -> pd.DataFrame:
    with get_session() as s:
        rows = (
            s.query(Trade).order_by(Trade.timestamp.desc()).limit(limit).all()
        )
        return pd.DataFrame(
            [
                {
                    "bot_id": t.bot_id,
                    "timestamp": t.timestamp,
                    "ticker": t.ticker,
                    "side": t.side,
                    "qty": t.qty,
                    "price_eur": t.price_eur,
                    "fee_eur": t.fee_eur,
                    "signal": t.signal_reason,
                }
                for t in rows
            ]
        )


def _kpis_for(bot: dict, equity_df: pd.DataFrame, trades_df: pd.DataFrame) -> dict:
    ser = equity_df[equity_df["bot_id"] == bot["id"]].sort_values("date")["total"]
    if ser.empty:
        total = float(bot["initial_eur"])
        ret = 0.0
        sharpe = float("nan")
        max_dd = 0.0
    else:
        total = float(ser.iloc[-1])
        ret = total / float(bot["initial_eur"]) - 1.0
        daily = ser.pct_change().dropna()
        sharpe = (
            (daily.mean() / daily.std()) * (252 ** 0.5)
            if daily.std() not in (0, float("nan")) and len(daily) > 1
            else float("nan")
        )
        running_max = ser.cummax()
        max_dd = float(((ser - running_max) / running_max).min()) if len(ser) else 0.0

    bot_trades = trades_df[trades_df["bot_id"] == bot["id"]]
    fees = float(bot_trades["fee_eur"].sum()) if not bot_trades.empty else 0.0
    return {
        "total_eur": total,
        "return_pct": ret,
        "sharpe": sharpe,
        "max_dd": max_dd,
        "fees_eur": fees,
        "n_trades": len(bot_trades),
    }


bots_df = _load_bots()
equity_df = _equity_history()
positions_df = _open_positions()
trades_df = _trades()

if bots_df.empty:
    st.warning("No bots in the database. Run `python main.py --init-db` first.")
    st.stop()

floor = CONFIG.settings["guardrails"]["portfolio_floor_eur"]

cols = st.columns(len(bots_df))
for col, (_, bot) in zip(cols, bots_df.iterrows()):
    with col:
        kpi = _kpis_for(bot, equity_df, trades_df)
        breached = kpi["total_eur"] < floor
        badge = "🟢" if bot["enabled"] else "⚪"
        if breached:
            badge = "🔴"
        st.subheader(f"{badge} Bot {bot['id']} — {bot['name']}")
        st.caption(f"Strategy: `{bot['strategy']}`")
        st.metric("Equity (EUR)", f"€{kpi['total_eur']:,.2f}", f"{kpi['return_pct']*100:+.2f}%")
        st.metric("Sharpe (ann.)", "—" if pd.isna(kpi["sharpe"]) else f"{kpi['sharpe']:.2f}")
        st.metric("Max drawdown", f"{kpi['max_dd']*100:.2f}%")
        st.metric("Fees paid", f"€{kpi['fees_eur']:,.2f}")
        st.metric("Trades", kpi["n_trades"])
        if breached:
            st.error(f"⚠ Below portfolio floor (€{floor}) — bot should be halted")

st.divider()

st.subheader("Equity curves")
if equity_df.empty:
    st.info("No equity snapshots yet. Run `python main.py --once` at least once.")
else:
    fig = go.Figure()
    for _, bot in bots_df.iterrows():
        sub = equity_df[equity_df["bot_id"] == bot["id"]].sort_values("date")
        if sub.empty:
            continue
        fig.add_trace(
            go.Scatter(
                x=sub["date"], y=sub["total"],
                mode="lines+markers", name=f"Bot {bot['id']} {bot['name']}",
            )
        )
    fig.add_hline(y=floor, line_dash="dot", annotation_text=f"Floor €{floor}")
    fig.update_layout(height=400, yaxis_title="Equity (EUR)", margin=dict(t=20, b=20))
    st.plotly_chart(fig, use_container_width=True)

st.divider()

left, right = st.columns(2)
with left:
    st.subheader("Open positions")
    if positions_df.empty:
        st.caption("No open positions.")
    else:
        st.dataframe(positions_df, use_container_width=True, hide_index=True)

with right:
    st.subheader("Guardrail status")
    today = date.today()
    with get_session() as s:
        rows = []
        for _, bot in bots_df.iterrows():
            placed = Portfolio.trades_today(s, int(bot["id"]), today)
            rows.append({"bot_id": bot["id"], "trades_today": placed,
                         "daily_limit": CONFIG.settings["guardrails"]["max_trades_per_day"]})
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

st.divider()

st.subheader("Trade log")
if trades_df.empty:
    st.caption("No trades yet.")
else:
    st.dataframe(trades_df, use_container_width=True, hide_index=True)
