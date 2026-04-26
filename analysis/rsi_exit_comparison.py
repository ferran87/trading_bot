"""Three-way exit strategy comparison for Bot 6.

Option A (current)  : RSI take-profit at 70, trailing stop 35%
Option B            : RSI take-profit at 80, trailing stop 35%
Option C (proposed) : No hard RSI take-profit; trailing stop tightens with RSI
                       RSI < 70  → 35% trail
                       RSI 70-80 → 20% trail
                       RSI > 80  → 12% trail

For every trade in the real backtest we replay the daily price/RSI series
and record what each option would have done. Gives a per-trade and aggregate
comparison without having to re-run the full backtest engine three times.

Run: .venv/Scripts/python.exe -m analysis.rsi_exit_comparison
"""
from __future__ import annotations

import sys
from datetime import date

sys.path.insert(0, ".")

import pandas as pd

from analysis import market_data
from analysis.price_signals import rsi as compute_rsi
from backtesting.engine import run_backtest

START = date(2025, 1, 1)
END   = date(2026, 4, 22)

RSI_PERIOD       = 14
CATASTROPHIC     = -0.40

# Option A
RSI_TP_A         = 70.0
TRAIL_A          = 0.35

# Option B
RSI_TP_B         = 80.0
TRAIL_B          = 0.35

# Option C — progressive trail bands
def trail_c(rsi_val: float) -> float:
    if rsi_val >= 80:
        return 0.12
    if rsi_val >= 70:
        return 0.20
    return 0.35


def simulate_trade(
    ticker: str,
    entry_date: date,
    entry_price: float,
    bars_map: dict,
    option: str,           # "A", "B", or "C"
) -> dict:
    """Replay a trade from entry_date using a given exit option.

    Returns the exit date, price, reason, and gain.
    """
    bars = bars_map.get(ticker)
    if bars is None:
        return {"exit_date": END, "exit_price": entry_price,
                "reason": "no_data", "gain_pct": 0.0}

    full_rsi = compute_rsi(bars.df["close"], RSI_PERIOD)
    df = bars.df[bars.df.index >= pd.Timestamp(entry_date)].copy()
    if df.empty:
        return {"exit_date": END, "exit_price": entry_price,
                "reason": "no_data", "gain_pct": 0.0}

    peak = entry_price

    for ts, row in df.iterrows():
        price = float(row["close"])
        gain  = price / entry_price - 1.0
        rsi_val = float(full_rsi[ts]) if ts in full_rsi.index else float("nan")

        # Catastrophic stop — same for all options
        if gain <= CATASTROPHIC:
            return {"exit_date": ts.date(), "exit_price": price,
                    "reason": "catastrophic_stop", "gain_pct": gain * 100}

        # RSI take-profit
        if rsi_val == rsi_val:
            if option == "A" and rsi_val >= RSI_TP_A:
                return {"exit_date": ts.date(), "exit_price": price,
                        "reason": "rsi_tp_70", "gain_pct": gain * 100}
            if option == "B" and rsi_val >= RSI_TP_B:
                return {"exit_date": ts.date(), "exit_price": price,
                        "reason": "rsi_tp_80", "gain_pct": gain * 100}

        # Trailing stop (only when profitable)
        if gain > 0:
            peak = max(peak, price)
            if option == "C" and rsi_val == rsi_val:
                active_trail = trail_c(rsi_val)
            elif option == "A":
                active_trail = TRAIL_A
            else:
                active_trail = TRAIL_B

            drawdown = price / peak - 1.0
            if drawdown <= -active_trail:
                return {"exit_date": ts.date(), "exit_price": price,
                        "reason": f"trailing_stop_{int(active_trail*100)}pct",
                        "gain_pct": gain * 100}

    last_price = float(df["close"].iloc[-1])
    gain = last_price / entry_price - 1.0
    return {"exit_date": df.index[-1].date(), "exit_price": last_price,
            "reason": "held_to_end", "gain_pct": gain * 100}


def main() -> None:
    print(f"Running Bot 6 backtest ({START} to {END})...")
    result = run_backtest(bot_id=6, start_date=START, end_date=END)
    trades = result.trades_df

    print(f"Base backtest: {len(trades)} trades, "
          f"return={result.total_return_pct*100:+.2f}%  "
          f"Sharpe={result.sharpe:.2f}  MaxDD={result.max_drawdown*100:.2f}%\n")

    buys  = trades[trades["side"] == "BUY"].copy()
    sells = trades[trades["side"] == "SELL"].copy()

    if buys.empty:
        print("No trades to analyse.")
        return

    tickers = list(buys["ticker"].unique())
    print(f"Fetching price data for {len(tickers)} tickers…")
    bars_map = market_data.prefetch_since(tickers, 60)

    rows = []
    for ticker in tickers:
        t_buys  = buys[buys["ticker"] == ticker].sort_values("date")
        t_sells = sells[sells["ticker"] == ticker].sort_values("date")
        sell_q  = t_sells.to_dict("records")

        for _, buy in t_buys.iterrows():
            entry_price = buy["price_eur"]
            entry_date  = buy["date"].date() if hasattr(buy["date"], "date") else buy["date"]

            # Actual exit from the real backtest (option A)
            matched_sell = None
            for i, s in enumerate(sell_q):
                if s["date"] > buy["date"]:
                    matched_sell = sell_q.pop(i)
                    break

            # Simulate all three options from entry
            sim_a = simulate_trade(ticker, entry_date, entry_price, bars_map, "A")
            sim_b = simulate_trade(ticker, entry_date, entry_price, bars_map, "B")
            sim_c = simulate_trade(ticker, entry_date, entry_price, bars_map, "C")

            rows.append({
                "ticker":          ticker,
                "entry_date":      entry_date,
                # Option A
                "gain_A":          round(sim_a["gain_pct"], 2),
                "exit_A":          sim_a["exit_date"],
                "reason_A":        sim_a["reason"],
                # Option B
                "gain_B":          round(sim_b["gain_pct"], 2),
                "exit_B":          sim_b["exit_date"],
                "reason_B":        sim_b["reason"],
                # Option C
                "gain_C":          round(sim_c["gain_pct"], 2),
                "exit_C":          sim_c["exit_date"],
                "reason_C":        sim_c["reason"],
                # Deltas
                "B_vs_A":          round(sim_b["gain_pct"] - sim_a["gain_pct"], 2),
                "C_vs_A":          round(sim_c["gain_pct"] - sim_a["gain_pct"], 2),
            })

    df = pd.DataFrame(rows)

    # ── Per-trade table ────────────────────────────────────────────────────
    print("=" * 90)
    print("PER-TRADE: gain by option  (A=TP70  B=TP80  C=progressive trail)")
    print("=" * 90)
    display_cols = ["ticker", "entry_date",
                    "gain_A", "reason_A",
                    "gain_B", "reason_B",
                    "gain_C", "reason_C",
                    "B_vs_A", "C_vs_A"]
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 160)
    pd.set_option("display.max_rows", 60)
    print(df[display_cols].to_string(index=False))

    # ── Aggregate summary ──────────────────────────────────────────────────
    print("\n" + "=" * 90)
    print("AGGREGATE SUMMARY")
    print("=" * 90)
    n = len(df)
    for opt, label in [("A", "A  RSI TP=70 (current)       "),
                        ("B", "B  RSI TP=80                 "),
                        ("C", "C  Progressive trail (no TP) ")]:
        avg  = df[f"gain_{opt}"].mean()
        med  = df[f"gain_{opt}"].median()
        wins = (df[f"gain_{opt}"] > 0).sum()
        print(f"Option {label}  avg={avg:+.2f}%  median={med:+.2f}%  "
              f"winners={wins}/{n} ({wins/n*100:.0f}%)")

    print()
    print("Delta vs Option A (current):")
    for opt, label in [("B", "B vs A"), ("C", "C vs A")]:
        col   = f"{opt}_vs_A"
        better = (df[col] > 0).sum()
        worse  = (df[col] < 0).sum()
        print(f"  {label}:  avg delta={df[col].mean():+.2f}%  "
              f"better={better}  worse={worse}")

    # ── Exit reason breakdown ──────────────────────────────────────────────
    print("\nExit reason distribution:")
    for opt, label in [("A", "Option A"), ("B", "Option B"), ("C", "Option C")]:
        print(f"\n  {label}:")
        vc = df[f"reason_{opt}"].value_counts()
        for reason, count in vc.items():
            avg_gain = df[df[f"reason_{opt}"] == reason][f"gain_{opt}"].mean()
            print(f"    {reason:<35} {count:>3}x   avg gain {avg_gain:+.2f}%")


if __name__ == "__main__":
    main()
