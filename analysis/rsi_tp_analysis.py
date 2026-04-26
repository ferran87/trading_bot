"""Compare RSI take-profit thresholds (70 vs 80) for Bot 6 (RSI Accumulator).

For each trade closed with an RSI take-profit exit at 70, this script also
simulates what the return would have been if we had held until RSI > 80 instead
(or until the trailing stop / catastrophic stop fired first).

Run: .venv/Scripts/python.exe -m analysis.rsi_tp_analysis
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from copy import deepcopy

sys.path.insert(0, ".")

import pandas as pd

from analysis.price_signals import rsi as compute_rsi
from backtesting.engine import run_backtest
from analysis import market_data

START = date(2025, 1, 1)
END   = date(2026, 4, 22)

TRAIL_PCT        = 0.35   # trailing stop (only when profitable)
CATASTROPHIC     = -0.40  # hard floor


def _simulate_hold_longer(
    ticker: str,
    entry_date: date,
    entry_price_eur: float,
    exit_date_tp70: date,
    exit_price_tp70: float,
    rsi_tp_new: float,
    bars_map: dict,
) -> dict:
    """Given a trade that was exited at RSI=70, simulate holding until RSI=80
    (or until trailing stop / catastrophic stop fires).

    Returns a dict with the simulated exit price, date, reason, and gain.
    """
    bars = bars_map.get(ticker)
    if bars is None:
        return {}

    df = bars.df.copy()
    df = df[df.index > pd.Timestamp(exit_date_tp70)]
    if df.empty:
        return {"exit_date": exit_date_tp70, "exit_price": exit_price_tp70,
                "reason": "no_data_after", "gain_pct": 0.0}

    rsi_series = compute_rsi(bars.df["close"], 14)
    peak = exit_price_tp70  # reset peak from new hold start

    for ts, row in df.iterrows():
        price = float(row["close"])
        gain = price / entry_price_eur - 1.0

        # Catastrophic stop
        if gain <= CATASTROPHIC:
            return {"exit_date": ts.date(), "exit_price": price,
                    "reason": "catastrophic_stop", "gain_pct": gain * 100}

        # Trailing stop (only when profitable vs entry)
        if gain > 0:
            peak = max(peak, price)
            drawdown = price / peak - 1.0
            if drawdown <= -TRAIL_PCT:
                return {"exit_date": ts.date(), "exit_price": price,
                        "reason": "trailing_stop", "gain_pct": gain * 100}

        # New RSI take-profit
        if ts in rsi_series.index:
            rsi_val = float(rsi_series[ts])
            if rsi_val == rsi_val and rsi_val >= rsi_tp_new:
                return {"exit_date": ts.date(), "exit_price": price,
                        "reason": f"rsi_tp_{int(rsi_tp_new)}", "gain_pct": gain * 100}

    # Held to end of data
    last_price = float(df["close"].iloc[-1])
    gain = last_price / entry_price_eur - 1.0
    return {"exit_date": df.index[-1].date(), "exit_price": last_price,
            "reason": "held_to_end", "gain_pct": gain * 100}


def main() -> None:
    print(f"Running Bot 6 backtest ({START} to {END})…")
    result = run_backtest(bot_id=6, start_date=START, end_date=END)

    trades = result.trades_df
    if trades.empty:
        print("No trades found.")
        return

    print(f"Total trades: {len(trades)}")
    print(f"Return (RSI TP=70): {result.total_return_pct*100:+.2f}%")
    print(f"Sharpe: {result.sharpe:.2f}  Max DD: {result.max_drawdown*100:.2f}%")

    # Find trades closed by RSI take-profit at 70
    tp70_sells = trades[
        (trades["side"] == "SELL") &
        (trades["signal_reason"].str.contains("take-profit", na=False))
    ].copy()

    print(f"\nTrades closed by RSI take-profit (70): {len(tp70_sells)}")
    if tp70_sells.empty:
        print("Nothing to simulate.")
        return

    # Pre-fetch all bars once (expensive, but shared)
    print("Fetching market data for post-exit simulation…")
    tickers = list(tp70_sells["ticker"].unique())
    bars_map = market_data.prefetch_since(tickers, 60)

    # For each RSI-70 exit, find the matching buy and simulate holding to RSI 80
    buys = trades[trades["side"] == "BUY"].copy()

    rows = []
    for _, sell in tp70_sells.iterrows():
        ticker = sell["ticker"]
        sell_date = sell["date"]
        sell_price = sell["price_eur"]

        # Match most recent buy before this sell
        t_buys = buys[
            (buys["ticker"] == ticker) &
            (buys["date"] < sell_date)
        ].sort_values("date")
        if t_buys.empty:
            continue
        buy = t_buys.iloc[-1]
        entry_price = buy["price_eur"]
        entry_date = buy["date"].date() if hasattr(buy["date"], "date") else buy["date"]

        gain_tp70 = sell_price / entry_price - 1.0
        sell_date_d = sell_date.date() if hasattr(sell_date, "date") else sell_date

        # Simulate what happens if we hold until RSI 80 instead
        sim = _simulate_hold_longer(
            ticker=ticker,
            entry_date=entry_date,
            entry_price_eur=entry_price,
            exit_date_tp70=sell_date_d,
            exit_price_tp70=sell_price,
            rsi_tp_new=80.0,
            bars_map=bars_map,
        )
        if not sim:
            continue

        delta = sim["gain_pct"] - gain_tp70 * 100
        rows.append({
            "ticker": ticker,
            "entry_date": entry_date,
            "exit_date_70": sell_date_d,
            "gain_tp70_pct": round(gain_tp70 * 100, 2),
            "exit_date_80": sim.get("exit_date"),
            "gain_tp80_pct": round(sim.get("gain_pct", 0), 2),
            "delta_pct": round(delta, 2),
            "days_extra": (sim.get("exit_date") - sell_date_d).days if sim.get("exit_date") else 0,
            "exit_reason_80": sim.get("reason"),
        })

    if not rows:
        print("Could not match buys to RSI-70 sells.")
        return

    df = pd.DataFrame(rows)

    print("\n" + "="*70)
    print("PER-TRADE COMPARISON: RSI TP=70 vs hold-until-RSI-80")
    print("="*70)
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 120)
    print(df.to_string(index=False))

    print("\n" + "="*70)
    print("SUMMARY")
    print("="*70)
    n = len(df)
    better  = (df["delta_pct"] > 0).sum()
    worse   = (df["delta_pct"] < 0).sum()
    neutral = (df["delta_pct"] == 0).sum()
    print(f"Trades analysed      : {n}")
    print(f"Better with TP=80    : {better} ({better/n*100:.0f}%)")
    print(f"Worse with TP=80     : {worse}  ({worse/n*100:.0f}%)")
    print(f"No difference        : {neutral}")
    print(f"Avg gain delta       : {df['delta_pct'].mean():+.2f}%")
    print(f"Avg extra days held  : {df['days_extra'].mean():.0f}")
    print(f"Exit reasons (TP=80) :\n{df['exit_reason_80'].value_counts().to_string()}")

    avg_tp70 = df["gain_tp70_pct"].mean()
    avg_tp80 = df["gain_tp80_pct"].mean()
    print(f"\nAvg gain per trade (TP=70): {avg_tp70:+.2f}%")
    print(f"Avg gain per trade (TP=80): {avg_tp80:+.2f}%")
    print(f"Improvement               : {avg_tp80 - avg_tp70:+.2f}%")

    print("\nBreakdown by exit reason at RSI=80:")
    print(df.groupby("exit_reason_80")["delta_pct"].agg(["count", "mean", "min", "max"]).round(2).to_string())


if __name__ == "__main__":
    main()
