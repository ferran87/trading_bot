"""Diagnostic: when would holding past the 14-day safety net have helped?

Runs a 6-month Bot 3 backtest, collects every SELL tagged with the
``safety net: max days held`` reason, and for each forced exit computes:

  - forward closes at T+3 / +7 / +14 / +30 after the exit (if we had held)
  - post-exit MFE (max favourable excursion over the next 30 days)
  - synthetic trailing-stop outcome (when would a 7% trail from peak close
    have exited the trade had we kept it open?)

It also records features *available at the exit date* so we can bucket:

  - gain at exit (price / avg_entry - 1)
  - whether close is above its own 50 / 200-day SMA at exit
  - RSI(14) at exit
  - consecutive up days ending at the exit date
  - 20-day total return (trend strength)
  - 10-day realised volatility

Output is a markdown summary to stdout plus CSVs under
``analysis/out/hold_extension/``.

Usage: ``.venv/Scripts/python.exe -m scripts.research.diagnose_hold_extension``
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
from scripts.research.diagnose_sharp_dip import match_trades  # noqa: E402
from backtesting.engine import run_backtest  # noqa: E402
from core.config import CONFIG  # noqa: E402

OUT_DIR = _REPO_ROOT / "analysis" / "out" / "hold_extension"
FETCH_PERIOD = "2y"
TRAIL_PCT = float(CONFIG.strategies["strategies"]["sharp_dip"].get("trail_pct", 0.07))
FWD_HORIZONS = (3, 7, 14, 30)


def _to_md(df: pd.DataFrame) -> str:
    try:
        return df.to_markdown(index=False, floatfmt=".3f")
    except ImportError:
        return df.to_string(index=False, float_format=lambda x: f"{x:.3f}")


def _consec_up_days(close: pd.Series) -> int:
    if len(close) < 2:
        return 0
    n = 0
    for i in range(len(close) - 1, 0, -1):
        if float(close.iloc[i]) > float(close.iloc[i - 1]):
            n += 1
        else:
            break
    return n


def _realised_vol(close: pd.Series, days: int = 10) -> float:
    if len(close) < days + 1:
        return float("nan")
    rets = close.pct_change().dropna().iloc[-days:]
    if rets.empty:
        return float("nan")
    return float(rets.std(ddof=0))


def _universe() -> list[str]:
    wl = CONFIG.watchlists
    return list(wl["stocks_us"]) + list(wl["stocks_eu"])


def _features_at(close: pd.Series, entry_price: float, exit_price: float) -> dict:
    """Features computed at the exit date from the price history up to exit."""
    gain = exit_price / entry_price - 1.0 if entry_price > 0 else float("nan")
    return {
        "gain_at_exit": gain,
        "above_sma50": bool(above_sma(close, 50)),
        "above_sma200": bool(above_sma(close, 200)),
        "rsi_at_exit": float(rsi(close, 14).iloc[-1]) if len(close) >= 15 else float("nan"),
        "consec_up_days": _consec_up_days(close),
        "ret_20d": total_return(close, 20),
        "vol_10d": _realised_vol(close, 10),
    }


def _synthetic_trail_exit(close_fwd: pd.Series, entry_price: float) -> dict:
    """Walk forward from the exit date and find when a `TRAIL_PCT` trailing
    stop from the running peak would have exited. If never triggered within
    the forward window, use the final bar.
    """
    if close_fwd.empty:
        return {"synth_exit_days": float("nan"), "synth_exit_return": float("nan")}
    peak = float(close_fwd.iloc[0])
    for i, (ts, px) in enumerate(close_fwd.items(), start=1):
        px = float(px)
        if px > peak:
            peak = px
        if peak > 0 and (px / peak - 1.0) <= -TRAIL_PCT:
            return {
                "synth_exit_days": i,
                "synth_exit_return": px / entry_price - 1.0 if entry_price > 0 else float("nan"),
            }
    last = float(close_fwd.iloc[-1])
    return {
        "synth_exit_days": len(close_fwd),
        "synth_exit_return": last / entry_price - 1.0 if entry_price > 0 else float("nan"),
    }


def _forward_returns(close_fwd: pd.Series, exit_price: float) -> dict:
    out: dict = {}
    for h in FWD_HORIZONS:
        if len(close_fwd) >= h and exit_price > 0:
            out[f"hold_ret_{h}d"] = float(close_fwd.iloc[h - 1]) / exit_price - 1.0
        else:
            out[f"hold_ret_{h}d"] = float("nan")
    if not close_fwd.empty and exit_price > 0:
        out["post_exit_mfe"] = float(close_fwd.iloc[: min(len(close_fwd), 30)].max()) / exit_price - 1.0
    else:
        out["post_exit_mfe"] = float("nan")
    return out


def analyse_forced_exits(
    closed_df: pd.DataFrame,
    all_bars: dict,
) -> pd.DataFrame:
    rows = []
    for _, t in closed_df.iterrows():
        reason = str(t.get("exit_reason", "")).lower()
        if "safety net" not in reason and "max days held" not in reason:
            continue
        bars = all_bars.get(t["ticker"])
        if bars is None:
            continue
        close = bars.df["close"]
        exit_ts = pd.Timestamp(t["exit_date"])
        hist = close[close.index <= exit_ts]
        fwd = close[close.index > exit_ts]
        entry_price = float(t["entry_price"])
        exit_price = float(t["exit_price"])
        feats = _features_at(hist, entry_price, exit_price)
        fwd_rets = _forward_returns(fwd, exit_price)
        synth = _synthetic_trail_exit(fwd, entry_price)
        rows.append({
            "ticker": t["ticker"],
            "entry_date": t["entry_date"],
            "exit_date": t["exit_date"],
            "realised_gain": t["gain_pct"],
            **feats,
            **fwd_rets,
            **synth,
        })
    return pd.DataFrame(rows)


def _bucket_summary(df: pd.DataFrame, bucket_col: str, ret_col: str = "hold_ret_14d") -> pd.DataFrame:
    rows = []
    for name, sub in df.groupby(bucket_col, observed=True, dropna=False):
        s = sub[ret_col].dropna()
        rows.append({
            "bucket": str(name),
            "n": int(len(sub)),
            f"mean_{ret_col}": float(s.mean()) if not s.empty else float("nan"),
            f"win_rate_{ret_col}": float((s > 0).sum() / len(s)) if len(s) else float("nan"),
        })
    return pd.DataFrame(rows)


def _rule_lift(df: pd.DataFrame, mask: pd.Series, label: str, ret_col: str) -> dict:
    sub = df[mask]
    s = sub[ret_col].dropna()
    return {
        "rule": label,
        "n": int(len(sub)),
        f"mean_{ret_col}": float(s.mean()) if not s.empty else float("nan"),
        f"win_rate_{ret_col}": float((s > 0).sum() / len(s)) if len(s) else float("nan"),
        "synth_mean_return": float(sub["synth_exit_return"].dropna().mean()) if not sub.empty else float("nan"),
    }


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    today = date.today()
    start = today - timedelta(days=180)
    end = today

    print(f"# Hold-Extension Diagnostic: {start} -> {end}\n")
    print(f"TRAIL_PCT (from config) = {TRAIL_PCT*100:.1f}%\n")

    print("Running Bot 3 backtest over the 6-month window...")
    bt = run_backtest(bot_id=3, start_date=start, end_date=end)
    if bt.trades_df.empty:
        print("No trades in window. Nothing to diagnose.")
        return
    print(
        f"Backtest produced {len(bt.trades_df)} trades, "
        f"final equity €{bt.equity_df['total_eur'].iloc[-1]:.2f} "
        f"(return {bt.total_return_pct*100:+.2f}%)\n"
    )

    closed = match_trades(bt.trades_df)
    forced = closed[closed["exit_reason"].str.contains("safety net|max days held", case=False, na=False)]
    print(f"Closed lots: {len(closed)} · forced (safety-net) exits: {len(forced)}\n")
    if forced.empty:
        print("No safety-net exits in this window — graduation rule cannot be calibrated from here.")
        closed.to_csv(OUT_DIR / "closed_trades.csv", index=False)
        return

    print("Fetching 2y of bars for feature calculation...")
    universe = _universe()
    all_bars = market_data.fetch_many(universe, period=FETCH_PERIOD)
    print(f"Got bars for {len(all_bars)}/{len(universe)} tickers.\n")

    per_trade = analyse_forced_exits(closed, all_bars)
    per_trade.to_csv(OUT_DIR / "per_trade.csv", index=False)
    print(f"Per-trade table written to {OUT_DIR / 'per_trade.csv'} ({len(per_trade)} rows)\n")

    if per_trade.empty:
        print("No per-trade rows after feature enrichment (missing bars?).")
        return

    # --- overall forward-hold distribution ---
    print("## Forward returns if we had *kept holding* after the 14-day forced exit\n")
    rows = []
    for h in FWD_HORIZONS:
        col = f"hold_ret_{h}d"
        s = per_trade[col].dropna()
        rows.append({
            "horizon_after_exit": f"+{h}d",
            "n": len(s),
            "mean": float(s.mean()) if not s.empty else float("nan"),
            "median": float(s.median()) if not s.empty else float("nan"),
            "win_rate": float((s > 0).sum() / len(s)) if len(s) else float("nan"),
        })
    print(_to_md(pd.DataFrame(rows)))
    print()

    synth = per_trade["synth_exit_return"].dropna()
    print("## Synthetic outcome: hold & exit on 7% trailing stop from peak\n")
    print(
        f"- n = {len(synth)}\n"
        f"- mean return (from entry) = {synth.mean()*100:+.2f}%\n"
        f"- win rate = {(synth > 0).mean()*100:.1f}%\n"
        f"- mean days-after-exit before trail fired = "
        f"{per_trade['synth_exit_days'].dropna().mean():.1f}\n"
    )
    realised_sum = per_trade["realised_gain"].dropna().sum()
    synth_sum = synth.sum()
    print(
        f"Sum of realised gains (forced exits): {realised_sum*100:+.2f}%  vs "
        f"sum of synthetic-hold gains: {synth_sum*100:+.2f}%\n"
    )

    # --- bucketed analyses ---
    print("## Forward return (hold_ret_14d) bucketed by features at exit\n")

    per_trade["gain_bucket"] = pd.cut(
        per_trade["gain_at_exit"],
        bins=[-1, -0.02, 0.0, 0.03, 0.07, 1.0],
        labels=["<=-2%", "-2..0%", "0..3%", "3..7%", ">=7%"],
    )
    per_trade["rsi_bucket"] = pd.cut(
        per_trade["rsi_at_exit"],
        bins=[0, 40, 55, 70, 100],
        labels=["<40", "40-55", "55-70", ">=70"],
    )

    print("### by gain_at_exit")
    print(_to_md(_bucket_summary(per_trade, "gain_bucket")))
    print("\n### by close > 50-day SMA at exit")
    print(_to_md(_bucket_summary(per_trade, "above_sma50")))
    print("\n### by close > 200-day SMA at exit")
    print(_to_md(_bucket_summary(per_trade, "above_sma200")))
    print("\n### by RSI(14) at exit")
    print(_to_md(_bucket_summary(per_trade, "rsi_bucket")))
    print()

    # --- rule candidates ---
    print("## Candidate 'graduation' rules (hold_ret_14d expectancy)\n")
    rules = [
        _rule_lift(per_trade, pd.Series(True, index=per_trade.index), "baseline (all forced exits)", "hold_ret_14d"),
        _rule_lift(per_trade, per_trade["above_sma50"], "close > 50MA", "hold_ret_14d"),
        _rule_lift(per_trade, per_trade["above_sma200"], "close > 200MA", "hold_ret_14d"),
        _rule_lift(per_trade, per_trade["gain_at_exit"] > 0.03, "gain_at_exit > 3%", "hold_ret_14d"),
        _rule_lift(per_trade, per_trade["gain_at_exit"] > 0.0, "gain_at_exit > 0%", "hold_ret_14d"),
        _rule_lift(
            per_trade,
            per_trade["above_sma50"] & (per_trade["gain_at_exit"] > 0.03),
            "close > 50MA AND gain > 3%",
            "hold_ret_14d",
        ),
        _rule_lift(
            per_trade,
            per_trade["above_sma50"] & (per_trade["gain_at_exit"] > 0.0),
            "close > 50MA AND gain > 0%",
            "hold_ret_14d",
        ),
        _rule_lift(
            per_trade,
            per_trade["above_sma50"] & (per_trade["rsi_at_exit"] < 75),
            "close > 50MA AND RSI < 75",
            "hold_ret_14d",
        ),
        _rule_lift(
            per_trade,
            per_trade["above_sma50"]
            & (per_trade["gain_at_exit"] > 0.03)
            & (per_trade["rsi_at_exit"] < 75),
            "close > 50MA AND gain > 3% AND RSI < 75",
            "hold_ret_14d",
        ),
    ]
    rules_df = pd.DataFrame(rules)
    print(_to_md(rules_df))
    rules_df.to_csv(OUT_DIR / "rules.csv", index=False)
    print(f"\nCSVs written to: {OUT_DIR}")


if __name__ == "__main__":
    main()
