"""RSI entry cap comparison: current (40-65) vs capped (40-60).

Scans the past 2 weeks (Apr 9 – Apr 23, 2026) day-by-day for entry signals
under two conditions and reports which trades get blocked and forward returns.

Run: .venv/Scripts/python.exe -m analysis.rsi_cap_comparison
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from analysis import market_data
from analysis.price_signals import rsi as compute_rsi
from core.config import CONFIG

RSI_PERIOD      = 14
RSI_WAS_BELOW   = 25.0
RSI_NOW_ABOVE   = 40.0
RSI_CAP_CURRENT = 65.0   # Bots 6/7 cap; Bots 4/5 have no cap (use 100 as proxy)
RSI_CAP_NEW     = 60.0
RSI_LOOKBACK    = 15
MKT_TICKER      = "SXR8.DE"
MKT_RSI_BELOW   = 30.0
MKT_LOOKBACK    = 15

SCAN_START = date(2026, 4, 9)
SCAN_END   = date(2026, 4, 23)
DATA_START_DAYS = 80  # enough history for RSI


def trading_days(start: date, end: date) -> list[date]:
    days, d = [], start
    while d <= end:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def rsi_min_window(rsi_series: pd.Series, as_of_pos: int, lookback: int) -> float:
    window = rsi_series.iloc[max(0, as_of_pos - lookback): as_of_pos]
    return float(window.min()) if not window.empty else float("nan")


def forward_return(close: pd.Series, entry_pos: int) -> float | None:
    """Return from entry_pos to the last available bar."""
    if entry_pos >= len(close) - 1:
        return None
    entry_price = float(close.iloc[entry_pos])
    last_price  = float(close.iloc[-1])
    return (last_price / entry_price - 1.0) * 100


def main() -> None:
    universe = (
        list(CONFIG.watchlists["stocks_us"])
        + list(CONFIG.watchlists["stocks_eu"])
        + list(CONFIG.watchlists["etfs_ucits"])
    )

    print(f"Fetching data for {len(universe)} tickers...")
    bars = market_data.prefetch_since(
        universe + [MKT_TICKER], DATA_START_DAYS, as_of=SCAN_END
    )
    print(f"Got data for {len(bars)} tickers.\n")

    mkt_close = bars[MKT_TICKER].df["close"] if MKT_TICKER in bars else None
    mkt_rsi   = compute_rsi(mkt_close, RSI_PERIOD) if mkt_close is not None else None

    scan_days = trading_days(SCAN_START, SCAN_END)

    rows = []
    seen: set[str] = set()  # first signal only per ticker

    for day in scan_days:
        # Market co-crash check for this day
        market_ok = False
        if mkt_rsi is not None:
            idx_arr = mkt_rsi.index.get_indexer(
                [pd.Timestamp(day)], method="ffill"
            )
            mkt_idx = int(idx_arr[0])
            if mkt_idx >= MKT_LOOKBACK:
                mkt_win = mkt_rsi.iloc[max(0, mkt_idx - MKT_LOOKBACK): mkt_idx]
                if not mkt_win.empty and float(mkt_win.min()) < MKT_RSI_BELOW:
                    market_ok = True

        if not market_ok:
            continue

        for ticker in universe:
            if ticker not in bars or ticker in seen:
                continue
            b = bars[ticker]
            close = b.df["close"]
            ts = pd.Timestamp(day)
            if ts not in close.index:
                idx = close.index.get_indexer([ts], method="ffill")[0]
            else:
                idx = close.index.get_loc(ts)
            if idx < RSI_PERIOD + RSI_LOOKBACK + 1:
                continue

            rsi_series = compute_rsi(close.iloc[: idx + 1], RSI_PERIOD)
            rsi_now = float(rsi_series.iloc[-1])
            if rsi_now != rsi_now or rsi_now <= RSI_NOW_ABOVE:
                continue

            rsi_min = rsi_min_window(rsi_series, len(rsi_series) - 1, RSI_LOOKBACK)
            if rsi_min != rsi_min or rsi_min >= RSI_WAS_BELOW:
                continue

            # Valid base signal — categorise by cap
            enters_current = rsi_now <= RSI_CAP_CURRENT   # Bots 6/7 logic
            enters_new     = rsi_now <= RSI_CAP_NEW

            fwd = forward_return(close, idx)

            rows.append({
                "date":           day,
                "ticker":         ticker,
                "rsi_now":        round(rsi_now, 1),
                "rsi_min_15d":    round(rsi_min, 1),
                "enters_current": enters_current,
                "enters_new":     enters_new,
                "fwd_return_pct": round(fwd, 1) if fwd is not None else None,
            })
            seen.add(ticker)

    if not rows:
        print("No qualifying signals found in scan window.")
        return

    df = pd.DataFrame(rows).sort_values("rsi_now")

    print("=" * 80)
    print("ALL QUALIFYING BASE SIGNALS (RSI was < 25, now > 40)")
    print("=" * 80)
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 120)
    print(df[["date","ticker","rsi_now","rsi_min_15d",
              "enters_current","enters_new","fwd_return_pct"]].to_string(index=False))

    print()
    print("=" * 80)
    print("SUMMARY")
    print("=" * 80)
    n = len(df)
    for label, col_name, cap in [
        ("Current (RSI <= 65)", "enters_current", RSI_CAP_CURRENT),
        (f"New cap  (RSI <= {RSI_CAP_NEW:.0f})", "enters_new", RSI_CAP_NEW),
    ]:
        sub = df[df[col_name]]
        blocked = df[~df[col_name]]
        avg_fwd = sub["fwd_return_pct"].mean() if not sub.empty else float("nan")
        print(f"\n  {label}:")
        print(f"    Trades entered : {len(sub)}/{n}")
        print(f"    Avg fwd return : {avg_fwd:+.1f}%  (to {SCAN_END})")
        if not blocked.empty:
            print(f"    BLOCKED ({len(blocked)} trades, RSI > {cap:.0f}):")
            for _, r in blocked.iterrows():
                fwd_str = f"{r['fwd_return_pct']:+.1f}%" if r["fwd_return_pct"] is not None else "n/a"
                print(f"      {r['ticker']:<10} RSI={r['rsi_now']:.1f}  fwd={fwd_str}")

    # Trades that differ between the two approaches
    diff = df[df["enters_current"] != df["enters_new"]]
    if not diff.empty:
        print(f"\n  Trades in current but NOT in new cap (RSI 60-65 zone):")
        for _, r in diff.iterrows():
            fwd_str = f"{r['fwd_return_pct']:+.1f}%" if r["fwd_return_pct"] is not None else "n/a"
            print(f"    {r['ticker']:<10} RSI={r['rsi_now']:.1f}  fwd return={fwd_str}")


if __name__ == "__main__":
    main()
