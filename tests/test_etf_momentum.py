"""Strategy tests: ETF momentum ranks + order generation (no network)."""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd

from analysis.market_data import Bars
from analysis.price_signals import momentum_rank, total_return
from core.types import PortfolioSnapshot
from strategies.base import StrategyContext
from strategies.etf_momentum import EtfMomentumStrategy


def _series(start: float, end: float, n: int = 80) -> pd.Series:
    """Linear ramp from start to end, n daily points."""
    idx = pd.date_range("2026-01-01", periods=n, freq="B")
    return pd.Series(np.linspace(start, end, n), index=idx)


def _bars(ticker: str, closes: pd.Series) -> Bars:
    df = pd.DataFrame(
        {
            "open": closes.values,
            "high": closes.values * 1.01,
            "low": closes.values * 0.99,
            "close": closes.values,
            "volume": np.full(len(closes), 1_000_000.0),
        },
        index=closes.index,
    )
    return Bars(ticker=ticker, df=df)


def test_total_return_basic():
    s = _series(100.0, 110.0, n=65)
    # 63-day return over a 65-point series
    r = total_return(s, 63)
    assert abs(r - (float(s.iloc[-1]) / float(s.iloc[-64]) - 1.0)) < 1e-9


def test_momentum_rank_order():
    series = {
        "A": _series(100, 110, 80),   # +10%
        "B": _series(100, 105, 80),   # +5%
        "C": _series(100, 95, 80),    # -5%
    }
    ranked = momentum_rank(series, lookback=63)
    assert [t for t, _ in ranked] == ["A", "B", "C"]


def test_etf_momentum_generates_buys_on_monday():
    bars = {
        "SXR8.DE": _bars("SXR8.DE", _series(100, 120, 80)),
        "SXRV.DE": _bars("SXRV.DE", _series(100, 115, 80)),
        "ZPRR.DE": _bars("ZPRR.DE", _series(100, 110, 80)),
        "CEUG.DE": _bars("CEUG.DE", _series(100, 95, 80)),
    }
    snap = PortfolioSnapshot(bot_id=1, cash_eur=1000.0)
    # First Monday after the test series' start
    monday = date(2026, 1, 5)
    assert monday.weekday() == 0

    ctx = StrategyContext(
        bot_id=1,
        today=monday,
        bars=bars,
        params={
            "universe": "etfs_ucits",
            "lookback_days": 63,
            "top_n": 3,
            "rebalance_weekday": 0,
            "trend_filter": True,
            "min_history_days": 70,
        },
    )
    orders = EtfMomentumStrategy().propose_orders(snap, ctx)

    assert orders, "should have proposed BUYs on Monday"
    tickers = {o.ticker for o in orders}
    assert tickers == {"SXR8.DE", "SXRV.DE", "ZPRR.DE"}
    assert all(o.side.value == "BUY" for o in orders)


def test_etf_momentum_force_rebalance_on_saturday():
    """Weekend run: same logic as Monday when force_rebalance is set."""
    bars = {
        "SXR8.DE": _bars("SXR8.DE", _series(100, 120, 80)),
        "SXRV.DE": _bars("SXRV.DE", _series(100, 115, 80)),
        "ZPRR.DE": _bars("ZPRR.DE", _series(100, 110, 80)),
        "CEUG.DE": _bars("CEUG.DE", _series(100, 95, 80)),
    }
    snap = PortfolioSnapshot(bot_id=1, cash_eur=1000.0)
    saturday = date(2026, 1, 10)
    assert saturday.weekday() == 5

    ctx = StrategyContext(
        bot_id=1,
        today=saturday,
        bars=bars,
        params={
            "universe": "etfs_ucits",
            "lookback_days": 63,
            "top_n": 3,
            "rebalance_weekday": 0,
            "trend_filter": True,
            "min_history_days": 70,
        },
        force_rebalance=True,
    )
    orders = EtfMomentumStrategy().propose_orders(snap, ctx)
    assert orders
    assert {o.ticker for o in orders} == {"SXR8.DE", "SXRV.DE", "ZPRR.DE"}


def test_etf_momentum_skips_non_monday():
    bars = {
        "SXR8.DE": _bars("SXR8.DE", _series(100, 120, 80)),
    }
    snap = PortfolioSnapshot(bot_id=1, cash_eur=1000.0)
    tuesday = date(2026, 1, 6)
    assert tuesday.weekday() == 1
    ctx = StrategyContext(
        bot_id=1, today=tuesday, bars=bars,
        params={
            "universe": "etfs_ucits", "lookback_days": 63, "top_n": 3,
            "rebalance_weekday": 0, "trend_filter": True, "min_history_days": 70,
        },
    )
    assert EtfMomentumStrategy().propose_orders(snap, ctx) == []


def test_etf_momentum_trend_filter_goes_cash():
    bars = {
        "A": _bars("A", _series(100, 90, 80)),
        "B": _bars("B", _series(100, 85, 80)),
        "C": _bars("C", _series(100, 80, 80)),
    }
    snap = PortfolioSnapshot(bot_id=1, cash_eur=1000.0)
    monday = date(2026, 1, 5)
    ctx = StrategyContext(
        bot_id=1, today=monday, bars=bars,
        params={
            "universe": "etfs_ucits", "lookback_days": 63, "top_n": 3,
            "rebalance_weekday": 0, "trend_filter": True, "min_history_days": 70,
        },
    )
    # No existing positions, all negative -> no buys.
    assert EtfMomentumStrategy().propose_orders(snap, ctx) == []
