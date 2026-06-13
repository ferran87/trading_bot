"""Tools for the Strategy Critic agent.

Each tool here is exposed to Claude via a JSON schema in
``agents/strategy_critic.py``. The functions return strings (the agent loop
expects string tool results), but internally they work with native Python
types and serialise at the boundary.

Anti-overfitting guardrails live here, not in the prompt:

  - ``BOUNDED_RANGES``      restricts which params can move and by how much
  - ``walk_forward_validate`` splits history 70/30 so Claude can never validate
                              a proposal on the same data it analysed
  - ``compute_ratchet``     a proposal must improve return AND not worsen
                              max_drawdown by more than the allowed slack

The agent CAN make a proposal that fails the ratchet, but the dashboard
filters those out by default — Claude's "track record" is still scored
against ratchet-passing proposals only.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from backtesting.engine import BacktestResult, run_backtest
from core.config import CONFIG
from core.db import (
    Bot,
    RuleChangeLog,
    SimulatedClosedPosition,
    Trade,
    get_session,
)

log = logging.getLogger(__name__)

# Strategy → bot id (matches scripts/bootstrap_strategy_lab.py)
STRATEGY_BOTS: dict[str, int] = {
    "rsi_compounder": 7,
    "trend_momentum": 10,
}


# ── Bounded ranges per param ──────────────────────────────────────────────
#
# Format:  param_name → (min_value, max_value, step_hint)
# Any proposed_value outside [min, max] is rejected before being persisted.
# The step_hint is informational for Claude, not enforced.
#
# Add a new entry here to "open" a parameter for tuning. Anything not in this
# dict is FROZEN — the critic cannot propose changes to it.

BOUNDED_RANGES: dict[str, tuple[float, float, float]] = {
    # ── RSI thresholds (integer-ish, treated as floats) ─────────
    "rsi_was_below":              (15.0, 35.0,  1.0),
    "rsi_now_above":              (30.0, 55.0,  1.0),
    "rsi_entry_max":              (55.0, 75.0,  1.0),
    "rsi_entry_min":              (30.0, 50.0,  1.0),
    "rsi_period":                 (10.0, 21.0,  1.0),
    "rsi_lookback_days":          ( 5.0, 30.0,  1.0),
    "rsi_take_profit":            (60.0, 80.0,  1.0),
    "rsi_trail_mid":              (60.0, 80.0,  1.0),
    "rsi_trail_tight":            (75.0, 90.0,  1.0),
    "rsi_momentum_days":          ( 1.0,  7.0,  1.0),
    "market_rsi_was_below":       (20.0, 40.0,  1.0),
    "market_rsi_lookback_days":   ( 5.0, 30.0,  1.0),

    # ── Trailing stops (decimals, e.g. 0.20 = 20%) ──────────────
    "trail_pct":                  (0.10, 0.45, 0.01),
    "trail_pct_mid":              (0.08, 0.30, 0.01),
    "trail_pct_tight":            (0.05, 0.20, 0.01),
    "long_trail_pct":             (0.10, 0.45, 0.01),

    # ── Hard stops & adds (negative = below entry) ──────────────
    "stop_loss_pct":              (0.03, 0.15, 0.01),
    "catastrophic_stop":          (-0.50, -0.10, 0.01),
    "add_at_loss_1":              (-0.15, -0.04, 0.01),
    "add_at_loss_2":              (-0.25, -0.10, 0.01),

    # ── Sizing (decimals) ────────────────────────────────────────
    "per_position_pct":           (0.04, 0.30, 0.005),
    "max_concurrent":             ( 5.0, 15.0,  1.0),
    "max_adds_per_ticker":        ( 0.0,  3.0,  1.0),

    # ── Time / horizon ──────────────────────────────────────────
    "max_days_held":              (20.0, 180.0, 1.0),
    "graduate_min_days":          ( 3.0, 21.0,  1.0),
    "earnings_blackout_days":     ( 0.0, 14.0,  1.0),
    "trend_break_days":           ( 1.0,  7.0,  1.0),

    # ── SMA / lookback ──────────────────────────────────────────
    "sma_period":                 (20.0, 100.0, 1.0),
    "market_sma_period":          (100.0, 250.0, 1.0),
    "lookback_days":              (10.0, 90.0,  1.0),

    # ── trend_momentum specific ─────────────────────────────────
    "rotation_loss_threshold":    (-0.10, -0.01, 0.005),
}


# Maximum proposals per strategy per critic run (defends against agent flooding)
MAX_PROPOSALS_PER_STRATEGY = 3


# Acceptable max_drawdown worsening (in percentage points) before ratchet
# rejects a proposal. 2pp = if baseline max_dd is -8%, proposed must be no
# worse than -10%.
RATCHET_MAX_DD_SLACK_PP = 0.02


# Anchor for the critic's backtest/walk-forward window. Shorter window = faster
# runs (each backtest replays day-by-day from this date). Tradeoff: a tighter
# 70/30 walk-forward split and fewer in-window trades make validation noisier —
# revert to date(2024, 1, 1) if proposals start looking flimsy.
CRITIC_BACKTEST_START = date(2025, 1, 1)


# ── Per-run backtest cache ──────────────────────────────────────────────────
#
# The critic evaluates each hypothesis with simulate_param_change +
# walk_forward_validate, and the BASELINE backtest (no override) is identical
# across every hypothesis for a strategy — yet was recomputed each time, and
# each run_backtest re-downloads the whole universe (it calls
# market_data.clear_cache() internally). Memoising by (bot_id, start, end,
# overrides) eliminates that waste. Scoped to one critic run: clear_backtest_cache()
# is called at the start of run_critic_for_strategy so we never serve a result
# stale w.r.t. changed YAML params or refreshed market data across runs.

_BACKTEST_CACHE: dict[tuple, BacktestResult] = {}


def clear_backtest_cache() -> None:
    """Drop all memoised backtests. Call once at the start of each critic run."""
    _BACKTEST_CACHE.clear()


def _cached_run_backtest(
    bot_id: int,
    start: date,
    end: date,
    params_override: dict | None = None,
) -> BacktestResult:
    """run_backtest() with per-run memoization. Falls back to a direct call if
    the override isn't hashable (e.g. a nested regime_overrides dict)."""
    try:
        key = (
            bot_id, start.isoformat(), end.isoformat(),
            tuple(sorted((params_override or {}).items())),
        )
        hit = _BACKTEST_CACHE.get(key)  # hashing the key happens here
    except TypeError:
        return run_backtest(bot_id, start, end, params_override=params_override)
    if hit is None:
        hit = run_backtest(bot_id, start, end, params_override=params_override)
        _BACKTEST_CACHE[key] = hit
    return hit


@dataclass
class BacktestSummary:
    """Light JSON-serialisable view of a BacktestResult — what Claude sees."""

    return_pct: float
    sharpe: float
    max_drawdown_pct: float
    n_trades: int
    win_rate_pct: float | None = None
    period: str = ""

    def to_dict(self) -> dict:
        return {
            "return_pct":       round(self.return_pct, 4),
            "sharpe":           None if self._isnan(self.sharpe) else round(self.sharpe, 3),
            "max_drawdown_pct": round(self.max_drawdown_pct, 4),
            "n_trades":         int(self.n_trades),
            "win_rate_pct":     None if self.win_rate_pct is None else round(self.win_rate_pct, 2),
            "period":           self.period,
        }

    @staticmethod
    def _isnan(x: float) -> bool:
        return x != x  # NaN is the only float that fails reflexive equality


def _summarise(result: BacktestResult, period_label: str = "") -> BacktestSummary:
    win_rate: float | None = None
    if not result.trades_df.empty:
        # Approximate win rate from SELL trades that net out to positive
        # (full P&L attribution per round-trip lives in
        # SimulatedClosedPosition; for live backtests we use this proxy)
        sells = result.trades_df[result.trades_df["side"] == "SELL"]
        if not sells.empty:
            # Without round-trip reconstruction here we just report total trades
            win_rate = None
    return BacktestSummary(
        return_pct=result.total_return_pct,
        sharpe=result.sharpe,
        max_drawdown_pct=result.max_drawdown,
        n_trades=int(len(result.trades_df)),
        win_rate_pct=win_rate,
        period=period_label,
    )


# ── Tool 1: get_simulated_closed_positions ────────────────────────────────

def get_simulated_closed_positions(
    strategy: str,
    limit: int = 200,
    latest_run_only: bool = True,
) -> str:
    """Return closed positions from the bootstrap backtest as JSON.

    Each row has ticker, dates, return, exit_reason, regime tags. Use this
    to spot patterns (e.g. "trailing stops fire too early in BULL regime").
    """
    with get_session() as s:
        q = s.query(SimulatedClosedPosition).filter(
            SimulatedClosedPosition.strategy == strategy,
        )
        if latest_run_only:
            latest_id = (
                s.query(SimulatedClosedPosition.backtest_run_id)
                .filter(SimulatedClosedPosition.strategy == strategy)
                .order_by(SimulatedClosedPosition.created_at.desc())
                .limit(1)
                .scalar()
            )
            if latest_id:
                q = q.filter(SimulatedClosedPosition.backtest_run_id == latest_id)

        rows = q.order_by(SimulatedClosedPosition.exit_date.desc()).limit(limit).all()

    out = [
        {
            "ticker":             r.ticker,
            "entry_date":         r.entry_date.isoformat(),
            "exit_date":          r.exit_date.isoformat(),
            "hold_days":          r.hold_days,
            "return_pct":         round(r.return_pct, 4),
            "max_unrealized_gain_pct": round(r.max_unrealized_gain_pct, 4),
            "max_drawdown_pct":   round(r.max_drawdown_pct, 4),
            "exit_reason":        r.exit_reason,
            "regime_at_entry":    r.regime_at_entry,
            "regime_at_exit":     r.regime_at_exit,
        }
        for r in rows
    ]
    return json.dumps({"strategy": strategy, "n": len(out), "rows": out})


# ── Tool 2: get_real_closed_positions ─────────────────────────────────────

def get_real_closed_positions(strategy: str, lookback_days: int = 365) -> str:
    """Reconstruct closed round-trips from the live trades ledger.

    Returns recent real-world closed positions (the ones the live bot has
    actually executed). Useful once enough live history accumulates;
    for now this will be empty or sparse.
    """
    bot_id = STRATEGY_BOTS.get(strategy)
    if bot_id is None:
        return json.dumps({"error": f"unknown strategy {strategy!r}"})

    cutoff = datetime.now() - timedelta(days=lookback_days)
    with get_session() as s:
        trades = (
            s.query(Trade)
            .filter(Trade.bot_id == bot_id, Trade.timestamp >= cutoff,
                    Trade.status == "filled")
            .order_by(Trade.timestamp)
            .all()
        )

    if not trades:
        return json.dumps({"strategy": strategy, "n": 0, "rows": [],
                           "note": "no real trades yet — strategy_critic should rely on simulated_closed_positions"})

    # Lightweight round-trip reconstruction (mirrors bootstrap script logic)
    state: dict[str, dict] = {}
    closed: list[dict] = []
    for t in trades:
        st = state.setdefault(t.ticker, {"qty": 0.0, "cost": 0.0, "fees": 0.0,
                                          "entry_date": None, "exit_proceeds": 0.0,
                                          "exit_qty": 0.0, "last_reason": ""})
        if t.side == "BUY":
            if st["qty"] < 1e-9:
                st["entry_date"] = t.timestamp.date()
            st["qty"]  += t.qty
            st["cost"] += t.qty * t.price_eur
            st["fees"] += t.fee_eur
        elif t.side == "SELL":
            if st["qty"] < 1e-9:
                continue
            st["qty"]          -= t.qty
            st["fees"]         += t.fee_eur
            st["exit_proceeds"] += t.qty * t.price_eur
            st["exit_qty"]      += t.qty
            st["last_reason"]   = t.signal_reason or ""
            if st["qty"] <= 1e-6:
                bought_qty = st["exit_qty"]
                avg_entry = st["cost"] / bought_qty if bought_qty > 0 else 0
                avg_exit  = st["exit_proceeds"] / st["exit_qty"] if st["exit_qty"] else 0
                ret_pct   = (avg_exit / avg_entry - 1.0) if avg_entry > 0 else 0
                closed.append({
                    "ticker":      t.ticker,
                    "entry_date":  st["entry_date"].isoformat() if st["entry_date"] else None,
                    "exit_date":   t.timestamp.date().isoformat(),
                    "return_pct":  round(ret_pct, 4),
                    "exit_reason": st["last_reason"][:100],
                })
                state[t.ticker] = {"qty": 0, "cost": 0, "fees": 0, "entry_date": None,
                                   "exit_proceeds": 0, "exit_qty": 0, "last_reason": ""}

    return json.dumps({"strategy": strategy, "n": len(closed), "rows": closed})


# ── Tool 3: get_strategy_params ───────────────────────────────────────────

def get_strategy_params(strategy: str) -> str:
    """Return the current strategy params + which ones are tunable.

    Tunable params show their allowed range so Claude knows the bounds.
    Frozen params (not in BOUNDED_RANGES) are listed as "frozen=true".
    """
    try:
        params = CONFIG.strategies["strategies"][strategy]
    except KeyError:
        return json.dumps({"error": f"unknown strategy {strategy!r}"})

    out = {}
    for k, v in params.items():
        # Skip non-numeric (universe is a list, etc.)
        if not isinstance(v, (int, float)):
            continue
        rng = BOUNDED_RANGES.get(k)
        out[k] = {
            "current_value": float(v),
            "tunable":       rng is not None,
            "min":           rng[0] if rng else None,
            "max":           rng[1] if rng else None,
            "step":          rng[2] if rng else None,
        }
    return json.dumps({"strategy": strategy, "params": out})


# ── Tool 4: simulate_param_change ─────────────────────────────────────────

def simulate_param_change(
    strategy: str,
    param_overrides: dict,
    start: str | None = None,
    end: str | None = None,
) -> str:
    """Run a backtest with the given overrides; return a summary.

    Returns both the BASELINE summary (current params) and the PROPOSED
    summary (with overrides) plus a diff and the ratchet verdict — so Claude
    sees the trade-off in a single tool call.

    Default period: 2024-01-01 → today.
    """
    bot_id = STRATEGY_BOTS.get(strategy)
    if bot_id is None:
        return json.dumps({"error": f"unknown strategy {strategy!r}"})

    # Validate every override against BOUNDED_RANGES
    invalid = _validate_overrides(param_overrides)
    if invalid:
        return json.dumps({"error": "invalid overrides", "details": invalid})

    s_date = date.fromisoformat(start) if start else CRITIC_BACKTEST_START
    e_date = date.fromisoformat(end)   if end   else date.today()

    log.info("simulate_param_change(%s) baseline...", strategy)
    base = _summarise(_cached_run_backtest(bot_id, s_date, e_date), period_label=f"{s_date}→{e_date}")
    log.info("simulate_param_change(%s) proposed %s...", strategy, param_overrides)
    prop = _summarise(
        _cached_run_backtest(bot_id, s_date, e_date, params_override=param_overrides),
        period_label=f"{s_date}→{e_date}",
    )

    return json.dumps({
        "strategy":        strategy,
        "param_overrides": param_overrides,
        "baseline":        base.to_dict(),
        "proposed":        prop.to_dict(),
        "delta": {
            "return_pct":       round(prop.return_pct - base.return_pct, 4),
            "max_drawdown_pct": round(prop.max_drawdown_pct - base.max_drawdown_pct, 4),
            "n_trades":         int(prop.n_trades - base.n_trades),
        },
        "passes_ratchet":  compute_ratchet(base.to_dict(), prop.to_dict()),
    })


# ── Tool 5: walk_forward_validate ─────────────────────────────────────────

def walk_forward_validate(
    strategy: str,
    param_overrides: dict,
    train_pct: float = 0.7,
) -> str:
    """Split available history train/test, return both summaries.

    Default split: 70% in-sample (training period the agent looked at), 30%
    out-of-sample (held-out). A proposal that looks great in-sample but
    poor out-of-sample is overfit.

    Window is anchored to 2024-01-01 → today (matches bootstrap default).
    """
    bot_id = STRATEGY_BOTS.get(strategy)
    if bot_id is None:
        return json.dumps({"error": f"unknown strategy {strategy!r}"})

    invalid = _validate_overrides(param_overrides)
    if invalid:
        return json.dumps({"error": "invalid overrides", "details": invalid})

    full_start = CRITIC_BACKTEST_START
    full_end   = date.today()
    total_days = (full_end - full_start).days
    split_day  = full_start + timedelta(days=int(total_days * train_pct))

    log.info("walk_forward(%s) train %s→%s test %s→%s",
             strategy, full_start, split_day, split_day, full_end)

    train_baseline = _summarise(
        _cached_run_backtest(bot_id, full_start, split_day),
        period_label=f"train {full_start}→{split_day}",
    )
    train_proposed = _summarise(
        _cached_run_backtest(bot_id, full_start, split_day, params_override=param_overrides),
        period_label=f"train {full_start}→{split_day}",
    )
    test_baseline = _summarise(
        _cached_run_backtest(bot_id, split_day, full_end),
        period_label=f"test {split_day}→{full_end}",
    )
    test_proposed = _summarise(
        _cached_run_backtest(bot_id, split_day, full_end, params_override=param_overrides),
        period_label=f"test {split_day}→{full_end}",
    )

    train_delta = train_proposed.return_pct - train_baseline.return_pct
    test_delta  = test_proposed.return_pct  - test_baseline.return_pct

    # Overfitting flag: in-sample improvement >> out-of-sample improvement
    # (or test went the wrong way entirely)
    overfit = train_delta > 0 and (test_delta < 0 or test_delta < 0.5 * train_delta)

    return json.dumps({
        "strategy":        strategy,
        "param_overrides": param_overrides,
        "train": {
            "baseline": train_baseline.to_dict(),
            "proposed": train_proposed.to_dict(),
            "delta_return_pct": round(train_delta, 4),
        },
        "test": {
            "baseline": test_baseline.to_dict(),
            "proposed": test_proposed.to_dict(),
            "delta_return_pct": round(test_delta, 4),
        },
        "overfit_flag":   overfit,
        "passes_ratchet": compute_ratchet(test_baseline.to_dict(), test_proposed.to_dict()),
    })


# ── Helpers ────────────────────────────────────────────────────────────────

def _validate_overrides(overrides: dict) -> list[str]:
    """Return list of error strings (empty = all OK)."""
    errors: list[str] = []
    for name, value in overrides.items():
        if not isinstance(value, (int, float)):
            errors.append(f"{name}: must be numeric, got {type(value).__name__}")
            continue
        rng = BOUNDED_RANGES.get(name)
        if rng is None:
            errors.append(f"{name}: not in BOUNDED_RANGES (frozen — Strategy Critic cannot tune this)")
            continue
        lo, hi, _ = rng
        if not (lo <= float(value) <= hi):
            errors.append(f"{name}={value}: out of bounds [{lo}, {hi}]")
    return errors


def compute_ratchet(baseline: dict, proposed: dict) -> bool:
    """Return True iff proposed improves return AND max_dd does not worsen
    by more than RATCHET_MAX_DD_SLACK_PP percentage points.

    Both inputs are dicts from BacktestSummary.to_dict().
    """
    try:
        ret_better = proposed["return_pct"] > baseline["return_pct"]
        # max_drawdown_pct is negative; "worse" means more negative
        dd_delta   = proposed["max_drawdown_pct"] - baseline["max_drawdown_pct"]
        dd_ok      = dd_delta >= -RATCHET_MAX_DD_SLACK_PP   # allow tiny worsening
        return bool(ret_better and dd_ok)
    except (KeyError, TypeError):
        return False


# ── Track-record measurement (closes the learning loop) ─────────────────────
#
# After a parameter change is approved + applied, we want to know whether it
# actually helped on data the critic had NOT seen at proposal time. Naive "bot
# equity N days later" is confounded by market regime and by other param
# changes in the same window, so instead we run a FORWARD COUNTERFACTUAL:
# backtest the realized window [applied_at, applied_at + N days] twice — once
# with the param at its old value, once at its new value, everything else at
# current YAML — and take the return delta. This is walk-forward validation on
# real elapsed calendar time, isolating the single parameter.


def measure_change_forward_delta(
    strategy: str,
    param_name: str,
    old_value: float,
    new_value: float,
    applied_at: date,
    window_days: int,
    *,
    today: date | None = None,
) -> float | None:
    """Forward counterfactual return delta for one applied param change.

    Returns ``proposed_return_pct - baseline_return_pct`` over the realized
    window ``[applied_at, applied_at + window_days]``, where baseline reverts
    the single param to ``old_value`` and proposed sets it to ``new_value``
    (everything else from current YAML).

    Returns ``None`` if the window has not fully elapsed yet, the strategy is
    unknown, or the param is frozen (not tunable) — i.e. nothing to measure.
    """
    today = today or date.today()
    bot_id = STRATEGY_BOTS.get(strategy)
    if bot_id is None:
        return None
    if param_name not in BOUNDED_RANGES:
        # Param was frozen after the change; we cannot override it anymore.
        return None

    start = applied_at
    end = applied_at + timedelta(days=window_days)
    if end > today:
        return None  # window not fully elapsed — caller should retry later

    baseline = _summarise(
        run_backtest(bot_id, start, end, params_override={param_name: float(old_value)}),
        period_label=f"fwd-baseline {start}→{end}",
    )
    proposed = _summarise(
        run_backtest(bot_id, start, end, params_override={param_name: float(new_value)}),
        period_label=f"fwd-proposed {start}→{end}",
    )
    return round(proposed.return_pct - baseline.return_pct, 4)


# ── Tool 6: get_proposal_track_record ──────────────────────────────────────

def get_proposal_track_record(strategy: str) -> str:
    """Return the critic's own batting average for a strategy as JSON.

    Reads measured forward deltas from ``RuleChangeLog`` (filled in by
    ``scripts/measure_rule_changes.py``). Groups by ``param_name`` so the
    critic can see which knobs its past changes helped vs hurt and calibrate
    accordingly. Only rows with a measured 30-day delta are counted.
    """
    with get_session() as s:
        rows = (
            s.query(RuleChangeLog)
            .filter(RuleChangeLog.strategy == strategy)
            .order_by(RuleChangeLog.applied_at.desc())
            .all()
        )

    per_param: dict[str, dict] = {}
    changes: list[dict] = []
    for r in rows:
        d30 = r.pnl_30d_after
        d90 = r.pnl_90d_after
        changes.append({
            "param_name":    r.param_name,
            "old_value":     r.old_value,
            "new_value":     r.new_value,
            "applied_at":    r.applied_at.date().isoformat() if r.applied_at else None,
            "fwd_delta_30d": d30,
            "fwd_delta_90d": d90,
        })
        if d30 is None:
            continue  # not yet measured — exclude from batting average
        agg = per_param.setdefault(
            r.param_name,
            {"n_measured": 0, "n_positive": 0, "sum_delta_30d": 0.0,
             "sum_delta_90d": 0.0, "n_delta_90d": 0},
        )
        agg["n_measured"] += 1
        if d30 > 0:
            agg["n_positive"] += 1
        agg["sum_delta_30d"] += d30
        if d90 is not None:
            agg["sum_delta_90d"] += d90
            agg["n_delta_90d"] += 1

    summary: dict[str, dict] = {}
    total_measured = 0
    total_positive = 0
    for name, agg in per_param.items():
        n = agg["n_measured"]
        total_measured += n
        total_positive += agg["n_positive"]
        summary[name] = {
            "n_measured":         n,
            "batting_average":    round(agg["n_positive"] / n, 3) if n else None,
            "mean_fwd_delta_30d": round(agg["sum_delta_30d"] / n, 4) if n else None,
            "mean_fwd_delta_90d": (
                round(agg["sum_delta_90d"] / agg["n_delta_90d"], 4)
                if agg["n_delta_90d"] else None
            ),
        }

    overall_ba = round(total_positive / total_measured, 3) if total_measured else None
    return json.dumps({
        "strategy":          strategy,
        "n_changes_total":   len(changes),
        "n_measured":        total_measured,
        "overall_batting_average": overall_ba,
        "per_param":         summary,
        "changes":           changes,
    })
