"""Streamlit dashboard.

Run with: `streamlit run dashboard/app.py`
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


st.set_page_config(page_title="Tauler de Trading", layout="wide")
st.title("Bot de Trading — Tauler de Paper Trading")
st.caption(
    f"Backend: **{CONFIG.broker_backend}** · Moneda base: **{CONFIG.settings['base_currency']}** · "
    f"BD: `{CONFIG.db_path}`"
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
    import json
    from core.config import DATA_DIR
    path = DATA_DIR / "contracts.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {ticker: entry.get("long_name", ticker).title()
            for ticker, entry in data.items()}


@st.cache_data(ttl=30)
def _open_positions() -> pd.DataFrame:
    names = _asset_names()
    with get_session() as s:
        rows = s.query(Position).all()
        return pd.DataFrame(
            [
                {
                    "bot_id": p.bot_id,
                    "ticker": p.ticker,
                    "nom": names.get(p.ticker, p.ticker),
                    "quantitat": p.qty,
                    "preu_entrada_eur": round(p.avg_entry_eur, 2),
                    "total_eur": round(p.qty * p.avg_entry_eur, 2),
                    "data_entrada": p.entry_date,
                }
                for p in rows
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
                }
                for t in rows
            ]
        )


def _kpis_for(bot: dict, equity_df: pd.DataFrame, trades_df: pd.DataFrame) -> dict:
    bot_eq = equity_df[equity_df["bot_id"] == bot["id"]].sort_values("date")
    ser = bot_eq["total"]
    if ser.empty:
        total = float(bot["initial_eur"])
        cash = total
        invested = 0.0
        ret = 0.0
        sharpe = float("nan")
        max_dd = 0.0
    else:
        total = float(ser.iloc[-1])
        cash = float(bot_eq["cash"].iloc[-1])
        invested = float(bot_eq["positions"].iloc[-1])
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
    fees = float(bot_trades["comissió_eur"].sum()) if not bot_trades.empty else 0.0
    return {
        "total_eur": total,
        "cash_eur": cash,
        "invested_eur": invested,
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
    st.warning("Cap bot a la base de dades. Executa `python main.py --init-db` primer.")
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
        st.caption(f"Estratègia: `{bot['strategy']}`")
        st.metric("Patrimoni total (EUR)", f"€{kpi['total_eur']:,.2f}", f"{kpi['return_pct']*100:+.2f}%")
        c1, c2 = st.columns(2)
        c1.metric("Efectiu", f"€{kpi['cash_eur']:,.2f}")
        c2.metric("Invertit", f"€{kpi['invested_eur']:,.2f}")
        st.metric("Sharpe (anual.)", "—" if pd.isna(kpi["sharpe"]) else f"{kpi['sharpe']:.2f}")
        st.metric("Màxima caiguda", f"{kpi['max_dd']*100:.2f}%")
        st.metric("Comissions pagades", f"€{kpi['fees_eur']:,.2f}")
        st.metric("Operacions", kpi["n_trades"])
        if breached:
            st.error(f"⚠ Per sota del mínim de cartera (€{floor}) — el bot hauria d'aturar-se")

st.divider()

st.subheader("Evolució del patrimoni")
if equity_df.empty:
    st.info("Encara no hi ha dades. Executa `python main.py --once` almenys una vegada.")
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
    fig.add_hline(y=floor, line_dash="dot", annotation_text=f"Mínim €{floor}")
    fig.update_layout(height=400, yaxis_title="Patrimoni (EUR)", margin=dict(t=20, b=20))
    fig.update_xaxes(type="date", dtick="D1", tickformat="%d/%m/%Y")
    st.plotly_chart(fig, use_container_width=True)

st.divider()

left, right = st.columns(2)
with left:
    st.subheader("Posicions obertes")
    if positions_df.empty:
        st.caption("Cap posició oberta.")
    else:
        st.dataframe(positions_df, use_container_width=True, hide_index=True)

with right:
    st.subheader("Estat dels límits de risc")
    today = date.today()
    with get_session() as s:
        rows = []
        for _, bot in bots_df.iterrows():
            placed = Portfolio.trades_today(s, int(bot["id"]), today)
            rows.append({
                "bot_id": bot["id"],
                "operacions_avui": placed,
                "límit_diari": CONFIG.settings["guardrails"]["max_trades_per_day"],
            })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

st.divider()

st.subheader("Registre d'operacions")
if trades_df.empty:
    st.caption("Encara no hi ha operacions.")
else:
    st.dataframe(trades_df, use_container_width=True, hide_index=True)
