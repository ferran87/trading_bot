"""Tests for the per-run backtest cache + the 2025 window anchor.

The cache eliminates the repeated baseline backtests that simulate_param_change
and walk_forward_validate would otherwise recompute for every hypothesis.
run_backtest is monkeypatched with a counter so we assert call de-duplication
without touching yfinance.
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pandas as pd

import agents.critic_tools as ct


def _fake_result(return_pct: float = 0.05) -> SimpleNamespace:
    return SimpleNamespace(
        total_return_pct=return_pct,
        sharpe=float("nan"),
        max_drawdown=0.0,
        trades_df=pd.DataFrame(),
    )


def test_window_anchor_is_2025():
    assert ct.CRITIC_BACKTEST_START == date(2025, 1, 1)


def test_cached_run_backtest_dedupes_same_key(monkeypatch):
    calls: list[tuple] = []

    def fake_backtest(bot_id, start, end, params_override=None):
        calls.append((bot_id, start, end, tuple(sorted((params_override or {}).items()))))
        return _fake_result()

    monkeypatch.setattr(ct, "run_backtest", fake_backtest)
    ct.clear_backtest_cache()

    s, e = date(2025, 1, 1), date(2025, 6, 1)
    # Same (bot, window, no override) three times → only one real backtest.
    for _ in range(3):
        ct._cached_run_backtest(7, s, e)
    assert len(calls) == 1

    # A different override is a distinct key → one more real backtest.
    ct._cached_run_backtest(7, s, e, params_override={"trail_pct": 0.25})
    ct._cached_run_backtest(7, s, e, params_override={"trail_pct": 0.25})
    assert len(calls) == 2

    # A different window is a distinct key.
    ct._cached_run_backtest(7, s, date(2025, 9, 1))
    assert len(calls) == 3


def test_clear_backtest_cache_forces_recompute(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(ct, "run_backtest",
                        lambda *a, **k: (calls.append(1), _fake_result())[1])
    ct.clear_backtest_cache()

    s, e = date(2025, 1, 1), date(2025, 6, 1)
    ct._cached_run_backtest(7, s, e)
    ct._cached_run_backtest(7, s, e)
    assert len(calls) == 1  # second served from cache

    ct.clear_backtest_cache()
    ct._cached_run_backtest(7, s, e)
    assert len(calls) == 2  # recomputed after clear


def test_cached_run_backtest_unhashable_override_bypasses_cache(monkeypatch):
    """A nested override (e.g. future regime_overrides) isn't hashable — must
    fall back to a direct call instead of raising."""
    calls: list[int] = []
    monkeypatch.setattr(ct, "run_backtest",
                        lambda *a, **k: (calls.append(1), _fake_result())[1])
    ct.clear_backtest_cache()

    nested = {"regime_overrides": {"CORRECTION": {"trail_pct": 0.2}}}
    ct._cached_run_backtest(7, date(2025, 1, 1), date(2025, 6, 1), params_override=nested)
    ct._cached_run_backtest(7, date(2025, 1, 1), date(2025, 6, 1), params_override=nested)
    # No caching for unhashable keys → both are real calls, and neither raises.
    assert len(calls) == 2
