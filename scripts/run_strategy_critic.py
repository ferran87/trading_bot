"""Manual entrypoint for the Strategy Critic agent.

Used by:
  - The "Run review now" button in the dashboard
  - The weekly Windows Task Scheduler entry (\\StrategyCritic_Weekly)
  - Ad-hoc manual runs

Usage:
    python scripts/run_strategy_critic.py
    python scripts/run_strategy_critic.py --strategy rsi_compounder
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

# Project root on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.strategy_critic import run_critic_for_strategy

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--strategy",
        choices=["rsi_compounder", "trend_momentum"],
        help="Run only this strategy (default: both)",
    )
    args = parser.parse_args()

    strategies = [args.strategy] if args.strategy else ["rsi_compounder", "trend_momentum"]
    results = []
    for strat in strategies:
        log.info("=== %s ===", strat)
        try:
            r = run_critic_for_strategy(strat)
            results.append(r)
            log.info("  result: %s", json.dumps(r))
        except Exception as exc:
            log.exception("  failed for %s: %s", strat, exc)
            results.append({"strategy": strat, "error": str(exc)})

    # Summary
    log.info("=== summary ===")
    for r in results:
        if "error" in r:
            log.info("  %s: ERROR %s", r["strategy"], r["error"])
        else:
            log.info(
                "  %s: %d proposal(s) submitted (%d validation errors), %d iterations",
                r["strategy"], r["proposals_submitted"], r["validation_errors"], r["iterations"],
            )


if __name__ == "__main__":
    main()
