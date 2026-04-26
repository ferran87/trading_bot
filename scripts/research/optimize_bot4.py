"""Bot 4 optimizer — data-first strategy design.

Scans the full universe over the last 2 months (Feb 20 – Apr 20, 2026) and answers:

  Step 1 — Which stocks had positive returns? (buy-and-hold baseline)
  Step 2 — Which entry signals would have selected them, and on what day?
  Step 3 — Which exit rules maximised per-trade return?
  Step 4 — Which entry+exit combo maximises a €2000 simulated portfolio?

Outputs CSVs to analysis/out/bot4/ and prints a markdown summary.

Usage: .venv/Scripts/python.exe -m scripts.research.optimize_bot4
"""
from __future__ import annotations

import io
import sys
from datetime import date, timedelta
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

import numpy as np
import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from analysis import market_data  # noqa: E402
from analysis.price_signals import above_sma, rsi, total_return  # noqa: E402
from core.config import CONFIG  # noqa: E402

OUT_DIR = _REPO_ROOT / "analysis" / "out" / "bot4"
WINDOW_START = date(2026, 2, 20)
WINDOW_END   = date(2026, 4, 20)
INITIAL_CAP  = 2000.0
MAX_POS_PCT  = 0.20   # hard cap per position
BASE_POS_PCT = 0.10   # target per position (10 slots)


def _to_md(df: pd.DataFrame, floatfmt: str = ".3f") -> str:
    try:
        return df.to_markdown(index=False, floatfmt=floatfmt)
    except ImportError:
        return df.to_string(index=False, float_format=lambda x: f"{x:.3f}")


def _universe() -> list[str]:
    wl = CONFIG.watchlists
    tickers: list[str] = []
    for key in ("stocks_us", "stocks_eu", "etfs_ucits"):
        tickers.extend(list(wl.get(key, [])))
    return list(dict.fromkeys(tickers))  # dedup, preserve order


def _realised_vol(close: pd.Series, days: int = 10) -> float:
    if len(close) < days + 1:
        return float("nan")
    return float(close.pct_change().dropna().iloc[-days:].std(ddof=0))


# ---------------------------------------------------------------------------
# Step 1 — per-ticker 2-month return table
# ---------------------------------------------------------------------------

def step1_returns(all_bars: dict) -> pd.DataFrame:
    sub_windows = [
        ("Feb20-Mar06", date(2026, 2, 20), date(2026, 3, 6)),
        ("Mar06-Mar27", date(2026, 3, 6),  date(2026, 3, 27)),
        ("Mar27-Apr20", date(2026, 3, 27), date(2026, 4, 20)),
    ]
    rows = []
    for ticker, bars in all_bars.items():
        close = bars.df["close"]
        # Window start bar
        start_ts = pd.Timestamp(WINDOW_START)
        end_ts   = pd.Timestamp(WINDOW_END)
        hist_start = close[close.index <= start_ts]
        hist_end   = close[close.index <= end_ts]
        if hist_start.empty or hist_end.empty:
            continue
        p_start = float(hist_start.iloc[-1])
        p_end   = float(hist_end.iloc[-1])
        total_ret = p_end / p_start - 1.0 if p_start > 0 else float("nan")

        # Sub-window returns
        sub_rets = {}
        for label, ws, we in sub_windows:
            h_ws = close[close.index <= pd.Timestamp(ws)]
            h_we = close[close.index <= pd.Timestamp(we)]
            if not h_ws.empty and not h_we.empty and float(h_ws.iloc[-1]) > 0:
                sub_rets[label] = float(h_we.iloc[-1]) / float(h_ws.iloc[-1]) - 1.0
            else:
                sub_rets[label] = float("nan")

        # Features at window start
        feat_rsi   = float(rsi(hist_start, 14).iloc[-1]) if len(hist_start) >= 15 else float("nan")
        feat_a50   = bool(above_sma(hist_start, 50))
        feat_a200  = bool(above_sma(hist_start, 200))
        feat_20d   = total_return(hist_start, 20)
        feat_vol   = _realised_vol(hist_start, 10)

        rows.append({
            "ticker": ticker,
            "total_2mo": total_ret,
            **sub_rets,
            "rsi_at_start": feat_rsi,
            "above_sma50_start": feat_a50,
            "above_sma200_start": feat_a200,
            "ret_20d_start": feat_20d,
            "vol_10d_start": feat_vol,
            "price_start": p_start,
            "price_end": p_end,
        })
    df = pd.DataFrame(rows).sort_values("total_2mo", ascending=False).reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# Step 2 — entry signal scan
# ---------------------------------------------------------------------------

def _sma(close: pd.Series, period: int) -> float:
    if len(close) < period:
        return float("nan")
    return float(close.iloc[-period:].mean())


def step2_entry_scan(all_bars: dict) -> pd.DataFrame:
    universe_closes: dict[str, pd.Series] = {t: b.df["close"] for t, b in all_bars.items()}
    trading_days = sorted(
        {d.date() for bars in all_bars.values()
         for d in bars.df.index
         if WINDOW_START <= d.date() <= WINDOW_END}
    )
    rows = []
    for d in trading_days:
        d_ts = pd.Timestamp(d)
        # Universe-wide 20-day returns for momentum ranking
        universe_20d: dict[str, float] = {}
        for t, close in universe_closes.items():
            hist = close[close.index <= d_ts]
            universe_20d[t] = total_return(hist, 20)
        sorted_by_mom = sorted(
            [(t, r) for t, r in universe_20d.items() if not np.isnan(r)],
            key=lambda x: -x[1],
        )
        top10_tickers = {t for t, _ in sorted_by_mom[:10]}
        # Universe vol percentile for low_vol_dip signal
        vols = [_realised_vol(universe_closes[t][universe_closes[t].index <= d_ts], 10)
                for t in universe_closes]
        vols = [v for v in vols if not np.isnan(v)]
        vol_p20 = float(np.percentile(vols, 20)) if vols else float("nan")

        for ticker, bars in all_bars.items():
            close = bars.df["close"]
            hist = close[close.index <= d_ts]
            if len(hist) < 60:
                continue
            price = float(hist.iloc[-1])
            if price <= 0:
                continue

            # Forward returns from this entry day
            fwd = close[close.index > d_ts]
            fwd_ret = {}
            for h in (14, 30, 60):
                if len(fwd) >= h and price > 0:
                    fwd_ret[f"fwd_{h}d"] = float(fwd.iloc[h - 1]) / price - 1.0
                elif not fwd.empty and price > 0:
                    fwd_ret[f"fwd_{h}d"] = float(fwd.iloc[-1]) / price - 1.0
                else:
                    fwd_ret[f"fwd_{h}d"] = float("nan")

            # Signal flags
            sig_mom_top10 = ticker in top10_tickers
            # SMA-50 breakout: close > sma50 today AND close was < sma50 five days ago
            sma50_now = _sma(hist, 50)
            hist_5d_ago = close[close.index <= d_ts - pd.Timedelta(days=7)]  # ~5 trading days
            sma50_5d_ago = _sma(hist_5d_ago, 50) if len(hist_5d_ago) >= 50 else float("nan")
            close_5d_ago = float(hist_5d_ago.iloc[-1]) if not hist_5d_ago.empty else float("nan")
            sig_sma50_breakout = (
                not np.isnan(sma50_now) and price > sma50_now
                and not np.isnan(sma50_5d_ago) and not np.isnan(close_5d_ago)
                and close_5d_ago < sma50_5d_ago
            )
            # Low-vol dip: 10-day vol below universe p20 AND 5-day return between -10% and -3%
            vol10 = _realised_vol(hist, 10)
            ret5d = total_return(hist, 5)
            sig_low_vol_dip = (
                not np.isnan(vol10) and not np.isnan(vol_p20)
                and vol10 < vol_p20
                and not np.isnan(ret5d) and -0.10 < ret5d < -0.03
            )
            # RSI recovery: RSI was < 35 one week ago and is now > 40
            rsi_now = float(rsi(hist, 14).iloc[-1]) if len(hist) >= 15 else float("nan")
            hist_1w = close[close.index <= d_ts - pd.Timedelta(days=8)]
            rsi_1w_ago = float(rsi(hist_1w, 14).iloc[-1]) if len(hist_1w) >= 15 else float("nan")
            sig_rsi_recovery = (
                not np.isnan(rsi_now) and not np.isnan(rsi_1w_ago)
                and rsi_1w_ago < 35 and rsi_now > 40
            )
            # Buy-and-hold day 1
            sig_bah_day1 = (d == WINDOW_START)

            if not any([sig_mom_top10, sig_sma50_breakout, sig_low_vol_dip,
                        sig_rsi_recovery, sig_bah_day1]):
                continue

            rows.append({
                "ticker": ticker,
                "date": d,
                "price": price,
                "sig_momentum_top10":  sig_mom_top10,
                "sig_sma50_breakout":  sig_sma50_breakout,
                "sig_low_vol_dip":     sig_low_vol_dip,
                "sig_rsi_recovery":    sig_rsi_recovery,
                "sig_bah_day1":        sig_bah_day1,
                "rsi": rsi_now,
                "ret_20d": universe_20d.get(ticker, float("nan")),
                **fwd_ret,
            })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Step 3 — exit rule comparison per (ticker, entry_date)
# ---------------------------------------------------------------------------

def _apply_exit(close_fwd: pd.Series, entry_price: float, rule: str) -> tuple[float, int]:
    """Return (final_return, days_held). close_fwd starts the day AFTER entry."""
    if close_fwd.empty or entry_price <= 0:
        return float("nan"), 0
    peak = entry_price
    for i, (_, px) in enumerate(close_fwd.items(), start=1):
        px = float(px)
        if px > peak:
            peak = px
        if rule == "hold_to_end":
            pass
        elif rule == "trail_7pct" and px / peak - 1.0 <= -0.07:
            return px / entry_price - 1.0, i
        elif rule == "trail_15pct" and px / peak - 1.0 <= -0.15:
            return px / entry_price - 1.0, i
        elif rule == "sma50_cross_below":
            # Need full history — approximate: sell if price drops >12% from entry with no trail
            # (full SMA requires context; use px < entry_price * 0.88 as proxy)
            if px < entry_price * 0.88:
                return px / entry_price - 1.0, i
        elif rule == "profit_target_5pct" and px / entry_price - 1.0 >= 0.05:
            return px / entry_price - 1.0, i
        elif rule == "profit_target_10pct" and px / entry_price - 1.0 >= 0.10:
            return px / entry_price - 1.0, i
    last = float(close_fwd.iloc[-1])
    return last / entry_price - 1.0, len(close_fwd)


def step3_exit_comparison(entry_scan: pd.DataFrame, all_bars: dict) -> pd.DataFrame:
    exit_rules = ["hold_to_end", "trail_7pct", "trail_15pct",
                  "sma50_cross_below", "profit_target_5pct", "profit_target_10pct"]
    rows = []
    for _, row in entry_scan.iterrows():
        ticker = row["ticker"]
        bars = all_bars.get(ticker)
        if bars is None:
            continue
        close = bars.df["close"]
        entry_ts = pd.Timestamp(row["date"])
        close_fwd = close[(close.index > entry_ts) & (close.index <= pd.Timestamp(WINDOW_END))]
        entry_price = float(row["price"])
        rec = {"ticker": ticker, "entry_date": row["date"], "entry_price": entry_price}
        for sig in ("sig_momentum_top10", "sig_sma50_breakout",
                    "sig_low_vol_dip", "sig_rsi_recovery", "sig_bah_day1"):
            rec[sig] = row.get(sig, False)
        for rule in exit_rules:
            ret, days = _apply_exit(close_fwd, entry_price, rule)
            rec[f"ret_{rule}"] = ret
            rec[f"days_{rule}"] = days
        rows.append(rec)
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Step 4 — portfolio simulation
# ---------------------------------------------------------------------------

def step4_portfolio_sim(
    entry_scan: pd.DataFrame,
    all_bars: dict,
    signal_col: str,
    exit_rule: str,
    label: str,
) -> dict:
    """Greedy equal-weight portfolio simulation for one signal+exit combo."""
    cash = INITIAL_CAP
    positions: dict[str, dict] = {}  # ticker -> {qty, entry_price, entry_date}
    equity_curve: list[tuple[date, float]] = []
    last_buy: dict[str, date] = {}
    trades = 0

    # Collect all entry signals for this combo, sorted by date
    signals = (
        entry_scan[entry_scan[signal_col] == True]  # noqa: E712
        .sort_values("date")
        .copy()
    )

    # Build trading day sequence
    all_dates = sorted({d.date() for bars in all_bars.values()
                        for d in bars.df.index
                        if WINDOW_START <= d.date() <= WINDOW_END})

    for d in all_dates:
        d_ts = pd.Timestamp(d)

        # --- exits ---
        to_sell = []
        for ticker, pos in list(positions.items()):
            bars = all_bars.get(ticker)
            if bars is None:
                continue
            close = bars.df["close"]
            hist_today = close[close.index <= d_ts]
            if hist_today.empty:
                continue
            price = float(hist_today.iloc[-1])
            fwd_after = close[(close.index > d_ts) & (close.index <= pd.Timestamp(WINDOW_END))]
            # re-apply exit rule at each day
            entry_price = pos["entry_price"]
            peak = pos.get("peak", entry_price)
            if price > peak:
                pos["peak"] = peak = price

            exit_now = False
            if exit_rule == "trail_7pct" and price / peak - 1.0 <= -0.07:
                exit_now = True
            elif exit_rule == "trail_15pct" and price / peak - 1.0 <= -0.15:
                exit_now = True
            elif exit_rule == "sma50_cross_below" and price < entry_price * 0.88:
                exit_now = True
            elif exit_rule == "profit_target_5pct" and price / entry_price - 1.0 >= 0.05:
                exit_now = True
            elif exit_rule == "profit_target_10pct" and price / entry_price - 1.0 >= 0.10:
                exit_now = True

            if exit_now:
                cash += pos["qty"] * price
                to_sell.append(ticker)
                trades += 1
        for t in to_sell:
            del positions[t]

        # --- entries ---
        todays_signals = signals[signals["date"] == d]
        for _, sig_row in todays_signals.iterrows():
            ticker = sig_row["ticker"]
            if ticker in positions:
                continue  # already held
            if (ticker in last_buy and
                    (d - last_buy[ticker]).days < 5):
                continue  # re-entry cooldown
            n_positions = len(positions)
            target_value = min(INITIAL_CAP * BASE_POS_PCT,
                               INITIAL_CAP * MAX_POS_PCT,
                               cash * 0.99)
            if target_value < 10 or n_positions >= int(1 / BASE_POS_PCT):
                continue
            if cash < target_value:
                continue
            price = float(sig_row["price"])
            if price <= 0:
                continue
            qty = target_value / price
            cash -= qty * price
            positions[ticker] = {"qty": qty, "entry_price": price, "peak": price,
                                  "entry_date": d}
            last_buy[ticker] = d
            trades += 1

        # Mark-to-market equity
        pos_value = sum(
            pos["qty"] * float(
                all_bars[t].df["close"][
                    all_bars[t].df["close"].index <= d_ts
                ].iloc[-1]
            )
            for t, pos in positions.items()
            if t in all_bars and not all_bars[t].df["close"][
                all_bars[t].df["close"].index <= d_ts
            ].empty
        )
        equity_curve.append((d, cash + pos_value))

    final_equity = equity_curve[-1][1] if equity_curve else INITIAL_CAP
    eq_series = pd.Series([e for _, e in equity_curve])
    running_max = eq_series.cummax()
    drawdowns = (eq_series - running_max) / running_max
    max_dd = float(drawdowns.min())

    return {
        "label": label,
        "signal": signal_col,
        "exit_rule": exit_rule,
        "final_equity": round(final_equity, 2),
        "total_return_pct": round((final_equity / INITIAL_CAP - 1) * 100, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "trades": trades,
        "open_positions_end": len(positions),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"# Bot 4 Optimizer: {WINDOW_START} -> {WINDOW_END}\n")

    tickers = _universe()
    print(f"Fetching 2y bars for {len(tickers)} tickers...")
    all_bars = market_data.fetch_many(tickers, period="2y")
    print(f"Got data for {len(all_bars)}/{len(tickers)} tickers.\n")

    # Step 1 — returns table
    print("## Step 1: Per-ticker 2-month returns (buy & hold)\n")
    returns_df = step1_returns(all_bars)
    returns_df.to_csv(OUT_DIR / "returns_2mo.csv", index=False)
    display_cols = ["ticker", "total_2mo", "Feb20-Mar06", "Mar06-Mar27",
                    "Mar27-Apr20", "rsi_at_start", "above_sma50_start", "above_sma200_start"]
    print(_to_md(returns_df[display_cols].head(20)))
    print(f"\n... ({len(returns_df)} tickers total. Full table in returns_2mo.csv)\n")
    winners = returns_df[returns_df["total_2mo"] > 0]
    losers  = returns_df[returns_df["total_2mo"] < 0]
    print(f"Winners (positive 2-month return): {len(winners)} tickers")
    print(f"Losers:  {len(losers)} tickers")
    if not winners.empty:
        print(f"Top 5: {', '.join(winners['ticker'].head(5).tolist())}")
    print()

    # Step 2 — entry signal scan
    print("## Step 2: Entry signal scan\n")
    entry_scan = step2_entry_scan(all_bars)
    entry_scan.to_csv(OUT_DIR / "entry_scan.csv", index=False)
    print(f"Total signal fires: {len(entry_scan)}\n")
    for sig in ("sig_momentum_top10", "sig_sma50_breakout",
                "sig_low_vol_dip", "sig_rsi_recovery", "sig_bah_day1"):
        sub = entry_scan[entry_scan[sig] == True]  # noqa: E712
        fwd14 = sub["fwd_14d"].dropna()
        print(
            f"  {sig:<25}: {len(sub):3d} fires | "
            f"mean fwd14d = {fwd14.mean()*100:+.1f}% | "
            f"win_rate = {(fwd14>0).mean()*100:.0f}%"
        )
    print()

    # Step 3 — exit comparison
    print("## Step 3: Exit rule comparison\n")
    exit_df = step3_exit_comparison(entry_scan, all_bars)
    exit_df.to_csv(OUT_DIR / "exit_comparison.csv", index=False)
    exit_rules = ["hold_to_end", "trail_7pct", "trail_15pct",
                  "sma50_cross_below", "profit_target_5pct", "profit_target_10pct"]
    exit_summary_rows = []
    for rule in exit_rules:
        col = f"ret_{rule}"
        s = exit_df[col].dropna()
        exit_summary_rows.append({
            "exit_rule": rule,
            "n": len(s),
            "mean_ret": s.mean(),
            "win_rate": (s > 0).mean(),
            "median_ret": s.median(),
        })
    print(_to_md(pd.DataFrame(exit_summary_rows)))
    print()

    # Step 4 — portfolio simulation
    print("## Step 4: Portfolio simulation (EUR 2000, 10% per position)\n")
    combos = [
        ("sig_momentum_top10",  "hold_to_end",        "momentum_top10 + hold"),
        ("sig_momentum_top10",  "trail_15pct",        "momentum_top10 + trail15%"),
        ("sig_momentum_top10",  "trail_7pct",         "momentum_top10 + trail7%"),
        ("sig_sma50_breakout",  "hold_to_end",        "sma50_breakout + hold"),
        ("sig_sma50_breakout",  "trail_15pct",        "sma50_breakout + trail15%"),
        ("sig_rsi_recovery",    "hold_to_end",        "rsi_recovery + hold"),
        ("sig_rsi_recovery",    "trail_15pct",        "rsi_recovery + trail15%"),
        ("sig_low_vol_dip",     "hold_to_end",        "low_vol_dip + hold"),
        ("sig_bah_day1",        "hold_to_end",        "buy_and_hold_day1 + hold"),
        ("sig_bah_day1",        "trail_15pct",        "buy_and_hold_day1 + trail15%"),
    ]
    sim_results = []
    for sig_col, exit_rule, label in combos:
        result = step4_portfolio_sim(entry_scan, all_bars, sig_col, exit_rule, label)
        sim_results.append(result)
        print(
            f"  {label:<40}: {result['final_equity']:>8.2f} EUR "
            f"({result['total_return_pct']:+.1f}%)  "
            f"maxDD {result['max_drawdown_pct']:.1f}%  "
            f"{result['trades']} trades"
        )
    sim_df = pd.DataFrame(sim_results).sort_values("total_return_pct", ascending=False)
    sim_df.to_csv(OUT_DIR / "portfolio_sim.csv", index=False)
    print()

    # Recommendation
    best = sim_df.iloc[0]
    print("## Recommendation\n")
    print(f"Best combo:  **{best['label']}**")
    print(f"Final equity: EUR {best['final_equity']:.2f}  "
          f"({best['total_return_pct']:+.1f}%)  "
          f"max drawdown {best['max_drawdown_pct']:.1f}%")
    print(f"\nUse `signal={best['signal']}` and `exit_rule={best['exit_rule']}` "
          f"as the starting point for Bot 4.")
    print(f"\nCSVs written to: {OUT_DIR}")


if __name__ == "__main__":
    main()
