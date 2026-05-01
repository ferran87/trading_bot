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
from core.db import get_session  # noqa: E402
from core.portfolio import Portfolio  # noqa: E402

from dashboard.backtest import render_backtest_tab  # noqa: E402
from dashboard.readme_tab import render_readme_tab  # noqa: E402
from dashboard.kpis import _kpis_for  # noqa: E402
from dashboard.queries import (  # noqa: E402
    _closed_positions,
    _equity_history,
    _fetch_prices_eur,
    _ibkr_account_eur,
    _ibkr_executions,
    _ibkr_portfolio,
    _load_bots,
    _open_positions,
    _reconcile_cached,
    _run_logs,
    _set_owner_live_enabled,
    _set_owner_mode_strategies,
    _trades,
)

# ── Page config ────────────────────────────────────────────────────────────────
st.set_page_config(page_title="Trading Bot", layout="wide", page_icon="📈")

st.markdown("""
<style>
/* ── Metrics ── */
[data-testid="stMetricValue"] { font-size: 1.25rem !important; font-weight: 700; }
[data-testid="stMetricLabel"] { font-size: 0.78rem !important; opacity: 0.75; }
hr { margin: 0.6rem 0 !important; }
[data-testid="stRadio"] label { padding: 4px 0; }

/* ── Tab bar — bigger, pill-shaped, clearly clickable ── */
[data-testid="stTabs"] [role="tablist"] {
    gap: 6px;
    padding: 6px 4px;
    background: rgba(128,128,128,0.07);
    border-radius: 12px;
    border-bottom: none !important;
}
[data-testid="stTabs"] [role="tab"] {
    font-size: 0.95rem !important;
    font-weight: 600 !important;
    padding: 10px 22px !important;
    border-radius: 8px !important;
    border: 1.5px solid transparent !important;
    color: rgba(200,200,200,0.85) !important;
    background: transparent !important;
    transition: background 0.15s, color 0.15s, border-color 0.15s;
    white-space: nowrap;
}
[data-testid="stTabs"] [role="tab"]:hover {
    background: rgba(128,128,128,0.18) !important;
    color: white !important;
    border-color: rgba(128,128,128,0.35) !important;
    cursor: pointer;
}
[data-testid="stTabs"] [role="tab"][aria-selected="true"] {
    background: rgba(59,130,246,0.22) !important;
    color: #93c5fd !important;
    border-color: rgba(59,130,246,0.55) !important;
}
/* Remove the default bottom underline indicator */
[data-testid="stTabs"] [role="tab"][aria-selected="true"]::after { display: none !important; }
[data-testid="stTabs"] [role="tabpanel"] { padding-top: 1.2rem; }
</style>
""", unsafe_allow_html=True)

# ── Strategy metadata ──────────────────────────────────────────────────────────
_STRATEGY_META: dict[str, dict] = {
    "rsi_compounder": {
        "emoji":   "🤖",
        "label":   "RSI Compounder",
        "regime":  "Crashes · Correccions profundes",
        "tagline": "Deixa córrer els guanyadors, protegeix quan el mercat s'escalfa",
        "points": [
            "Entra quan una acció ha caigut molt (RSI < 25) i ha tornat a pujar (RSI entre 40 i 65)",
            "Acumula al -8% i al -15% si el preu cau més — redueix el cost mitjà",
            "Stop seguidor progressiu: 35% → 20% → 12% a mesura que el RSI puja sobre 70 i 80",
            "Dissenyat per mercats amb crashes en V; espera el moment exacte",
        ],
        "self_selects": "✅ S'auto-selecciona: roman en cash si no hi ha crash",
    },
    "rsi_recovery": {
        "emoji":   "🔄",
        "label":   "RSI Recovery",
        "regime":  "Rebots post-crash",
        "tagline": "Compra el rebot, aprofita la recuperació",
        "points": [
            "Entra quan una acció ha caigut molt (RSI < 25) i ha començat a recuperar-se",
            "Requereix que el mercat en general també hagi caigut",
            "Stop seguidor del 15% els primers 7 dies; s'eixampla al 30% un cop en guanys",
        ],
        "self_selects": "✅ S'auto-selecciona: roman en cash si no hi ha co-crash",
    },
    "trend_momentum": {
        "emoji":   "📈",
        "label":   "Trend Momentum",
        "regime":  "Bull markets · Correccions moderades",
        "tagline": "Captura correccions dins de tendències alcistes",
        "points": [
            "Entra quan el mercat (SXR8.DE) és sobre SMA200 I l'acció és sobre SMA50",
            "RSI de l'acció entre 40–62 (pull-back moderat) i RSI pujant vs fa 3 dies",
            "Stop catastròfic -15% · Trailing stop 22% des del pic · Sortida si 3 dies sota SMA50",
            "Dissenyat per mercats alcistes graduals i correccions del 10-15%",
        ],
        "self_selects": "⚠️ Actiu durant bull markets, quiet durant crashes",
    },
}

# ── Strategy selector options ──────────────────────────────────────────────────
_STRATEGY_OPTIONS: dict[str, list[str]] = {
    "🤖  RSI Compounder":  ["rsi_compounder"],
    "📈  Trend Momentum":  ["trend_momentum"],
    "🔀  Tots dos":        ["rsi_compounder", "trend_momentum"],
}


def _infer_strategy_selection(bots_for_mode: pd.DataFrame) -> str:
    """Infer which radio label matches the current DB state for a given mode's bots."""
    enabled = set(bots_for_mode[bots_for_mode["enabled"]]["strategy"])
    if enabled == {"rsi_compounder"}:
        return "🤖  RSI Compounder"
    if enabled == {"trend_momentum"}:
        return "📈  Trend Momentum"
    return "🔀  Tots dos"


def _x_axis_dtick(date_range_days: int) -> tuple[str, str]:
    if date_range_days <= 30:
        return "D1", "%d %b"
    if date_range_days <= 120:
        return "D7", "%d %b"
    if date_range_days <= 400:
        return "M1", "%b %Y"
    return "M3", "%b %Y"


# ── KPI helpers ────────────────────────────────────────────────────────────────

def _kpi_with_ibkr(
    bot: pd.Series,
    equity_df: pd.DataFrame,
    trades_df: pd.DataFrame,
    mode: str,
    ibkr_portfolio_df: pd.DataFrame | None = None,
    n_active_bots: int = 1,
) -> dict:
    """Compute KPIs, using IBKR live data when available.

    Virtual budget logic (when IBKR is connected and n_active_bots > 0):
      - Total IBKR equity is split equally across active bots.
      - Each bot's equity = its share of total equity.
      - Each bot's invested = sum of IBKR market values of its SQLite positions.
      - Each bot's virtual cash = its equity share − invested.
    """
    kpi = _kpis_for(bot, equity_df, trades_df)
    use_ibkr = CONFIG.broker_backend == "ibkr"
    if not use_ibkr:
        return kpi

    port_key = "ibkr_port_paper" if mode == "paper" else "ibkr_port"
    port = bot.get(port_key)
    if not port or pd.isna(port):
        return kpi

    ibkr_acc = _ibkr_account_eur(int(port))
    if not ibkr_acc:
        return kpi

    total_ibkr = ibkr_acc["total_eur"]
    bot_share  = total_ibkr / max(n_active_bots, 1)

    # Invested: sum of IBKR market values for this bot's tickers (from SQLite positions)
    invested_eur = 0.0
    if ibkr_portfolio_df is not None and not ibkr_portfolio_df.empty:
        rate = _eur_per_usd()
        # Which tickers does this bot hold in SQLite?
        from core.db import Position, get_session
        with get_session() as s:
            bot_positions = s.query(Position).filter(Position.bot_id == int(bot["id"])).all()
        bot_tickers = {p.ticker for p in bot_positions}
        for _, item in ibkr_portfolio_df.iterrows():
            if item["ticker"] in bot_tickers:
                invested_eur += _native_to_eur(
                    item["market_value_native"], item["contract_currency"], rate
                )

    kpi["total_eur"]    = bot_share
    kpi["invested_eur"] = invested_eur
    kpi["cash_eur"]     = max(bot_share - invested_eur, 0.0)
    kpi["return_pct"]   = bot_share / float(bot["initial_eur"]) - 1.0 if bot["initial_eur"] else 0.0
    return kpi


def _combined_kpis(bots_subset: pd.DataFrame, kpis: dict[int, dict],
                   initial_total: float,
                   live_pnls: dict[int, dict] | None = None) -> dict:
    total    = sum(k["total_eur"]    for k in kpis.values())
    cash     = sum(k["cash_eur"]     for k in kpis.values())
    invested = sum(k["invested_eur"] for k in kpis.values())
    fees     = sum(k["fees_eur"]     for k in kpis.values())
    trades   = sum(k["n_trades"]     for k in kpis.values())
    ret      = total / initial_total - 1.0 if initial_total else 0.0
    max_dd   = min(k["max_dd"] for k in kpis.values()) if kpis else 0.0
    unrealized = sum(v["unrealized_pnl_eur"] for v in (live_pnls or {}).values())
    total_pl   = total - initial_total
    realized   = total_pl - unrealized
    return {
        "total_eur": total, "cash_eur": cash, "invested_eur": invested,
        "return_pct": ret, "max_dd": max_dd, "fees_eur": fees, "n_trades": trades,
        "sharpe": float("nan"),
        "unrealized_pnl_eur": unrealized,
        "realized_pnl_eur": realized,
    }


def _compute_live_pnl_per_bot(
    bots_subset: pd.DataFrame,
    positions_df: pd.DataFrame,
) -> dict[int, dict]:
    """Compute live unrealized P&L and invested value per bot from current prices.

    Uses yfinance end-of-day prices (same source as the positions table display).
    Returns {bot_id: {"unrealized_pnl_eur": float, "live_invested_eur": float}}.
    Falls back to cost-basis when a price is unavailable (unrealized stays 0).
    """
    result: dict[int, dict] = {
        int(bot["id"]): {"unrealized_pnl_eur": 0.0, "live_invested_eur": 0.0}
        for _, bot in bots_subset.iterrows()
    }
    if positions_df.empty:
        return result

    bot_ids = {int(b) for b in bots_subset["id"]}
    active = positions_df[positions_df["bot_id"].isin(bot_ids)]
    if active.empty:
        return result

    open_tickers = tuple(active["ticker"].unique())
    live_prices  = _fetch_prices_eur(open_tickers)

    for _, bot in bots_subset.iterrows():
        bot_id  = int(bot["id"])
        bot_pos = active[active["bot_id"] == bot_id]
        unrealized   = 0.0
        live_invested = 0.0
        for _, p in bot_pos.iterrows():
            px = live_prices.get(p["ticker"])
            if px:
                live_val      = px * p["quantitat"]
                live_invested += live_val
                unrealized    += live_val - p["cost_eur"]
            else:
                live_invested += p["cost_eur"]  # fallback: price unavailable
        result[bot_id] = {
            "unrealized_pnl_eur": unrealized,
            "live_invested_eur":  live_invested,
        }
    return result


# ── Render helpers ─────────────────────────────────────────────────────────────

def _status_color(value: float, floor: float, mode: str) -> str:
    if value < floor:
        return "🔴"
    return "💶" if mode == "live" else "🧪"


def _render_strategy_info(strategy: str) -> None:
    meta = _STRATEGY_META.get(strategy)
    if not meta:
        return
    with st.expander(f"{meta['emoji']} Com funciona: {meta['label']}"):
        col1, col2 = st.columns([3, 1])
        with col1:
            st.caption(meta["tagline"])
            for point in meta["points"]:
                st.markdown(f"- {point}")
        with col2:
            st.markdown(f"**Règim ideal**  \n{meta['regime']}")
            st.markdown(meta["self_selects"])


def _render_strategy_selector(
    bots_for_mode: pd.DataFrame,
    mode: str,
    owner: str,
) -> list[str]:
    """Render the strategy radio for a given mode tab.

    Returns the currently-selected list of strategy keys.
    If the selection changed, updates the DB and reruns.
    """
    current_selection = _infer_strategy_selection(bots_for_mode)

    mode_label = "Paper Trading" if mode == "paper" else "En Viu"
    st.markdown(f"**📊 Estratègia activa — {mode_label}**")

    strategy_label = st.radio(
        "Estratègia",
        options=list(_STRATEGY_OPTIONS.keys()),
        index=list(_STRATEGY_OPTIONS.keys()).index(current_selection),
        horizontal=True,
        label_visibility="collapsed",
        key=f"strategy_{mode}_{owner}",
    )
    active_strategies = _STRATEGY_OPTIONS[strategy_label]

    if len(active_strategies) == 1:
        meta = _STRATEGY_META.get(active_strategies[0], {})
        st.caption(f"Règim: *{meta.get('regime', '')}*")
    else:
        st.caption("Règim: *cobertura completa del mercat*")

    if strategy_label != current_selection:
        _set_owner_mode_strategies(owner, mode, active_strategies)
        st.rerun()

    return active_strategies


def _render_combined_header(bots_subset: pd.DataFrame, kpis: dict[int, dict],
                            initial_total: float, floor: float, mode: str,
                            live_pnls: dict[int, dict] | None = None) -> None:
    if len(bots_subset) < 2:
        return
    ck = _combined_kpis(bots_subset, kpis, initial_total, live_pnls)
    total_pl   = ck["total_eur"] - initial_total
    ret_color  = "normal" if ck["return_pct"] >= 0 else "inverse"
    unrl_color = "normal" if ck["unrealized_pnl_eur"] >= 0 else "inverse"
    rlzd_color = "normal" if ck["realized_pnl_eur"]   >= 0 else "inverse"

    with st.container(border=True):
        st.markdown("#### 🔀 Cartera combinada")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Patrimoni total",   f"€{ck['total_eur']:,.0f}",
                  f"{ck['return_pct']*100:+.2f}%", delta_color=ret_color)
        c2.metric("Capital aportat",   f"€{initial_total:,.0f}")
        c3.metric("Guany / Pèrdua",    f"€{total_pl:+,.0f}")
        c4.metric("Comissions totals", f"€{ck['fees_eur']:,.2f}")

        c5, c6, c7, c8 = st.columns(4)
        unrl_pct = f"{ck['unrealized_pnl_eur'] / initial_total * 100:+.1f}%" if initial_total else "—"
        rlzd_pct = f"{ck['realized_pnl_eur']   / initial_total * 100:+.1f}%" if initial_total else "—"
        c5.metric("No realitzat",     f"€{ck['unrealized_pnl_eur']:+,.2f}",
                  unrl_pct, delta_color=unrl_color)
        c6.metric("Realitzat",        f"€{ck['realized_pnl_eur']:+,.2f}",
                  rlzd_pct, delta_color=rlzd_color)
        c7.metric("Màxima caiguda",   f"{ck['max_dd']*100:.1f}%")
        c8.metric("Total operacions", ck["n_trades"])
        if ck["total_eur"] < floor * len(bots_subset):
            st.error("⚠ Patrimoni combinat per sota del mínim")


def _render_bot_card(bot: pd.Series, kpi: dict, floor: float, mode: str,
                     live_pnl: dict | None = None) -> None:
    meta = _STRATEGY_META.get(bot["strategy"], {})
    emoji  = meta.get("emoji", "🤖")
    label  = meta.get("label", bot["strategy"])
    regime = meta.get("regime", "")
    icon   = _status_color(kpi["total_eur"], floor, mode)
    ret_color = "normal" if kpi["return_pct"] >= 0 else "inverse"

    initial    = float(bot["initial_eur"])
    unrealized = live_pnl["unrealized_pnl_eur"] if live_pnl else 0.0
    total_pl   = kpi["total_eur"] - initial
    realized   = total_pl - unrealized
    unrl_color = "normal" if unrealized >= 0 else "inverse"
    rlzd_color = "normal" if realized   >= 0 else "inverse"

    with st.container(border=True):
        h1, h2 = st.columns([3, 1])
        h1.markdown(f"#### {emoji} {label}")
        h2.markdown(f"<div style='text-align:right;padding-top:8px'>{icon} <b>{mode.upper()}</b></div>",
                    unsafe_allow_html=True)
        st.caption(f"👤 {bot['owner']}  ·  *{regime}*")
        st.divider()

        # Row 1 — portfolio summary
        c1, c2, c3 = st.columns(3)
        c1.metric("Patrimoni", f"€{kpi['total_eur']:,.2f}",
                  f"{kpi['return_pct']*100:+.2f}%", delta_color=ret_color)
        c2.metric("Efectiu",   f"€{kpi['cash_eur']:,.2f}")
        c3.metric("Invertit",  f"€{kpi['invested_eur']:,.2f}")

        # Row 2 — P&L breakdown
        c4, c5, c6 = st.columns(3)
        unrl_pct = f"{unrealized / initial * 100:+.1f}%" if initial else "—"
        rlzd_pct = f"{realized   / initial * 100:+.1f}%" if initial else "—"
        c4.metric("No realitzat", f"€{unrealized:+,.2f}", unrl_pct, delta_color=unrl_color)
        c5.metric("Realitzat",    f"€{realized:+,.2f}",   rlzd_pct, delta_color=rlzd_color)
        c6.metric("Comissions",   f"€{kpi['fees_eur']:,.2f}")

        # Row 3 — risk metrics
        c7, c8, c9 = st.columns(3)
        c7.metric("Sharpe (anual.)",
                  "—" if pd.isna(kpi["sharpe"]) else f"{kpi['sharpe']:.2f}")
        c8.metric("Màxima caiguda", f"{kpi['max_dd']*100:.1f}%")
        c9.metric("Operacions", kpi["n_trades"])

        if kpi["total_eur"] < floor:
            st.error(f"⚠ Per sota del mínim de cartera (€{floor:,.0f})")


def _render_equity_chart(bots_subset: pd.DataFrame, equity_df: pd.DataFrame,
                         floor: float) -> None:
    active_eq = equity_df[equity_df["bot_id"].isin(bots_subset["id"])]
    if active_eq.empty:
        st.info("Encara no hi ha dades d'evolució. Executa `python main.py --once`.")
        return

    date_range = int((active_eq["date"].max() - active_eq["date"].min()).days) if len(active_eq) > 1 else 1
    dtick, tickfmt = _x_axis_dtick(date_range)

    fig = go.Figure()
    colors = ["#3B82F6", "#10B981", "#8B5CF6", "#F59E0B"]

    for i, (_, bot) in enumerate(bots_subset.iterrows()):
        sub = active_eq[active_eq["bot_id"] == bot["id"]].sort_values("date")
        if sub.empty:
            continue
        meta  = _STRATEGY_META.get(bot["strategy"], {})
        color = colors[i % len(colors)]
        fig.add_trace(go.Scatter(
            x=sub["date"], y=sub["total"],
            mode="lines+markers",
            name=f"{meta.get('emoji','🤖')} {meta.get('label', bot['strategy'])}",
            line=dict(width=2.5, color=color),
            marker=dict(size=5),
        ))

    if len(bots_subset) > 1:
        combined = (
            active_eq.groupby("date")["total"]
            .sum()
            .reset_index()
            .sort_values("date")
        )
        fig.add_trace(go.Scatter(
            x=combined["date"], y=combined["total"],
            mode="lines",
            name="🔀 Combinat",
            line=dict(width=2, dash="dash", color="#A78BFA"),
        ))

    fig.add_hline(y=floor, line_dash="dot",
                  annotation_text=f"Mínim €{floor:,.0f}",
                  line_color="rgba(239,68,68,0.6)")
    fig.update_layout(
        height=380,
        yaxis_title="Patrimoni (EUR)",
        margin=dict(t=10, b=20, l=0, r=0),
        legend=dict(orientation="h", yanchor="bottom", y=1.02,
                    xanchor="right", x=1),
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
    )
    fig.update_xaxes(type="date", dtick=dtick, tickformat=tickfmt,
                     gridcolor="rgba(128,128,128,0.15)")
    fig.update_yaxes(gridcolor="rgba(128,128,128,0.15)")
    st.plotly_chart(fig, use_container_width=True)


def _eur_per_usd() -> float:
    """Return EUR per 1 USD (e.g. ~0.853 when EUR/USD = 1.17).

    Uses the same fx module as the bot so IBKR market-price conversion is
    consistent with how trade prices are stored in the database.
    Falls back to 0.88 if yfinance is temporarily unavailable.
    """
    try:
        from core import fx
        return fx.eur_per_unit("USD")
    except Exception:
        return 0.88


def _native_to_eur(value: float, contract_currency: str, rate_eur_per_usd: float) -> float:
    """Convert a value in the account's native currency (USD) to EUR.

    IBKR reports portfolio values in account base currency (USD for paper).
    For EUR-quoted contracts the price is in EUR; for USD-quoted it's in USD.
    We use the contract_currency to know which rate to apply.
    """
    if contract_currency == "EUR":
        return value  # already in EUR
    return value * rate_eur_per_usd  # USD → EUR


def _render_positions(
    bots_subset: pd.DataFrame,
    positions_df: pd.DataFrame,
    floor: float,
    ibkr_portfolio_df: pd.DataFrame | None = None,
) -> None:
    """Render open positions.

    When `ibkr_portfolio_df` is provided (broker=ibkr), IBKR is the source of
    truth for quantities, prices and P&L.  Each position is attributed to a bot
    via the SQLite positions table; positions not found in SQLite are shown as
    "👤 Manual".
    """
    use_ibkr = ibkr_portfolio_df is not None and not ibkr_portfolio_df.empty

    if use_ibkr:
        # ── IBKR-backed positions display ─────────────────────────────────────
        from dashboard.queries import _asset_names
        asset_names = _asset_names()
        rate = _eur_per_usd()
        # Build ticker → bot attribution map from SQLite
        bot_ids_in_scope = set(bots_subset["id"].tolist())
        sqlite_pos = positions_df[positions_df["bot_id"].isin(bot_ids_in_scope)]
        ticker_to_bot: dict[str, pd.Series] = {}
        for _, sp in sqlite_pos.iterrows():
            ticker_to_bot[sp["ticker"]] = sp

        rows = []
        for _, item in ibkr_portfolio_df.iterrows():
            ticker = item["ticker"]
            qty    = item["qty"]
            ccy    = item["contract_currency"]

            # Market value and P&L in EUR
            mkt_val_eur  = _native_to_eur(item["market_value_native"],   ccy, rate)
            unrlz_eur    = _native_to_eur(item["unrealized_pnl_native"],  ccy, rate)
            avg_cost_eur = _native_to_eur(item["avg_cost_native"],        ccy, rate)
            cost_eur     = round(qty * avg_cost_eur, 2)
            pl_pct       = f"{(unrlz_eur / cost_eur * 100):+.1f}%" if cost_eur else "—"

            # Bot attribution
            sp = ticker_to_bot.get(ticker)
            if sp is not None:
                bot_row = bots_subset.loc[bots_subset["id"] == sp["bot_id"]]
                strat   = bot_row["strategy"].values[0] if len(bot_row) else ""
                meta    = _STRATEGY_META.get(strat, {})
                bot_lbl = f"{meta.get('emoji','🤖')} {meta.get('label', strat)}"
                entry_dt = sp.get("data_entrada", "—")
                dies     = (date.today() - entry_dt).days if entry_dt and entry_dt != "—" else "—"
            else:
                bot_lbl  = "👤 Manual"
                entry_dt = "—"
                dies     = "—"

            rows.append({
                "bot":           bot_lbl,
                "ticker":        ticker,
                "nom":           asset_names.get(ticker, ticker),
                "qty":           qty,
                "data entrada":  entry_dt,
                "dies":          dies,
                "preu entrada":  f"€{avg_cost_eur:,.2f}",
                "preu actual":   f"€{(_native_to_eur(item['market_price_native'], ccy, rate)):,.2f}",
                "cost":          f"€{cost_eur:,.2f}",
                "valor actual":  f"€{mkt_val_eur:,.2f}",
                "P&L €":         f"€{unrlz_eur:+,.2f}",
                "P&L %":         pl_pct,
            })

        if not rows:
            st.caption("Cap posició oberta a l'account IBKR.")
            return
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    else:
        # ── SQLite-backed positions display (mock broker or IBKR offline) ─────
        active = (
            positions_df[positions_df["bot_id"].isin(bots_subset["id"])].copy()
            if not positions_df.empty else positions_df
        )
        if active.empty:
            st.caption("Cap posició oberta.")
            return

        open_tickers = tuple(active["ticker"].unique())
        live_prices  = _fetch_prices_eur(open_tickers)
        today_date   = date.today()
        rows = []
        for _, p in active.iterrows():
            px        = live_prices.get(p["ticker"])
            cost      = p["cost_eur"]
            valor     = round(px * p["quantitat"], 2) if px else None
            pl_eur    = round(valor - cost, 2) if valor else None
            guany_pct = f"{(valor / cost - 1) * 100:+.1f}%" if valor and cost > 0 else "—"
            dies      = (today_date - p["data_entrada"]).days if p["data_entrada"] else "—"
            bot_row   = bots_subset.loc[bots_subset["id"] == p["bot_id"]]
            owner     = bots_subset.loc[bots_subset["id"] == p["bot_id"], "owner"].values
            strat     = bot_row["strategy"].values[0] if len(bot_row) else ""
            meta      = _STRATEGY_META.get(strat, {})
            rows.append({
                "bot":          f"{meta.get('emoji','🤖')} {meta.get('label', strat)}",
                "compte":       owner[0] if len(owner) else f"Bot {p['bot_id']}",
                "nom":          p["nom"],
                "ticker":       p["ticker"],
                "data entrada": p["data_entrada"],
                "dies":         dies,
                "preu entrada": f"€{p['preu_entrada_eur']:,.2f}",
                "preu actual":  f"€{px:,.2f}" if px else "—",
                "cost":         f"€{cost:,.2f}",
                "valor actual": f"€{valor:,.2f}" if valor else "—",
                "guany %":      guany_pct,
                "P&L €":        f"€{pl_eur:+,.2f}" if pl_eur is not None else "—",
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


def _render_risk_and_trades(
    bots_subset: pd.DataFrame,
    trades_df: pd.DataFrame,
    ibkr_executions_df: pd.DataFrame | None = None,
) -> None:
    left, right = st.columns(2)
    with left:
        st.markdown("**⚠️ Límits de risc**")
        today = date.today()
        with get_session() as s:
            rows = []
            for _, bot in bots_subset.iterrows():
                placed = Portfolio.trades_today(s, int(bot["id"]), today)
                meta   = _STRATEGY_META.get(bot["strategy"], {})
                rows.append({
                    "estratègia":      f"{meta.get('emoji','🤖')} {meta.get('label', bot['strategy'])}",
                    "operacions avui": placed,
                    "límit diari":     CONFIG.settings["guardrails"]["max_trades_per_day"],
                })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    with right:
        # ── Pending orders banner (always shown, regardless of IBKR connection) ─
        active_trades = (
            trades_df[trades_df["bot_id"].isin(bots_subset["id"])]
            if not trades_df.empty else trades_df
        )
        if not active_trades.empty and "estat" in active_trades.columns:
            pending = active_trades[active_trades["estat"] == "pending"]
            if not pending.empty:
                lines = "  \n".join(
                    f"• **{r['ticker']}** {int(r['quantitat'])} accions"
                    f" — est. €{r['total_eur']:,.2f}"
                    for _, r in pending.iterrows()
                )
                st.warning(
                    f"⏳ **{len(pending)} ordre(s) pendent(s) a IBKR** "
                    "(s'executarà quan el mercat obri — reconciliació automàtica demà)\n\n"
                    + lines
                )

        use_ibkr = ibkr_executions_df is not None and not ibkr_executions_df.empty
        if use_ibkr:
            st.markdown("**📋 Operacions IBKR (comissions reals)**")
            rate = _eur_per_usd()
            display = ibkr_executions_df.copy()
            # Convert commission to EUR where currency is USD
            def _fmt_comm(row: pd.Series) -> str:
                if row["commission"] is None:
                    return "—"
                ccy = row.get("comm_currency", "USD")
                val = row["commission"] * rate if ccy == "USD" else row["commission"]
                return f"€{val:.2f}"

            display["comissió €"] = display.apply(_fmt_comm, axis=1)
            display["P&L tancat"] = display["realized_pnl"].apply(
                lambda v: f"€{v * rate:+.2f}" if v is not None else "—"
            )
            show_cols = ["time", "ticker", "side", "qty", "price", "comissió €", "P&L tancat"]
            st.dataframe(display[show_cols].rename(columns={
                "time": "hora", "side": "operació", "qty": "quantitat", "price": "preu"
            }), use_container_width=True, hide_index=True)
        else:
            st.markdown("**📋 Registre d'operacions**")
            if active_trades.empty:
                st.caption("Encara no hi ha operacions.")
            else:
                display = active_trades.drop(columns=["bot_id"], errors="ignore").copy()
                if "estat" in display.columns:
                    display["estat"] = display["estat"].map(
                        {"filled": "✅ executat", "pending": "⏳ pendent", "cancelled": "❌ cancel·lat"}
                    ).fillna("⚠️ desconegut")
                st.dataframe(display, use_container_width=True, hide_index=True)


def _render_run_logs(bots_subset: pd.DataFrame) -> None:
    run_logs_df = _run_logs()
    active = (
        run_logs_df[run_logs_df["bot_id"].isin(bots_subset["id"])]
        if not run_logs_df.empty else run_logs_df
    )
    if active.empty:
        st.info("Encara no hi ha execucions registrades.")
        return

    display_cols = [c for c in active.columns if c not in ("bot_id", "explicació", "tipus")]
    display_cols.insert(display_cols.index("data_execució") + 1, "tipus")
    st.dataframe(active[display_cols], use_container_width=True, hide_index=True)

    explanations = active[active["explicació"].str.len() > 0]
    if not explanations.empty:
        st.markdown("**🤖 Explicacions d'operacions (IA)**")
        for _, row in explanations.iterrows():
            label = (
                f"{row['data_mercat']} — {row['bot']} "
                f"({row['compres']} compres, {row['vendes']} vendes)"
            )
            with st.expander(label):
                st.markdown(row["explicació"])


def _render_reconciliation(bots_subset: pd.DataFrame, mode: str) -> None:
    if CONFIG.broker_backend != "ibkr":
        return
    port_key = "ibkr_port_paper" if mode == "paper" else "ibkr_port"
    ports: set[int] = set()
    for _, bot in bots_subset.iterrows():
        p = bot.get(port_key)
        if p and not pd.isna(p):
            ports.add(int(p))
    if not ports:
        return

    with st.expander("🔍 Reconciliació SQLite ↔ IBKR"):
        bot_ids = tuple(int(i) for i in bots_subset["id"])
        for port in ports:
            st.caption(f"Port IBKR: {port}")
            discrepancies = _reconcile_cached(bot_ids, port)
            if not discrepancies:
                st.success("✅ Tot correcte — SQLite i IBKR coincideixen.")
            else:
                has_untracked = any(
                    d.get("ticker") not in ("IBKR_UNREACHABLE",) and d["ibkr_qty"] > 0 and d["sqlite_qty"] == 0
                    for d in discrepancies
                )
                for d in discrepancies:
                    if d.get("ticker") == "IBKR_UNREACHABLE":
                        st.warning("⚡ Gateway IBKR no disponible ara mateix.")
                    elif d["severity"] == "ERROR":
                        label = "👤 manual" if d["sqlite_qty"] == 0 else "desajust"
                        st.error(
                            f"❌ **{d['ticker']}** ({label}): SQLite={d['sqlite_qty']:.2f}  "
                            f"IBKR={d['ibkr_qty']:.2f}  (diff={d['diff']:+.2f})"
                        )
                    else:
                        st.warning(
                            f"⚠️ **{d['ticker']}**: SQLite={d['sqlite_qty']:.4f}  "
                            f"IBKR={d['ibkr_qty']:.4f}  (diff={d['diff']:+.4f})"
                        )

                if has_untracked:
                    st.caption(
                        "Les posicions 'manual' existeixen a IBKR però no al SQLite. "
                        "Prem el botó per importar-les automàticament."
                    )
                    if st.button("📥 Importar posicions manuals ara", key=f"import_manual_{port}"):
                        primary = int(bots_subset["id"].iloc[0]) if not bots_subset.empty else None
                        try:
                            from agents.reconciliation import import_manual_positions
                            n = import_manual_positions(list(bot_ids), port, primary_bot_id=primary)
                            if n:
                                st.success(f"✅ {n} posició(ns) importada(es) correctament.")
                                _reconcile_cached.clear()
                                _open_positions.clear()
                                _trades.clear()
                                st.rerun()
                            else:
                                st.info("Cap posició nova per importar.")
                        except Exception as exc:
                            st.error(f"Error en importar: {exc}")


def _get_ibkr_port(bots_subset: pd.DataFrame, mode: str) -> int | None:
    """Return the first valid IBKR port for this mode's bots."""
    port_key = "ibkr_port_paper" if mode == "paper" else "ibkr_port"
    for _, bot in bots_subset.iterrows():
        p = bot.get(port_key)
        if p and not pd.isna(p):
            return int(p)
    return None


def _render_tab(bots_subset: pd.DataFrame, mode: str, equity_df: pd.DataFrame,
                positions_df: pd.DataFrame, trades_df: pd.DataFrame,
                floor: float) -> None:
    """Render the main content of a Paper or Live tab (below the strategy selector)."""
    if bots_subset.empty:
        if mode == "live":
            st.info(
                "El trading en viu està **inactiu**.  \n"
                "Activa'l amb el botó de dalt ↑"
            )
        else:
            st.info("Cap bot de paper actiu per a aquest compte.")
        return

    # ── Fetch IBKR live data (once per tab render) ────────────────────────────
    use_ibkr = CONFIG.broker_backend == "ibkr"
    ibkr_port = _get_ibkr_port(bots_subset, mode) if use_ibkr else None
    ibkr_portfolio  = _ibkr_portfolio(ibkr_port)  if ibkr_port else pd.DataFrame()
    ibkr_executions = _ibkr_executions(ibkr_port) if ibkr_port else pd.DataFrame()

    # ── Strategy info expanders ───────────────────────────────────────────────
    seen: set[str] = set()
    for _, bot in bots_subset.iterrows():
        if bot["strategy"] not in seen:
            _render_strategy_info(bot["strategy"])
            seen.add(bot["strategy"])

    st.divider()

    # ── Compute KPIs ─────────────────────────────────────────────────────────
    n_active = len(bots_subset)
    kpis: dict[int, dict] = {}
    for _, bot in bots_subset.iterrows():
        kpis[int(bot["id"])] = _kpi_with_ibkr(
            bot, equity_df, trades_df, mode,
            ibkr_portfolio_df=ibkr_portfolio if use_ibkr else None,
            n_active_bots=n_active,
        )

    # ── Live P&L from current market prices (yfinance) ───────────────────────
    live_pnls = _compute_live_pnl_per_bot(bots_subset, positions_df)

    # In mock mode the KPI total comes from stale equity snapshots; replace it
    # with cash (always current) + live market value of open positions so the
    # card matches what the positions table shows.
    if not use_ibkr:
        for _, bot in bots_subset.iterrows():
            bot_id = int(bot["id"])
            lpnl = live_pnls.get(bot_id, {})
            live_invested = lpnl.get("live_invested_eur", kpis[bot_id]["invested_eur"])
            live_total    = kpis[bot_id]["cash_eur"] + live_invested
            initial       = float(bot["initial_eur"])
            kpis[bot_id]["invested_eur"] = live_invested
            kpis[bot_id]["total_eur"]    = live_total
            kpis[bot_id]["return_pct"]   = (live_total / initial - 1.0) if initial else 0.0

    initial_total = sum(float(bot["initial_eur"]) for _, bot in bots_subset.iterrows())

    _render_combined_header(bots_subset, kpis, initial_total, floor, mode, live_pnls)

    if len(bots_subset) > 1:
        st.markdown("")

    # ── Individual bot cards ─────────────────────────────────────────────────
    cols = st.columns(len(bots_subset))
    for col, (_, bot) in zip(cols, bots_subset.iterrows()):
        with col:
            bot_id = int(bot["id"])
            _render_bot_card(bot, kpis[bot_id], floor, mode, live_pnls.get(bot_id))

    st.divider()

    st.markdown("#### 📈 Evolució del patrimoni")
    _render_equity_chart(bots_subset, equity_df, floor)

    st.divider()

    st.markdown("#### 📂 Posicions obertes")
    if use_ibkr and not ibkr_portfolio.empty:
        st.caption("Font: IBKR Gateway (temps real) · Posicions del bot i manuals.")
    elif use_ibkr:
        st.caption("⚠️ IBKR Gateway no disponible — mostrant SQLite.")
    _render_positions(
        bots_subset, positions_df, floor,
        ibkr_portfolio_df=ibkr_portfolio if use_ibkr else None,
    )

    # ── Closed positions ──────────────────────────────────────────────────────
    closed_df = _closed_positions()
    bot_ids_in_scope = set(bots_subset["id"].tolist())
    closed_subset = (
        closed_df[closed_df["bot_id"].isin(bot_ids_in_scope)]
        if not closed_df.empty else closed_df
    )
    display_cols = [c for c in closed_subset.columns if c != "bot_id"]

    with st.expander(
        f"📁 Posicions tancades ({len(closed_subset)} operacions)",
        expanded=False,
    ):
        if closed_subset.empty:
            st.caption("Encara no hi ha posicions tancades.")
        else:
            # Summary line: total realised P&L
            total_pl_vals = []
            for _, r in closed_subset.iterrows():
                try:
                    total_pl_vals.append(float(str(r["P&L €"]).replace("€", "").replace(",", "").replace("+", "")))
                except ValueError:
                    pass
            total_pl = sum(total_pl_vals)
            colour = "green" if total_pl >= 0 else "red"
            st.markdown(
                f"P&L realitzat total: "
                f"<span style='color:{colour};font-weight:700'>€{total_pl:+,.2f}</span>",
                unsafe_allow_html=True,
            )
            st.dataframe(closed_subset[display_cols], use_container_width=True, hide_index=True)

    st.divider()

    _render_risk_and_trades(
        bots_subset, trades_df,
        ibkr_executions_df=ibkr_executions if use_ibkr else None,
    )

    st.divider()

    st.markdown("#### 📡 Registre d'execucions")
    st.caption("Una entrada per bot per execució, incloent quan no s'ha executat cap operació.")
    _render_run_logs(bots_subset)

    _render_reconciliation(bots_subset, mode)


# ── Load data ──────────────────────────────────────────────────────────────────
bots_df      = _load_bots()
equity_df    = _equity_history()
positions_df = _open_positions()
trades_df    = _trades()
floor        = CONFIG.settings["guardrails"]["portfolio_floor_eur"]

owners = sorted(o for o in bots_df["owner"].unique() if o and not o.startswith("Bot "))

# ── Sidebar ────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("⚙️ Configuració")
    st.divider()

    # ── Owner selector ────────────────────────────────────────────────────────
    selected_owner = st.selectbox(
        "👤 Compte",
        options=owners,
        format_func=lambda o: f"👤 {o}",
    )

    owner_bots = bots_df[bots_df["owner"] == selected_owner]

    st.divider()

    # ── Bot status panel (read-only) ──────────────────────────────────────────
    st.markdown("**🤖 Estat dels bots**")
    for _, bot in owner_bots.iterrows():
        meta        = _STRATEGY_META.get(bot["strategy"], {})
        emoji       = meta.get("emoji", "🤖")
        label       = meta.get("label", bot["strategy"])
        mode_icon   = "💶" if bot["trading_mode"] == "live" else "🧪"
        active_icon = "🟢" if bot["enabled"] else "⚫"
        st.caption(f"{active_icon} {emoji} {label} {mode_icon}")

    st.divider()
    st.caption(
        f"Backend: `{CONFIG.broker_backend}` · "
        f"BD: `{CONFIG.db_path.name}`"
    )

# ── Filter to selected owner ───────────────────────────────────────────────────
owner_all_bots = bots_df[bots_df["owner"] == selected_owner].copy()
paper_all      = owner_all_bots[owner_all_bots["trading_mode"] == "paper"]
live_all       = owner_all_bots[owner_all_bots["trading_mode"] == "live"]

# ── Page header ────────────────────────────────────────────────────────────────
live_is_on_global = not live_all[live_all["enabled"]].empty
live_badge = "💶 EN VIU" if live_is_on_global else "🧪 Paper"
st.title(f"📈 Trading Bot — {selected_owner}")
st.caption(
    f"{live_badge}  ·  Moneda: **{CONFIG.settings['base_currency']}**"
)

if owner_all_bots.empty:
    st.warning("Cap bot actiu per a aquest compte. Executa `python main.py --init-db` primer.")
    st.stop()

# ── Tabs ───────────────────────────────────────────────────────────────────────
n_paper = int(paper_all["enabled"].sum())
n_live  = int(live_all["enabled"].sum())

tab_readme, tab_paper, tab_live, tab_bt = st.tabs([
    "📖 Guia",
    f"🧪 Paper Trading ({n_paper} bot{'s' if n_paper != 1 else ''})",
    f"💶 En Viu ({n_live} bot{'s' if n_live != 1 else ''})",
    "📊 Backtest",
])

# ── Paper tab ──────────────────────────────────────────────────────────────────
with tab_paper:
    # Strategy selector — controls which paper bots are enabled
    _render_strategy_selector(paper_all, "paper", selected_owner)

    st.divider()

    paper_bots = paper_all[paper_all["enabled"]].copy()
    _render_tab(paper_bots, "paper", equity_df, positions_df, trades_df, floor)

# ── Live tab ───────────────────────────────────────────────────────────────────
with tab_live:
    # Strategy selector + live toggle — both scoped to live bots only
    sel_col, toggle_col = st.columns([3, 1])

    with sel_col:
        live_active_strategies = _render_strategy_selector(
            live_all, "live", selected_owner
        )

    with toggle_col:
        live_enabled_bots = live_all[live_all["enabled"]]
        live_is_on        = not live_enabled_bots.empty

        st.markdown("**💶 En viu**")
        new_live = st.toggle(
            "Activar trading en viu",
            value=live_is_on,
            key=f"live_toggle_{selected_owner}",
            label_visibility="collapsed",
        )
        if new_live:
            st.success("ACTIU")
        else:
            st.info("INACTIU")

        if new_live != live_is_on:
            _set_owner_live_enabled(selected_owner, new_live, live_active_strategies)
            st.rerun()

    st.divider()

    live_bots = live_all[live_all["enabled"]].copy()
    _render_tab(live_bots, "live", equity_df, positions_df, trades_df, floor)

# ── Backtest tab ───────────────────────────────────────────────────────────────
with tab_bt:
    all_owner_bots = pd.concat([paper_all, live_all], ignore_index=True)
    render_backtest_tab(
        all_owner_bots if not all_owner_bots.empty else owner_all_bots,
        floor,
    )

# ── Guia tab ───────────────────────────────────────────────────────────────────
with tab_readme:
    render_readme_tab()
