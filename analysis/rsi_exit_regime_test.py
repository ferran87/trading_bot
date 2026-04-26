"""Exit strategy regime test — COVID recovery into 2022 bear market.

Scenario: stocks crash in March 2020 (RSI < 25), recover in April-May 2020
(RSI > 40), rally hard through 2021 (RSI gets overbought), then reverse
sharply in 2022 (-30% to -70%). This is the stress test for Option C.

Three exit options applied to every qualifying entry signal:
  A  RSI take-profit 70, trailing stop 35%
  B  RSI take-profit 80, trailing stop 35%
  C  No RSI take-profit; trailing stop tightens progressively:
       RSI < 70  -> 35% trail
       RSI 70-80 -> 20% trail
       RSI > 80  -> 12% trail

Also tests the 2022 crash entries (Oct 2022 bottom -> 2023 recovery -> 2024
bull market) as a second regime.

Run: .venv/Scripts/python.exe -m analysis.rsi_exit_regime_test
"""
from __future__ import annotations

import sys
from datetime import date, timedelta

sys.path.insert(0, ".")

import pandas as pd
import yfinance as yf

from analysis.price_signals import rsi as compute_rsi
from core.config import CONFIG

RSI_PERIOD   = 14
CATASTROPHIC = -0.40
RSI_WAS_BELOW   = 25.0
RSI_NOW_ABOVE   = 40.0
RSI_LOOKBACK    = 15    # days
MKT_RSI_BELOW   = 30.0

TRAIL_A = 0.35
RSI_TP_A = 70.0
TRAIL_B = 0.35
RSI_TP_B = 80.0

def trail_c(rsi_val: float) -> float:
    if rsi_val >= 80: return 0.12
    if rsi_val >= 70: return 0.20
    return 0.35


REGIMES = [
    {
        "name": "COVID crash recovery (Apr 2020) into 2022 bear market",
        "data_start":  date(2019, 1, 1),
        "signal_from": date(2020, 3, 15),
        "signal_to":   date(2020, 6, 30),
        "sim_end":     date(2023, 1, 31),
    },
    {
        "name": "2022 bear market bottom (Oct 2022) into 2024 bull market",
        "data_start":  date(2021, 1, 1),
        "signal_from": date(2022, 9, 1),
        "signal_to":   date(2023, 2, 28),
        "sim_end":     date(2024, 12, 31),
    },
]

TICKERS = (
    list(CONFIG.watchlists["stocks_us"])
    + list(CONFIG.watchlists["stocks_eu"])
    + list(CONFIG.watchlists["etfs_ucits"])
)
MKT_TICKER = "SXR8.DE"


def fetch(ticker: str, start: date, end: date) -> pd.DataFrame | None:
    try:
        df = yf.download(ticker, start=start, end=end + timedelta(days=1),
                         auto_adjust=True, progress=False, threads=False)
        if df is None or df.empty:
            return None
        df = df.rename(columns=str.lower)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0] for c in df.columns]
        df.index = pd.to_datetime(df.index).tz_localize(None)
        return df
    except Exception:
        return None


def rsi_min_recent(close: pd.Series, as_of_idx: int, lookback: int) -> float:
    """Min RSI over `lookback` bars ending one bar before as_of_idx."""
    window_start = as_of_idx - lookback
    if window_start < 0:
        return float("nan")
    rsi_series = compute_rsi(close.iloc[:as_of_idx], RSI_PERIOD)
    window = rsi_series.iloc[-lookback:]
    if window.empty:
        return float("nan")
    return float(window.min())


def simulate_exit(
    close: pd.Series,
    rsi_series: pd.Series,
    entry_idx: int,
    entry_price: float,
    option: str,
) -> dict:
    peak = entry_price
    for i in range(entry_idx, len(close)):
        price = float(close.iloc[i])
        gain  = price / entry_price - 1.0
        ts    = close.index[i]
        rsi_val = float(rsi_series.iloc[i]) if i < len(rsi_series) else float("nan")

        if gain <= CATASTROPHIC:
            return {"exit_date": ts.date(), "price": price,
                    "reason": "catastrophic", "gain_pct": gain * 100}

        if rsi_val == rsi_val:
            if option == "A" and rsi_val >= RSI_TP_A:
                return {"exit_date": ts.date(), "price": price,
                        "reason": "rsi_tp_70", "gain_pct": gain * 100}
            if option == "B" and rsi_val >= RSI_TP_B:
                return {"exit_date": ts.date(), "price": price,
                        "reason": "rsi_tp_80", "gain_pct": gain * 100}

        if gain > 0:
            peak = max(peak, price)
            active_trail = (trail_c(rsi_val) if option == "C" and rsi_val == rsi_val
                            else (TRAIL_A if option == "A" else TRAIL_B))
            if price / peak - 1.0 <= -active_trail:
                label = f"trail_{int(active_trail*100)}pct"
                return {"exit_date": ts.date(), "price": price,
                        "reason": label, "gain_pct": gain * 100}

    last = float(close.iloc[-1])
    return {"exit_date": close.index[-1].date(), "price": last,
            "reason": "held_to_end", "gain_pct": (last / entry_price - 1.0) * 100}


def run_regime(regime: dict) -> None:
    name       = regime["name"]
    data_start = regime["data_start"]
    sig_from   = regime["signal_from"]
    sig_to     = regime["signal_to"]
    sim_end    = regime["sim_end"]

    print(f"\n{'='*80}")
    print(f"REGIME: {name}")
    print(f"Signal window: {sig_from} to {sig_to}  |  Hold until: {sim_end}")
    print(f"{'='*80}")

    # Fetch market filter data
    mkt_df = fetch(MKT_TICKER, data_start, sim_end)
    if mkt_df is None:
        print(f"Could not fetch {MKT_TICKER}")
        return
    mkt_close = mkt_df["close"]
    mkt_rsi   = compute_rsi(mkt_close, RSI_PERIOD)

    # Fetch all tickers
    all_data: dict[str, pd.DataFrame] = {}
    print(f"Fetching {len(TICKERS)} tickers ({data_start} to {sim_end})...")
    for t in TICKERS:
        df = fetch(t, data_start, sim_end)
        if df is not None and len(df) > RSI_PERIOD + RSI_LOOKBACK + 5:
            all_data[t] = df

    print(f"Got data for {len(all_data)} tickers.")

    # Scan for entry signals in the signal window
    entries = []
    for ticker, df in all_data.items():
        close     = df["close"]
        rsi_full  = compute_rsi(close, RSI_PERIOD)

        for i, ts in enumerate(close.index):
            d = ts.date()
            if d < sig_from or d > sig_to:
                continue
            if i < RSI_PERIOD + RSI_LOOKBACK + 1:
                continue

            # Market co-crash check
            mkt_idx = mkt_rsi.index.get_indexer([ts], method="ffill")[0]
            if mkt_idx < RSI_LOOKBACK:
                continue
            mkt_window = mkt_rsi.iloc[max(0, mkt_idx - RSI_LOOKBACK):mkt_idx]
            if mkt_window.empty or float(mkt_window.min()) >= MKT_RSI_BELOW:
                continue

            # RSI now above threshold
            rsi_now = float(rsi_full.iloc[i])
            if rsi_now != rsi_now or rsi_now <= RSI_NOW_ABOVE:
                continue

            # RSI was below threshold recently
            rsi_min = rsi_min_recent(close, i, RSI_LOOKBACK)
            if rsi_min != rsi_min or rsi_min >= RSI_WAS_BELOW:
                continue

            # Valid entry
            entries.append({
                "ticker":      ticker,
                "entry_date":  d,
                "entry_idx":   i,
                "entry_price": float(close.iloc[i]),
                "close":       close,
                "rsi":         rsi_full,
            })

    # Deduplicate: one entry per ticker (first signal)
    seen: set[str] = set()
    unique_entries = []
    for e in sorted(entries, key=lambda x: x["entry_date"]):
        if e["ticker"] not in seen:
            seen.add(e["ticker"])
            unique_entries.append(e)

    print(f"Qualifying entry signals: {len(unique_entries)}")
    if not unique_entries:
        print("No signals found — market filter may not have been met.")
        return

    rows = []
    for e in unique_entries:
        sim_a = simulate_exit(e["close"], e["rsi"], e["entry_idx"], e["entry_price"], "A")
        sim_b = simulate_exit(e["close"], e["rsi"], e["entry_idx"], e["entry_price"], "B")
        sim_c = simulate_exit(e["close"], e["rsi"], e["entry_idx"], e["entry_price"], "C")
        rows.append({
            "ticker":     e["ticker"],
            "entry_date": e["entry_date"],
            "gain_A":     round(sim_a["gain_pct"], 1),
            "reason_A":   sim_a["reason"],
            "gain_B":     round(sim_b["gain_pct"], 1),
            "reason_B":   sim_b["reason"],
            "gain_C":     round(sim_c["gain_pct"], 1),
            "reason_C":   sim_c["reason"],
            "B_vs_A":     round(sim_b["gain_pct"] - sim_a["gain_pct"], 1),
            "C_vs_A":     round(sim_c["gain_pct"] - sim_a["gain_pct"], 1),
        })

    df_out = pd.DataFrame(rows)
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 160)
    pd.set_option("display.max_rows", 80)
    print(df_out[["ticker","entry_date","gain_A","reason_A","gain_B","reason_B",
                  "gain_C","reason_C","B_vs_A","C_vs_A"]].to_string(index=False))

    print(f"\n--- AGGREGATE ---")
    n = len(df_out)
    for opt, label in [("A","A RSI TP=70 (current)      "),
                        ("B","B RSI TP=80               "),
                        ("C","C Progressive trail (no TP)")]:
        avg  = df_out[f"gain_{opt}"].mean()
        med  = df_out[f"gain_{opt}"].median()
        wins = (df_out[f"gain_{opt}"] > 0).sum()
        print(f"  {label}  avg={avg:+.1f}%  median={med:+.1f}%  winners={wins}/{n}")

    print(f"\n  Delta vs A:")
    for opt in ["B","C"]:
        col = f"{opt}_vs_A"
        better = (df_out[col] > 0).sum()
        worse  = (df_out[col] < 0).sum()
        print(f"    {opt} vs A:  avg={df_out[col].mean():+.1f}%  "
              f"better={better}  worse={worse}")

    print(f"\n  Exit reason breakdown:")
    for opt, label in [("A","Option A"),("B","Option B"),("C","Option C")]:
        col = f"reason_{opt}"
        print(f"  {label}:")
        for reason, cnt in df_out[col].value_counts().items():
            avg_g = df_out[df_out[col] == reason][f"gain_{opt}"].mean()
            print(f"    {reason:<35} {cnt:>2}x  avg {avg_g:+.1f}%")


if __name__ == "__main__":
    for regime in REGIMES:
        run_regime(regime)
