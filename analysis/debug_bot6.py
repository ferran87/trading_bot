"""Debug script: trace why Bot 6 fires 0 trades.

Simulates the RSI Accumulator entry check for specific dates and prints
exactly which condition blocks each ticker.

Run: .venv/Scripts/python.exe -m analysis.debug_bot6
"""
from __future__ import annotations

import sys
from datetime import date

sys.path.insert(0, ".")

from analysis import market_data
from analysis.price_signals import rsi
from core.config import CONFIG

# Mirror the strategy parameters exactly
params = CONFIG.strategies["strategies"]["rsi_accumulator"]
rsi_period        = int(params.get("rsi_period", 14))
rsi_was_below     = float(params.get("rsi_was_below", 25))
rsi_now_above     = float(params.get("rsi_now_above", 40))
rsi_entry_max     = float(params.get("rsi_entry_max", 65))
lookback_days     = int(params.get("rsi_lookback_days", 15))
min_history       = int(params.get("min_history_days", 60))
mkt_ticker        = params.get("market_filter_ticker")
mkt_rsi_below     = float(params.get("market_rsi_was_below", 30))
mkt_rsi_lookback  = int(params.get("market_rsi_lookback_days", lookback_days))

from analysis.market_data import Bars
import pandas as pd


def _rsi_min_recent(close: pd.Series, rsi_period: int, lookback: int) -> float:
    if len(close) < rsi_period + lookback + 1:
        return float("nan")
    rsi_series = rsi(close.iloc[:-1], rsi_period)
    window = rsi_series.iloc[-lookback:]
    if window.empty:
        return float("nan")
    return float(window.min())


def check_date(as_of: date) -> None:
    print(f"\n{'='*60}")
    print(f"Checking as_of={as_of}")

    from core.config import CONFIG
    watchlists = CONFIG.watchlists
    universe = list(watchlists["stocks_us"]) + list(watchlists["stocks_eu"]) + list(watchlists["etfs_ucits"])
    if mkt_ticker and mkt_ticker not in universe:
        universe.append(mkt_ticker)

    bars_map = market_data.prefetch_since(universe, min_history, as_of=as_of)

    # Check market filter
    market_was_oversold = True
    if mkt_ticker and mkt_ticker in bars_map:
        mkt_close = bars_map[mkt_ticker].df["close"]
        mkt_rsi_min = _rsi_min_recent(mkt_close, rsi_period, mkt_rsi_lookback)
        market_was_oversold = not (mkt_rsi_min != mkt_rsi_min) and mkt_rsi_min < mkt_rsi_below
        print(f"Market filter ({mkt_ticker}): RSI min in last {mkt_rsi_lookback} days = {mkt_rsi_min:.1f} (need < {mkt_rsi_below}) -> {'PASS' if market_was_oversold else 'FAIL'}")
        # Show last 5 RSI values for market
        mkt_rsi_series = rsi(mkt_close, rsi_period)
        print(f"  Last 5 {mkt_ticker} RSI: {[f'{v:.1f}' for v in mkt_rsi_series.iloc[-5:].values]}")
    else:
        print(f"Market filter: {mkt_ticker} NOT in bars!")

    if not market_was_oversold:
        print("  → Market filter BLOCKS all entries")

    print(f"\nPer-ticker results (min_history={min_history}, rsi_now_above={rsi_now_above}, rsi_was_below={rsi_was_below}):")
    passed = []
    for ticker, bars in bars_map.items():
        if ticker == mkt_ticker:
            continue
        n = len(bars.df)
        if n < min_history:
            print(f"  {ticker}: SKIP — only {n} bars (need {min_history})")
            continue

        close = bars.df["close"]
        if len(close) < rsi_period + 1:
            print(f"  {ticker}: SKIP — not enough bars for RSI ({len(close)})")
            continue

        rsi_now = float(rsi(close, rsi_period).iloc[-1])
        rsi_min = _rsi_min_recent(close, rsi_period, lookback_days)

        if rsi_now != rsi_now:
            print(f"  {ticker}: SKIP — RSI now = NaN")
            continue
        if rsi_min != rsi_min:
            print(f"  {ticker}: SKIP — RSI min = NaN")
            continue

        ok_now = rsi_now_above < rsi_now < rsi_entry_max
        ok_min = rsi_min < rsi_was_below
        status = "ENTRY" if (ok_now and ok_min and market_was_oversold) else "skip"
        reasons = []
        if not ok_now:
            reasons.append(f"RSI now={rsi_now:.1f} not in ({rsi_now_above},{rsi_entry_max})")
        if not ok_min:
            reasons.append(f"RSI min={rsi_min:.1f} >= {rsi_was_below}")
        if not market_was_oversold:
            reasons.append("market not oversold")
        reason_str = "; ".join(reasons) if reasons else ""
        print(f"  {ticker}: RSI now={rsi_now:.1f} min={rsi_min:.1f} bars={n} | {status} {reason_str}")
        if ok_now and ok_min and market_was_oversold:
            passed.append(ticker)

    print(f"\nResult: {len(passed)} tickers would trigger entry: {passed}")


if __name__ == "__main__":
    # Check key dates around the 2025 crash recovery
    for d in [date(2025, 4, 7), date(2025, 4, 9), date(2025, 4, 10), date(2025, 4, 14)]:
        check_date(d)
