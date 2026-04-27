"""KPI math for live dashboard and backtest position breakdown."""
from __future__ import annotations

import pandas as pd

_TICKER_NAMES: dict[str, str] = {
    # US stocks
    "AAPL":    "Apple",
    "MSFT":    "Microsoft",
    "GOOGL":   "Alphabet (Google)",
    "AMZN":    "Amazon",
    "META":    "Meta Platforms",
    "NVDA":    "NVIDIA",
    "JPM":     "JPMorgan Chase",
    "V":       "Visa",
    "UNH":     "UnitedHealth",
    "HD":      "Home Depot",
    "DIS":     "Walt Disney",
    "NKE":     "Nike",
    "BAC":     "Bank of America",
    "GS":      "Goldman Sachs",
    "JNJ":     "Johnson & Johnson",
    "PFE":     "Pfizer",
    "KO":      "Coca-Cola",
    "PG":      "Procter & Gamble",
    "WMT":     "Walmart",
    "XOM":     "ExxonMobil",
    "CVX":     "Chevron",
    "CAT":     "Caterpillar",
    "HON":     "Honeywell",
    "TSLA":    "Tesla",
    "AMD":     "AMD",
    "PLTR":    "Palantir",
    # EU stocks
    "MC.PA":   "LVMH",
    "AIR.PA":  "Airbus",
    "TTE.PA":  "TotalEnergies",
    "BNP.PA":  "BNP Paribas",
    "SIE.DE":  "Siemens",
    "BMW.DE":  "BMW",
    "ALV.DE":  "Allianz",
    "BAYN.DE": "Bayer",
    "SAP.DE":  "SAP",
    "ASML.AS": "ASML",
    "NESN.SW": "Nestlé",
    "NOVN.SW": "Novartis",
    # ETFs / ETPs
    "SXR8.DE": "iShares S&P 500 (Acc)",
    "SXRV.DE": "iShares NASDAQ 100 (Acc)",
    "EXSA.DE": "iShares Euro Stoxx 50",
    "XDWD.DE": "Xtrackers MSCI World",
    "QDVE.DE": "iShares S&P 500 IT Sector",
    "QDVH.DE": "iShares S&P 500 Health Care",
    "ZPRR.DE": "SPDR Russell 2000",
    "BTCE.DE": "ETC Bitcoin",
    "ZETH.DE": "ETC Ethereum",
}


def ticker_name(ticker: str) -> str:
    """Return a human-readable name for a ticker.

    Priority: contracts.json long_name (via _asset_names) → _TICKER_NAMES → ticker.
    _asset_names uses @st.cache_data so it's fast after the first call.
    """
    try:
        from dashboard.queries import _asset_names
        names = _asset_names()
        if ticker in names and names[ticker] != ticker:
            return names[ticker]
    except Exception:
        pass
    return _TICKER_NAMES.get(ticker, ticker)


def _analyze_bt_positions(
    trades_df: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split backtest trades into open and closed position summaries.

    Returns (open_df, closed_df). open_df has placeholder current-price columns
    that the caller fills in by fetching live prices.
    """
    if trades_df.empty:
        empty_open = pd.DataFrame(columns=[
            "ticker", "nom", "data_entrada", "cost_eur",
            "valor_actual_eur", "guany_pct", "p_l_no_realitzat_eur",
        ])
        empty_closed = pd.DataFrame(columns=[
            "ticker", "nom", "data_entrada", "data_sortida", "dies",
            "cost_eur", "valor_sortida_eur", "guany_pct",
            "p_l_realitzat_eur", "motiu_sortida",
        ])
        return empty_open, empty_closed

    buys = trades_df[trades_df["side"] == "BUY"].copy()
    sells = trades_df[trades_df["side"] == "SELL"].copy()

    open_rows: list[dict] = []
    closed_rows: list[dict] = []

    for ticker in buys["ticker"].unique():
        t_buys = buys[buys["ticker"] == ticker].sort_values("date").to_dict("records")
        t_sells = sells[sells["ticker"] == ticker].sort_values("date").to_dict("records")

        sell_queue = list(t_sells)
        for buy in t_buys:
            buy_qty = buy["qty"]
            buy_price = buy["price_eur"]
            buy_date = buy["date"]

            remaining = buy_qty
            while sell_queue and remaining > 1e-6:
                sell = sell_queue[0]
                matched = min(remaining, sell["qty"])
                sell["qty"] -= matched
                if sell["qty"] < 1e-6:
                    sell_queue.pop(0)
                remaining -= matched

                sell_price = sell["price_eur"]
                gain_pct = sell_price / buy_price - 1.0
                pnl = (sell_price - buy_price) * matched
                sell_date = sell["date"]
                if hasattr(sell_date, "date"):
                    sell_date_d = sell_date.date()
                else:
                    sell_date_d = sell_date
                if hasattr(buy_date, "date"):
                    buy_date_d = buy_date.date()
                else:
                    buy_date_d = buy_date
                days = (sell_date_d - buy_date_d).days if hasattr(sell_date_d, '__sub__') else "—"
                closed_rows.append({
                    "ticker": ticker,
                    "nom": ticker_name(ticker),
                    "data_entrada": buy_date_d,
                    "data_sortida": sell_date_d,
                    "dies": days,
                    "accions": round(matched, 2),
                    "cost_eur": round(buy_price * matched, 2),
                    "valor_sortida_eur": round(sell_price * matched, 2),
                    "guany_pct": f"{gain_pct*100:+.2f}%",
                    "p_l_realitzat_eur": round(pnl, 2),
                    "motiu_sortida": sell.get("signal_reason", ""),
                })

            if remaining > 1e-6:
                if hasattr(buy_date, "date"):
                    buy_date_d = buy_date.date()
                else:
                    buy_date_d = buy_date
                open_rows.append({
                    "ticker": ticker,
                    "nom": ticker_name(ticker),
                    "data_entrada": buy_date_d,
                    "_qty": remaining,
                    "accions": round(remaining, 2),
                    "_preu_entrada_eur": buy_price,
                    "cost_eur": round(buy_price * remaining, 2),
                    "valor_actual_eur": float("nan"),
                    "guany_pct": "—",
                    "p_l_no_realitzat_eur": float("nan"),
                })

    open_df = pd.DataFrame(open_rows) if open_rows else pd.DataFrame(columns=[
        "ticker", "data_entrada", "_qty", "_preu_entrada_eur",
        "cost_eur", "valor_actual_eur", "guany_pct", "p_l_no_realitzat_eur",
    ])
    closed_df = pd.DataFrame(closed_rows) if closed_rows else pd.DataFrame(columns=[
        "ticker", "data_entrada", "data_sortida", "dies",
        "cost_eur", "valor_sortida_eur", "guany_pct",
        "p_l_realitzat_eur", "motiu_sortida",
    ])
    return open_df, closed_df


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
