"""Market regime detection.

Classifies each trading day into one of four regimes based on a reference
index (default: SXR8.DE — S&P 500 UCITS ETF, our standard market filter).

Regimes (priority order, first match wins):
  CRASH      — RSI(14) < 30  OR  drawdown from 52-week high > 20%
  BEAR       — price below SMA200  AND  drawdown > 15%  (sustained downtrend)
  CORRECTION — drawdown 5-15%  OR  RSI(14) < 50
  BULL       — everything else (price above SMA200, RSI > 50, drawdown < 5%)

Typical usage
-------------
>>> from analysis.market_regime import compute_regimes
>>> regimes = compute_regimes("SXR8.DE", start, end)
>>> # returns pd.DataFrame with columns: date, regime, color
"""
from __future__ import annotations

import logging
from datetime import date

import pandas as pd

log = logging.getLogger(__name__)

# Regime labels and their Plotly fill colours (semi-transparent)
REGIME_COLORS: dict[str, str] = {
    "BULL":        "rgba(34, 197, 94, 0.12)",    # green
    "CORRECTION":  "rgba(250, 204, 21, 0.18)",   # yellow
    "CRASH":       "rgba(239, 68, 68, 0.22)",    # red
    "BEAR":        "rgba(127, 29, 29, 0.20)",    # dark red
}

REGIME_LABELS: dict[str, str] = {
    "BULL":        "🟢 Alcista",
    "CORRECTION":  "🟡 Correcció",
    "CRASH":       "🔴 Crash",
    "BEAR":        "⬛ Bajista",
}

_SMA200 = 200
_RSI_PERIOD = 14


def _rsi_series(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    return 100 - (100 / (1 + rs))


def _classify(price: float, sma200: float, rsi: float, drawdown: float) -> str:
    """Return the regime label for a single observation."""
    if rsi < 30 or drawdown < -0.20:
        return "CRASH"
    if price < sma200 and drawdown < -0.15:
        return "BEAR"
    if drawdown < -0.05 or rsi < 50:
        return "CORRECTION"
    return "BULL"


def compute_regimes(
    ticker: str,
    start: date,
    end: date,
) -> pd.DataFrame:
    """Return a DataFrame with daily regime labels for [start, end].

    Columns: date (date), regime (str), color (str), label (str).
    Requires at least 200 trading days of history before *start* to compute
    SMA200 reliably — fetches 2 years of data to guarantee this.

    Returns an empty DataFrame if data cannot be fetched.
    """
    try:
        from analysis.market_data import fetch_bars
        from datetime import datetime

        bars = fetch_bars(ticker, period="2y")
        close = bars.df["close"]

        # Filter to dates up to end
        close = close[close.index <= pd.Timestamp(end)]
        if close.empty or len(close) < _SMA200 + _RSI_PERIOD + 1:
            log.warning("market_regime: not enough data for %s", ticker)
            return pd.DataFrame()

        sma200 = close.rolling(_SMA200, min_periods=_SMA200).mean()
        rsi_vals = _rsi_series(close, _RSI_PERIOD)

        # Rolling 252-day high for drawdown
        rolling_high = close.rolling(252, min_periods=20).max()
        drawdown = close / rolling_high - 1.0

        # Build daily DataFrame
        df = pd.DataFrame({
            "close":    close,
            "sma200":   sma200,
            "rsi":      rsi_vals,
            "drawdown": drawdown,
        }).dropna()

        # Filter to requested window
        df = df[(df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))]
        if df.empty:
            return pd.DataFrame()

        df["regime"] = df.apply(
            lambda r: _classify(r["close"], r["sma200"], r["rsi"], r["drawdown"]),
            axis=1,
        )
        df["color"] = df["regime"].map(REGIME_COLORS)
        df["label"] = df["regime"].map(REGIME_LABELS)
        df["date"] = [idx.date() for idx in df.index]
        df = df.reset_index(drop=True)
        return df[["date", "regime", "color", "label"]]

    except Exception as exc:
        log.warning("market_regime: failed for %s: %s", ticker, exc)
        return pd.DataFrame()


def regime_spans(regime_df: pd.DataFrame) -> list[dict]:
    """Convert a daily-regime DataFrame into a list of contiguous spans.

    Each span is a dict: {x0, x1, regime, color, label}.
    Suitable for Plotly add_vrect() calls.
    """
    if regime_df.empty:
        return []

    spans: list[dict] = []
    prev_regime = None
    span_start = None

    for _, row in regime_df.iterrows():
        if row["regime"] != prev_regime:
            if prev_regime is not None:
                spans.append({
                    "x0":     span_start,
                    "x1":     row["date"],
                    "regime": prev_regime,
                    "color":  REGIME_COLORS[prev_regime],
                    "label":  REGIME_LABELS[prev_regime],
                })
            span_start = row["date"]
            prev_regime = row["regime"]

    # Close the last span
    if prev_regime is not None:
        spans.append({
            "x0":     span_start,
            "x1":     regime_df["date"].iloc[-1],
            "regime": prev_regime,
            "color":  REGIME_COLORS[prev_regime],
            "label":  REGIME_LABELS[prev_regime],
        })

    return spans
