"""Tests for the Strategy-Critic learning-loop feedback.

Covers:
  - measure_all (scripts/measure_rule_changes.py): fills elapsed windows,
    skips not-yet-elapsed ones, and is idempotent.
  - get_proposal_track_record (agents/critic_tools.py): correct batting
    average, excludes unmeasured changes.

The backtest engine is mocked so these tests are network-free and
deterministic — we are testing the loop logic, not yfinance.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from types import SimpleNamespace

import pandas as pd

import agents.critic_tools as ct
from core import db as db_mod
from scripts.measure_rule_changes import measure_all


def _fake_result(return_pct: float) -> SimpleNamespace:
    """Minimal stand-in for BacktestResult as consumed by _summarise()."""
    return SimpleNamespace(
        total_return_pct=return_pct,
        sharpe=float("nan"),
        max_drawdown=0.0,
        trades_df=pd.DataFrame(),
    )


def test_measure_change_forward_delta_returns_none_when_not_elapsed():
    """A window that has not fully elapsed yields None (caller retries later)."""
    delta = ct.measure_change_forward_delta(
        "rsi_compounder", "trail_pct", 0.35, 0.25,
        applied_at=date(2026, 5, 25), window_days=30, today=date(2026, 6, 1),
    )
    assert delta is None


def test_measure_change_forward_delta_none_for_frozen_param():
    """A param not in BOUNDED_RANGES cannot be measured (no override possible)."""
    delta = ct.measure_change_forward_delta(
        "rsi_compounder", "market_filter_ticker", 0.0, 1.0,
        applied_at=date(2026, 1, 1), window_days=30, today=date(2026, 6, 1),
    )
    assert delta is None


def test_measure_all_fills_elapsed_skips_recent_idempotent(db_session, monkeypatch):
    today = date(2026, 6, 1)

    # Applied ~100d ago: both 30d and 90d windows have elapsed.
    elapsed = db_mod.RuleChangeLog(
        proposal_id=1, strategy="rsi_compounder", param_name="trail_pct",
        old_value=0.35, new_value=0.25, applied_at=datetime(2026, 2, 21),
    )
    # Applied ~10d ago: neither window has elapsed.
    recent = db_mod.RuleChangeLog(
        proposal_id=2, strategy="rsi_compounder", param_name="trail_pct",
        old_value=0.20, new_value=0.30, applied_at=datetime(2026, 5, 22),
    )
    db_session.add_all([elapsed, recent])
    db_session.commit()

    # new_value (0.25) → +0.10 return; old_value (0.35) → +0.04 return.
    def fake_backtest(bot_id, start, end, params_override=None):
        v = params_override["trail_pct"]
        if abs(v - 0.25) < 1e-9:
            return _fake_result(0.10)
        if abs(v - 0.35) < 1e-9:
            return _fake_result(0.04)
        return _fake_result(0.0)

    monkeypatch.setattr(ct, "run_backtest", fake_backtest)

    summary = measure_all("rsi_compounder", today=today)

    # Two windows filled on the elapsed row; two skipped on the recent row.
    assert summary["filled"] == 2
    assert summary["skipped_not_elapsed"] == 2
    assert summary["errors"] == 0

    db_session.expire_all()
    rows = {r.id: r for r in db_session.query(db_mod.RuleChangeLog).all()}
    # forward delta = proposed(0.10) - baseline(0.04) = 0.06
    assert rows[elapsed.id].pnl_30d_after == 0.06
    assert rows[elapsed.id].pnl_90d_after == 0.06
    assert rows[recent.id].pnl_30d_after is None
    assert rows[recent.id].pnl_90d_after is None

    # Idempotent: a second run fills nothing new and does not overwrite.
    summary2 = measure_all("rsi_compounder", today=today)
    assert summary2["filled"] == 0
    assert summary2["skipped_not_elapsed"] == 2


def test_get_proposal_track_record_batting_average(db_session):
    # trail_pct: three measured changes, two positive → batting average 2/3.
    for i, d30 in enumerate([0.05, 0.03, -0.02], start=1):
        db_session.add(db_mod.RuleChangeLog(
            proposal_id=i, strategy="rsi_compounder", param_name="trail_pct",
            old_value=0.30, new_value=0.25, applied_at=datetime(2026, 1, i + 1),
            pnl_30d_after=d30,
        ))
    # An unmeasured change (NULL) must be excluded from the batting average.
    db_session.add(db_mod.RuleChangeLog(
        proposal_id=9, strategy="rsi_compounder", param_name="max_days_held",
        old_value=90, new_value=60, applied_at=datetime(2026, 5, 1),
        pnl_30d_after=None,
    ))
    db_session.commit()

    data = json.loads(ct.get_proposal_track_record("rsi_compounder"))

    assert data["n_measured"] == 3
    assert data["overall_batting_average"] == round(2 / 3, 3)

    tp = data["per_param"]["trail_pct"]
    assert tp["n_measured"] == 3
    assert tp["batting_average"] == round(2 / 3, 3)
    assert tp["mean_fwd_delta_30d"] == round((0.05 + 0.03 - 0.02) / 3, 4)

    # The all-NULL param is reported in the change list but not in per_param aggregates.
    assert "max_days_held" not in data["per_param"]
    assert data["n_changes_total"] == 4
