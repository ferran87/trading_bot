"""Manual entrypoint for the AI Thesis Portfolio Manager agent.

Usage:
  # Weekday daily review (review active theses only):
  .venv\\Scripts\\python.exe scripts\\run_portfolio_manager.py

  # Sunday full run (review + candidate scan):
  .venv\\Scripts\\python.exe scripts\\run_portfolio_manager.py --sunday

  # Force Sunday mode regardless of day:
  .venv\\Scripts\\python.exe scripts\\run_portfolio_manager.py --sunday

Logs are appended to data/logs/portfolio_manager.log (created if absent).
Summary is printed to stdout regardless of log level.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

# Project root on sys.path so `agents`, `core`, etc. are importable
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# ── Logging setup ─────────────────────────────────────────────────────────────
_LOG_PATH = Path(__file__).parents[1] / "data" / "logs" / "portfolio_manager.log"
_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(_LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the AI Thesis Portfolio Manager agent (bot 30)."
    )
    parser.add_argument(
        "--sunday",
        action="store_true",
        default=None,
        help="Force Sunday mode (review + candidate scan). Default: auto-detect from weekday.",
    )
    args = parser.parse_args()

    is_sunday = args.sunday if args.sunday is not None else (date.today().weekday() == 6)

    log.info(
        "run_portfolio_manager: starting (date=%s, is_sunday=%s)",
        date.today(), is_sunday,
    )

    try:
        from agents.portfolio_manager import run_daily_review
        summary = run_daily_review(is_sunday=is_sunday)
    except Exception:
        log.exception("run_portfolio_manager: unhandled error")
        sys.exit(1)

    print("\n-- Summary ------------------------------------------------------")
    print(json.dumps(summary, indent=2, default=str))

    if summary.get("reviews_written", 0) == 0 and not is_sunday:
        log.warning(
            "run_portfolio_manager: no reviews written — "
            "is the agent running correctly? Check data/logs/portfolio_manager.log"
        )

    log.info("run_portfolio_manager: done")


if __name__ == "__main__":
    main()
