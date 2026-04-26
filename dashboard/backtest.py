"""Backtest tab UI for the Streamlit dashboard."""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from analysis.market_regime import REGIME_LABELS, compute_regimes, regime_spans
from backtesting.engine import run_backtest

from dashboard.kpis import _analyze_bt_positions
from dashboard.queries import _fetch_prices_eur


def _x_axis_dtick(date_range_days: int) -> tuple[str, str]:
    if date_range_days <= 30:
        return "D1", "%d %b"
    if date_range_days <= 120:
        return "D7", "%d %b"
    if date_range_days <= 400:
        return "M1", "%b %Y"
    return "M3", "%b %Y"


def render_backtest_tab(bots_df: pd.DataFrame, floor: float) -> None:
    st.subheader("Backtest — simulació amb dades històriques reals")
    st.caption(
        "Simula cada bot dia a dia sobre un rang de dates passat. "
        "Les dades de preu s'obtenen de yfinance i es tallen al tancament de cada dia simulat. "
        "La BD en viu no es modifica."
    )

    bt_col1, bt_col2, bt_col3, bt_col4 = st.columns([2, 1, 1, 1])
    with bt_col1:
        bt_bot_options = {
            f"Bot {row['id']} — {row['name']}": row["id"]
            for _, row in bots_df[bots_df["enabled"]].iterrows()
        }
        selected_labels = st.multiselect(
            "Bots a simular",
            options=list(bt_bot_options.keys()),
            default=list(bt_bot_options.keys()),
        )
        selected_bot_ids = [bt_bot_options[lbl] for lbl in selected_labels]

    today_bt = date.today()
    default_start = today_bt - timedelta(days=90)
    with bt_col2:
        bt_start = st.date_input("Data inici", value=default_start, max_value=today_bt)
    with bt_col3:
        bt_end = st.date_input("Data fi", value=today_bt, max_value=today_bt)
    with bt_col4:
        show_regimes = st.toggle("Mostrar règims", value=True, help="Ressalta els règims de mercat al gràfic d'evolució del patrimoni")

    run_bt = st.button("Executar backtest", type="primary", disabled=not selected_bot_ids)

    if run_bt:
        st.session_state["bt_results"] = {}
        for bot_id in selected_bot_ids:
            bot_label = next(k for k, v in bt_bot_options.items() if v == bot_id)
            with st.spinner(f"Simulant {bot_label}…"):
                try:
                    result = run_backtest(bot_id, bt_start, bt_end)
                    st.session_state["bt_results"][bot_id] = result
                except Exception as exc:
                    st.error(f"{bot_label}: error durant el backtest — {exc}")

    bt_results: dict = st.session_state.get("bt_results", {})
    if bt_results:
        st.divider()

        for bot_id, res in bt_results.items():
            st.subheader(f"Bot {res.bot_id} — {res.bot_name}")

            # ── Pre-compute position data needed for overview ──────────────
            open_df, closed_df = _analyze_bt_positions(res.trades_df)

            if not open_df.empty:
                open_tickers = tuple(open_df["ticker"].unique())
                live_prices = _fetch_prices_eur(open_tickers)
                open_df["valor_actual_eur"] = open_df.apply(
                    lambda r: round(live_prices.get(r["ticker"], float("nan")) * r["_qty"], 2),
                    axis=1,
                )
                open_df["guany_pct"] = open_df.apply(
                    lambda r: (
                        f"{(r['valor_actual_eur'] / r['cost_eur'] - 1) * 100:+.2f}%"
                        if pd.notna(r["valor_actual_eur"]) and r["cost_eur"] > 0
                        else "—"
                    ),
                    axis=1,
                )
                open_df["p_l_no_realitzat_eur"] = open_df.apply(
                    lambda r: round(r["valor_actual_eur"] - r["cost_eur"], 2)
                    if pd.notna(r["valor_actual_eur"]) else float("nan"),
                    axis=1,
                )

            fees = res.trades_df["fee_eur"].sum() if not res.trades_df.empty else 0.0
            realized_pnl = closed_df["p_l_realitzat_eur"].sum() if not closed_df.empty else 0.0
            unrealized_pnl = open_df["p_l_no_realitzat_eur"].sum(skipna=True) if not open_df.empty else 0.0
            total_return_abs = realized_pnl + unrealized_pnl - fees
            total_return_pct = total_return_abs / res.initial_capital_eur if res.initial_capital_eur else 0.0
            n_trades = len(res.trades_df)

            last_eq = res.equity_df.iloc[-1] if not res.equity_df.empty else None
            invested_eur = float(last_eq["positions_value_eur"]) if last_eq is not None else 0.0
            cash_eur = float(last_eq["cash_eur"]) if last_eq is not None else res.initial_capital_eur

            # ── Overview KPIs ──────────────────────────────────────────────
            st.markdown("#### Resum")
            r1c1, r1c2, r1c3 = st.columns(3)
            r1c1.metric(
                "Rendiment total (%)",
                f"{total_return_pct * 100:+.2f}%",
            )
            r1c2.metric(
                "Rendiment total (€)",
                f"€{total_return_abs:+,.2f}",
            )
            r1c3.metric(
                "Màxima caiguda",
                f"{res.max_drawdown * 100:.2f}%",
            )

            r2c1, r2c2, r2c3, r2c4 = st.columns(4)
            r2c1.metric("Invertit", f"€{invested_eur:,.2f}")
            r2c2.metric("Efectiu", f"€{cash_eur:,.2f}")
            r2c3.metric("Comissions totals", f"€{fees:,.2f}")
            r2c4.metric("Operacions totals", n_trades)

            if res.errors:
                with st.expander(f"{len(res.errors)} avisos"):
                    for e in res.errors:
                        st.caption(e)

            st.divider()

            # ── Equity curve ───────────────────────────────────────────────
            st.markdown("#### Evolució del patrimoni simulat")
            if not res.equity_df.empty:
                all_dates = res.equity_df["date"].tolist()
                date_range = int((max(all_dates) - min(all_dates)).days) if len(all_dates) > 1 else 1
                dtick, tickfmt = _x_axis_dtick(date_range)
                fig_bt = go.Figure()

                # ── Regime background bands ────────────────────────────────
                if show_regimes:
                    regime_ticker = "SXR8.DE"
                    regime_df = compute_regimes(regime_ticker, bt_start, bt_end)
                    spans = regime_spans(regime_df)
                    added_labels: set[str] = set()
                    for span in spans:
                        label = span["label"]
                        show_legend = label not in added_labels
                        fig_bt.add_vrect(
                            x0=str(span["x0"]),
                            x1=str(span["x1"]),
                            fillcolor=span["color"],
                            layer="below",
                            line_width=0,
                            annotation_text=span["regime"] if date_range <= 180 else "",
                            annotation_position="top left",
                            annotation_font_size=9,
                            legendgroup=label,
                            showlegend=show_legend,
                            name=label,
                        )
                        added_labels.add(label)

                fig_bt.add_trace(go.Scatter(
                    x=res.equity_df["date"],
                    y=res.equity_df["total_eur"],
                    mode="lines+markers",
                    name=res.bot_name,
                    line=dict(width=2),
                ))
                fig_bt.add_hline(y=floor, line_dash="dot", annotation_text=f"Mínim €{floor}")
                fig_bt.update_layout(
                    height=420,
                    yaxis_title="Patrimoni simulat (EUR)",
                    margin=dict(t=20, b=20),
                    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
                )
                fig_bt.update_xaxes(type="date", dtick=dtick, tickformat=tickfmt)
                st.plotly_chart(fig_bt, use_container_width=True)

                # Legend caption
                if show_regimes:
                    legend_parts = [f"{v}" for v in REGIME_LABELS.values()]
                    st.caption("Règims: " + "  ·  ".join(legend_parts))

            st.divider()

            # ── Positions ──────────────────────────────────────────────────
            pos_col1, pos_col2 = st.columns(2)

            with pos_col1:
                st.markdown("**Posicions obertes** (al final del backtest)")
                if open_df.empty:
                    st.caption("Cap posició oberta al final del període.")
                else:
                    total_invested_open = open_df["cost_eur"].sum()
                    open_df["pes_%"] = (
                        open_df["cost_eur"] / total_invested_open * 100
                        if total_invested_open > 0 else 0.0
                    ).round(1)
                    total_unrealized = open_df["p_l_no_realitzat_eur"].sum(skipna=True)
                    st.metric("P&L no realitzat total", f"€{total_unrealized:+,.2f}")
                    display_open = open_df.drop(columns=["_qty", "_preu_entrada_eur"]).sort_values("data_entrada")
                    # reorder columns for clarity
                    cols_open = ["ticker", "nom", "data_entrada", "accions", "pes_%",
                                 "cost_eur", "valor_actual_eur", "guany_pct", "p_l_no_realitzat_eur"]
                    display_open = display_open[[c for c in cols_open if c in display_open.columns]]
                    st.dataframe(display_open, use_container_width=True, hide_index=True)

            with pos_col2:
                st.markdown("**Posicions tancades**")
                if closed_df.empty:
                    st.caption("Cap posició tancada durant el període.")
                else:
                    winners = (closed_df["p_l_realitzat_eur"] > 0).sum()
                    win_rate = winners / len(closed_df) * 100
                    m1, m2, m3 = st.columns(3)
                    m1.metric("P&L realitzat", f"€{realized_pnl:+,.2f}")
                    m2.metric("Taxa encert", f"{win_rate:.0f}%")
                    m3.metric("Operacions tancades", len(closed_df))
                    cols_closed = ["ticker", "nom", "data_entrada", "data_sortida", "dies",
                                   "accions", "cost_eur", "valor_sortida_eur",
                                   "guany_pct", "p_l_realitzat_eur", "motiu_sortida"]
                    display_closed = closed_df[[c for c in cols_closed if c in closed_df.columns]].sort_values("data_entrada")
                    st.dataframe(display_closed, use_container_width=True, hide_index=True)

            st.divider()

            # ── Trade log ──────────────────────────────────────────────────
            with st.expander(f"Registre d'operacions simulades ({n_trades} operacions)"):
                if res.trades_df.empty:
                    st.caption("Cap operació durant el període.")
                else:
                    st.dataframe(res.trades_df, use_container_width=True, hide_index=True)
