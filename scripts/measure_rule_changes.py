"""Track-record measurement job for the Strategy Critic learning loop.

Backfills ``RuleChangeLog.pnl_30d_after`` / ``pnl_90d_after`` for every applied
parameter change whose forward window has fully elapsed. Each value is a
FORWARD COUNTERFACTUAL return delta (proposed value vs old value, backtested
over the realized window after the change) — see
``agents.critic_tools.measure_change_forward_delta`` for the rationale.

This closes the learning loop: the critic reads these deltas via
``get_proposal_track_record`` before proposing again, and the dashboard shows
the batting average.

Idempotent — only fills columns that are still NULL and whose window has
elapsed, so it is safe to run on a weekly schedule.

Used by:
  - The weekly Windows Task Scheduler entry (\\RuleChangeTracker_Weekly)
  - Ad-hoc manual runs

Usage:
    python scripts/measure_rule_changes.py
    python scripts/measure_rule_changes.py --strategy rsi_compounder
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

# Project root on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.critic_tools import measure_change_forward_delta
from core.db import RuleChangeLog, get_session

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

WINDOWS = (("pnl_30d_after", 30), ("pnl_90d_after", 90))


def measure_all(strategy: str | None = None, *, today: date | None = None) -> dict:
    """Backfill forward deltas for all eligible RuleChangeLog rows.

    Returns a summary dict: rows scanned, deltas filled, rows skipped (window
    not elapsed yet). Each delta is computed and committed independently so a
    failure on one row never loses progress on the others.
    """
    today = today or date.today()
    n_scanned = 0
    n_filled = 0
    n_skipped = 0
    n_errors = 0

    with get_session() as s:
        q = s.query(RuleChangeLog)
        if strategy:
            q = q.filter(RuleChangeLog.strategy == strategy)
        rows = q.order_by(RuleChangeLog.applied_at).all()

        for r in rows:
            n_scanned += 1
            applied_date = r.applied_at.date() if r.applied_at else None
            if applied_date is None:
                continue

            for column, window_days in WINDOWS:
                if getattr(r, column) is not None:
                    continue  # already measured — idempotent

                try:
                    delta = measure_change_forward_delta(
                        r.strategy, r.param_name, r.old_value, r.new_value,
                        applied_date, window_days, today=today,
                    )
                except Exception as exc:  # one bad backtest must not abort the job
                    n_errors += 1
                    log.exception(
                        "  change id=%d %s %s: %dd measurement failed: %s",
                        r.id, r.strategy, r.param_name, window_days, exc,
                    )
                    continue

                if delta is None:
                    n_skipped += 1  # window not elapsed yet (or frozen param)
                    continue

                setattr(r, column, delta)
                s.commit()
                n_filled += 1
                log.info(
                    "  change id=%d %s %s %.4f→%.4f: %dd fwd delta = %+.4f",
                    r.id, r.strategy, r.param_name,
                    r.old_value, r.new_value, window_days, delta,
                )

    return {
        "scanned": n_scanned,
        "filled": n_filled,
        "skipped_not_elapsed": n_skipped,
        "errors": n_errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--strategy",
        choices=["rsi_compounder", "trend_momentum"],
        help="Measure only this strategy (default: all)",
    )
    args = parser.parse_args()

    log.info("measure_rule_changes: starting (strategy=%s)", args.strategy or "all")
    summary = measure_all(args.strategy)
    log.info(
        "=== done: scanned=%d filled=%d skipped(not elapsed)=%d errors=%d ===",
        summary["scanned"], summary["filled"],
        summary["skipped_not_elapsed"], summary["errors"],
    )


if __name__ == "__main__":
    main()
