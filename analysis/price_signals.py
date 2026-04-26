"""Price-derived signals: momentum, RSI, volume z-score, overnight gap.

All pure functions of pandas DataFrames — no I/O. Easy to unit-test.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def total_return(close: pd.Series, lookback: int) -> float:
    """Total return over the last `lookback` trading days.

    Returns NaN if the series is shorter than `lookback + 1`.
    """
    if len(close) < lookback + 1:
        return float("nan")
    start = float(close.iloc[-lookback - 1])
    end = float(close.iloc[-1])
    if start <= 0:
        return float("nan")
    return end / start - 1.0


def momentum_rank(
    closes_by_ticker: dict[str, pd.Series],
    lookback: int,
) -> list[tuple[str, float]]:
    """Return [(ticker, total_return), ...] sorted desc. NaNs dropped.

    Ties broken alphabetically by ticker for determinism.
    """
    rows = []
    for ticker, series in closes_by_ticker.items():
        r = total_return(series, lookback)
        if not np.isnan(r):
            rows.append((ticker, r))
    rows.sort(key=lambda x: (-x[1], x[0]))
    return rows


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def volume_zscore(volume: pd.Series, window: int = 20) -> float:
    """z-score of the latest volume vs the trailing `window` mean/std."""
    if len(volume) < window + 1:
        return float("nan")
    tail = volume.iloc[-window - 1 : -1]
    mean = float(tail.mean())
    std = float(tail.std(ddof=0))
    if std == 0:
        return float("nan")
    return (float(volume.iloc[-1]) - mean) / std


def consecutive_down_days(close: pd.Series) -> int:
    """Count consecutive sessions where close fell vs the prior close (ending at iloc[-1])."""
    if len(close) < 2:
        return 0
    count = 0
    for i in range(len(close) - 1, 0, -1):
        if float(close.iloc[i]) < float(close.iloc[i - 1]):
            count += 1
        else:
            break
    return count


def above_sma(close: pd.Series, period: int) -> bool:
    """True if the latest close is above the `period`-day simple moving average.

    Returns False (conservative) when there isn't enough history.
    """
    if len(close) < period:
        return False
    sma = float(close.iloc[-period:].mean())
    return float(close.iloc[-1]) > sma


def overnight_gap(open_: pd.Series, close: pd.Series) -> float:
    """(today's open - yesterday's close) / yesterday's close."""
    if len(open_) < 1 or len(close) < 2:
        return float("nan")
    prev_close = float(close.iloc[-2])
    today_open = float(open_.iloc[-1])
    if prev_close <= 0:
        return float("nan")
    return today_open / prev_close - 1.0
