"""Manual entrypoint for the Strategist agent (Phase 4).

Usage:
    .venv/Scripts/python.exe scripts/run_strategist.py --mode propose
    .venv/Scripts/python.exe scripts/run_strategist.py --mode review

--mode propose  : Claude proposes 4-5 new investment themes.
                  Results appear in the dashboard under "📚 Temes" as
                  pending proposals awaiting user approval.

--mode review   : Claude reviews active themes and surfaces informational
                  notes. Does NOT modify any theme ratings.
                  Notes appear in the dashboard under "Recomanacions de revisió".

This script is also triggered by the "Proposar nous temes" and
"Revisar temes existents" buttons in the dashboard themes tab.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

# ── Add project root to sys.path so imports work from scripts/ ─────────────────
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("run_strategist")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Strategist agent (Phase 4).")
    parser.add_argument(
        "--mode",
        choices=["propose", "review"],
        required=True,
        help=(
            "'propose' = suggest 4-5 new themes; "
            "'review' = surface informational notes on active themes."
        ),
    )
    args = parser.parse_args()

    from agents.strategist import propose_new_themes, review_existing_themes

    log.info("strategist starting: mode=%s", args.mode)

    if args.mode == "propose":
        summary = propose_new_themes()
    else:
        summary = review_existing_themes()

    print("\n── Strategist summary ───────────────────────────")
    print(json.dumps(summary, indent=2, ensure_ascii=False))

    if summary.get("errors"):
        log.warning("Strategist finished with %d error(s).", len(summary["errors"]))
        sys.exit(1)

    log.info("Strategist finished OK.")


if __name__ == "__main__":
    main()
