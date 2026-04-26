"""Diagnostic for Bot 3 (Sharp Dip) — why is it losing money?

Runs over the last 3 months of data and produces a markdown report (to stdout)
plus CSVs (under analysis/out/) answering:

  1. Signal forward-return distribution (T+1 / +3 / +5 / +10 / +20).
  2. Exit-reason breakdown from the actual backtest (stop / trail / safety net)
     with MFE (max favorable excursion) vs realized P&L per trade.
  3. Market-regime conditioning — SXR8.DE vs its 50-day & 200-day SMA.
  4. Signal-quality slices — RSI, volume z-score, stock above its own 200MA,
     magnitude of the 5-day drop.
  5. Filter-scenario comparison — which filter combos lift expectancy the most.

Usage: python -m scripts.research.diagnose_sharp_dip
"""
from __future__ import annotations

import io
import sys
from datetime import date, timedelta
from pathlib import Path

# Ensure UTF-8 stdout on Windows so markdown / arrows render cleanly.
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
from analysis.price_signals import (  # noqa: E402
    above_sma,
    consecutive_down_days,
    rsi,
    total_return,
    volume_zscore,
)
from backtesting.engine import run_backtest  # noqa: E402
from core.config import CONFIG  # noqa: E402

# Current sharp_dip thresholds (read from config to stay in sync).
_SD_PARAMS = CONFIG.strategies["strategies"]["sharp_dip"]
CONSEC_MIN = int(_SD_PARAMS.get("consec_down_days", 3))
DROP_5D_PCT = float(_SD_PARAMS.get("drop_5d_pct", 0.05))
MIN_HISTORY = int(_SD_PARAMS.get("min_history_days", 40))

FWD_HORIZONS = (1, 3, 5, 10, 20)
OUT_DIR = _REPO_ROOT / "analysis" / "out"
MKT_TICKER = "SXR8.DE"
FETCH_PERIOD = "2y"  # plenty of history for 200MA at any scan date


def _to_md(df: pd.DataFrame) -> str:
    try:
        return df.to_markdown(index=False, floatfmt=".3f")
    except ImportError:
        return df.to_string(index=False, float_format=lambda x: f"{x:.3f}")


def _universe() -> list[str]:
    wl = CONFIG.watchlists
    tickers = list(wl["stocks_us"]) + list(wl["stocks_eu"])
    if MKT_TICKER not in tickers:
        tickers.append(MKT_TICKER)
    return tickers


def _hit_rate(series: pd.Series) -> float:
    vals = series.dropna()
    if vals.empty:
        return float("nan")
    return float((vals > 0).sum() / len(vals))


def classify_regime(sxr8_close: pd.Series) -> dict:
    """Regime tags based on the latest bar of ``sxr8_close``."""
    r20 = total_return(sxr8_close, 20) if len(sxr8_close) > 20 else float("nan")
    reg50 = "above" if above_sma(sxr8_close, 50) else "below"
    reg200 = "above" if (len(sxr8_close) >= 200 and above_sma(sxr8_close, 200)) else (
        "below" if len(sxr8_close) >= 200 else "unknown"
    )
    return {"regime_50ma": reg50, "regime_200ma": reg200, "regime_20d_ret": r20}


def scan_signals(
    all_bars: dict,
    scan_start: date,
    scan_end: date,
) -> pd.DataFrame:
    """For each (ticker, trading-day) in the window where the sharp-dip rule fires,
    record metadata + forward returns."""
    sxr8_close = all_bars[MKT_TICKER].df["close"]
    rows = []
    for ticker, bars in all_bars.items():
        if ticker == MKT_TICKER:
            continue
        close = bars.df["close"]
        volume = bars.df["volume"] if "volume" in bars.df.columns else pd.Series(dtype=float)
        mask = (close.index >= pd.Timestamp(scan_start)) & (close.index <= pd.Timestamp(scan_end))
        for d in close.index[mask]:
            c_hist = close[close.index <= d]
            if len(c_hist) < MIN_HISTORY:
                continue
            consec = consecutive_down_days(c_hist)
            drop_5d = total_return(c_hist, 5)
            if consec < CONSEC_MIN or np.isnan(drop_5d) or drop_5d > -DROP_5D_PCT:
                continue

            sig_rsi = float(rsi(c_hist, 14).iloc[-1]) if len(c_hist) >= 15 else float("nan")
            v_hist = volume[volume.index <= d] if not volume.empty else pd.Series(dtype=float)
            sig_vol_z = volume_zscore(v_hist, 20) if not v_hist.empty else float("nan")
            sig_above_200 = bool(above_sma(c_hist, 200))

            entry_price = float(c_hist.iloc[-1])
            fwd = close[close.index > d]
            fwd_rets = {}
            for h in FWD_HORIZONS:
                if len(fwd) >= h:
                    fwd_rets[f"ret_{h}d"] = float(fwd.iloc[h - 1]) / entry_price - 1.0
                else:
                    fwd_rets[f"ret_{h}d"] = float("nan")

            regime = classify_regime(sxr8_close[sxr8_close.index <= d])
            rows.append({
                "ticker": ticker,
                "date": d.date(),
                "consec": consec,
                "drop_5d": drop_5d,
                "rsi": sig_rsi,
                "vol_z": sig_vol_z,
                "above_sma200": sig_above_200,
                **regime,
                **fwd_rets,
                "entry_price": entry_price,
            })
    return pd.DataFrame(rows)


def match_trades(trades_df: pd.DataFrame) -> pd.DataFrame:
    """FIFO-match BUYs to SELLs per ticker. Returns one row per closed lot."""
    if trades_df.empty:
        return pd.DataFrame()
    rows = []
    for ticker in trades_df["ticker"].unique():
        t_buys = (
            trades_df[(trades_df["ticker"] == ticker) & (trades_df["side"] == "BUY")]
            .sort_values("date").to_dict("records")
        )
        t_sells = (
            trades_df[(trades_df["ticker"] == ticker) & (trades_df["side"] == "SELL")]
            .sort_values("date").to_dict("records")
        )
        sell_q = list(t_sells)
        for buy in t_buys:
            rem = float(buy["qty"])
            while sell_q and rem > 1e-6:
                sell = sell_q[0]
                matched = min(rem, float(sell["qty"]))
                sell["qty"] = float(sell["qty"]) - matched
                if sell["qty"] < 1e-6:
                    sell_q.pop(0)
                rem -= matched
                entry_price = float(buy["price_eur"])
                exit_price = float(sell["price_eur"])
                rows.append({
                    "ticker": ticker,
                    "entry_date": pd.Timestamp(buy["date"]).date(),
                    "exit_date": pd.Timestamp(sell["date"]).date(),
                    "qty": matched,
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "gain_pct": exit_price / entry_price - 1.0 if entry_price > 0 else float("nan"),
                    "pnl_eur": (exit_price - entry_price) * matched,
                    "exit_reason": str(sell.get("signal_reason", "")),
                })
    return pd.DataFrame(rows)


def bucket_exit_reason(reason: str) -> str:
    r = reason.lower()
    if "stop loss" in r:
        return "stop_loss"
    if "trailing stop" in r:
        return "trailing"
    if "safety net" in r or "max days held" in r:
        return "safety_net"
    return "other"


def compute_mfe(closed_df: pd.DataFrame, all_bars: dict) -> pd.DataFrame:
    """Add max favorable excursion (peak close / entry_price - 1) per closed lot."""
    if closed_df.empty:
        return closed_df
    mfes = []
    for _, t in closed_df.iterrows():
        bars = all_bars.get(t["ticker"])
        if bars is None:
            mfes.append(float("nan"))
            continue
        mask = (
            (bars.df.index >= pd.Timestamp(t["entry_date"]))
            & (bars.df.index <= pd.Timestamp(t["exit_date"]))
        )
        window = bars.df.loc[mask, "close"]
        if window.empty or t["entry_price"] <= 0:
            mfes.append(float("nan"))
        else:
            mfes.append(float(window.max()) / float(t["entry_price"]) - 1.0)
    out = closed_df.copy()
    out["mfe_pct"] = mfes
    return out


def summarize_fwd(signals: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for h in FWD_HORIZONS:
        s = signals[f"ret_{h}d"].dropna()
        rows.append({
            "horizon": f"T+{h}d",
            "n": len(s),
            "mean": float(s.mean()) if not s.empty else float("nan"),
            "median": float(s.median()) if not s.empty else float("nan"),
            "win_rate": _hit_rate(s),
            "p25": float(s.quantile(0.25)) if not s.empty else float("nan"),
            "p75": float(s.quantile(0.75)) if not s.empty else float("nan"),
        })
    return pd.DataFrame(rows)


def summarize_by(signals: pd.DataFrame, col: str, fwd: str = "ret_5d") -> pd.DataFrame:
    """Group signals by ``col`` and report n, mean forward return, win rate."""
    rows = []
    for name, sub in signals.groupby(col, observed=True, dropna=False):
        s = sub[fwd].dropna()
        rows.append({
            "bucket": str(name),
            "n": int(len(sub)),
            f"mean_{fwd}": float(s.mean()) if not s.empty else float("nan"),
            f"win_rate_{fwd}": _hit_rate(s),
        })
    return pd.DataFrame(rows)


def scenario_stats(df: pd.DataFrame, label: str, fwd: str = "ret_5d") -> dict:
    s = df[fwd].dropna()
    return {
        "scenario": label,
        "n": int(len(df)),
        f"mean_{fwd}": float(s.mean()) if not s.empty else float("nan"),
        f"win_rate_{fwd}": _hit_rate(s),
        f"sum_{fwd}": float(s.sum()),  # unlevered "if we took every trade" total
    }


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    today = date.today()
    scan_end = today
    scan_start = today - timedelta(days=90)

    print(f"# Sharp Dip Diagnostic: {scan_start} -> {scan_end}\n")
    print(
        f"Sharp-dip rules in use: consec_down_days >= {CONSEC_MIN}, "
        f"5d drop >= {DROP_5D_PCT*100:.1f}%, min_history = {MIN_HISTORY}.\n"
    )

    # --- Backtest first (it clears the market_data cache at start) ---
    print("Running current-strategy backtest for the 3-month window...")
    try:
        bt = run_backtest(bot_id=3, start_date=scan_start, end_date=scan_end)
    except Exception as exc:
        print(f"  Backtest failed: {exc}")
        bt = None

    # --- Fetch bars AFTER the backtest (backtest clears cache) ---
    print(f"\nFetching {FETCH_PERIOD} of bars for universe...")
    universe = _universe()
    all_bars = market_data.fetch_many(universe, period=FETCH_PERIOD)
    print(f"Got bars for {len(all_bars)}/{len(universe)} tickers.")
    if MKT_TICKER not in all_bars:
        print(f"FATAL: could not fetch market proxy {MKT_TICKER}.")
        return

    # --- Section 1, 3, 4: Signal scan ---
    print("\nScanning for signal fires in the window...")
    signals = scan_signals(all_bars, scan_start, scan_end)
    signals.to_csv(OUT_DIR / "signals.csv", index=False)
    print(f"Found {len(signals)} signal fires across {signals['ticker'].nunique() if not signals.empty else 0} tickers.\n")

    if signals.empty:
        print("No signals fired — can't continue diagnosis. Widen the window or loosen the rules.")
        return

    print("## 1. Forward-return distribution across ALL signals\n")
    print(_to_md(summarize_fwd(signals)))
    print()

    print("## 3. Regime-conditioned forward return (T+5d)\n")
    print("### SPY proxy (SXR8.DE) vs 50-day MA")
    print(_to_md(summarize_by(signals, "regime_50ma")))
    print("\n### SPY proxy (SXR8.DE) vs 200-day MA")
    print(_to_md(summarize_by(signals, "regime_200ma")))
    print()

    print("## 4. Signal-quality slices (T+5d win rate)\n")
    signals["rsi_bucket"] = pd.cut(
        signals["rsi"], bins=[0, 25, 35, 45, 100], labels=["<25", "25-35", "35-45", ">=45"]
    )
    signals["volz_bucket"] = pd.cut(
        signals["vol_z"], bins=[-10, 0.5, 1.5, 3.0, 20],
        labels=["<0.5", "0.5-1.5", "1.5-3.0", ">=3.0"],
    )
    signals["drop_bucket"] = pd.cut(
        signals["drop_5d"], bins=[-1, -0.15, -0.10, -0.07, -0.05],
        labels=["<=-15%", "-15 to -10%", "-10 to -7%", "-7 to -5%"],
    )
    print("### RSI(14) on signal day")
    print(_to_md(summarize_by(signals, "rsi_bucket")))
    print("\n### Volume z-score on signal day")
    print(_to_md(summarize_by(signals, "volz_bucket")))
    print("\n### Stock above its own 200-day MA")
    print(_to_md(summarize_by(signals, "above_sma200")))
    print("\n### 5-day drop magnitude")
    print(_to_md(summarize_by(signals, "drop_bucket")))
    print()

    # --- Section 2: Exit-reason breakdown from backtest ---
    print("## 2. Exit-reason breakdown (current strategy backtest)\n")
    if bt is None or bt.trades_df.empty:
        print("_No trades in backtest window — nothing to break down._\n")
    else:
        closed = match_trades(bt.trades_df)
        closed = compute_mfe(closed, all_bars)
        closed["bucket"] = closed["exit_reason"].apply(bucket_exit_reason)
        closed.to_csv(OUT_DIR / "closed_trades.csv", index=False)
        summary = closed.groupby("bucket").agg(
            n=("pnl_eur", "size"),
            pnl_sum=("pnl_eur", "sum"),
            pnl_mean=("pnl_eur", "mean"),
            gain_pct_mean=("gain_pct", "mean"),
            mfe_mean=("mfe_pct", "mean"),
        ).reset_index()
        print(_to_md(summary))
        total_pnl = closed["pnl_eur"].sum()
        mfe_give_back = (closed["mfe_pct"] - closed["gain_pct"]).mean()
        print(
            f"\nClosed lots: {len(closed)} · total realized P&L: €{total_pnl:+.2f} · "
            f"avg MFE given back: {mfe_give_back*100:+.2f}%"
        )
        print(
            f"Equity at end: €{bt.equity_df['total_eur'].iloc[-1]:.2f} "
            f"(initial €{bt.initial_capital_eur:.2f}, return {bt.total_return_pct*100:+.2f}%)"
        )
    print()

    # --- Section 5: Filter-scenario comparison ---
    print("## 5. Filter-scenario comparison (on signal T+5d returns)\n")
    scenarios = [
        scenario_stats(signals, "baseline (current rules)"),
        scenario_stats(signals[signals["regime_50ma"] == "above"], "+ regime: SPY > 50MA"),
        scenario_stats(signals[signals["regime_200ma"] == "above"], "+ regime: SPY > 200MA"),
        scenario_stats(signals[signals["above_sma200"] == True], "+ stock above its 200MA"),  # noqa: E712
        scenario_stats(signals[signals["vol_z"] > 1.5], "+ volume_zscore > 1.5"),
        scenario_stats(signals[signals["rsi"] < 35], "+ RSI(14) < 35"),
        scenario_stats(
            signals[(signals["regime_50ma"] == "above") & (signals["rsi"] < 35)],
            "+ regime(50MA) + RSI<35",
        ),
        scenario_stats(
            signals[(signals["regime_50ma"] == "above") & (signals["vol_z"] > 1.5)],
            "+ regime(50MA) + vol_z>1.5",
        ),
        scenario_stats(
            signals[(signals["regime_50ma"] == "above") & (signals["above_sma200"] == True)],  # noqa: E712
            "+ regime(50MA) + stock>200MA",
        ),
    ]
    scen_df = pd.DataFrame(scenarios)
    print(_to_md(scen_df))
    scen_df.to_csv(OUT_DIR / "scenarios.csv", index=False)
    print()

    print(f"CSVs written to: {OUT_DIR}")


if __name__ == "__main__":
    main()
