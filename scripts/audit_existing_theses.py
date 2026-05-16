"""Phase 6 — retroactive audit of every active thesis.

For each thesis in {candidate, waiting, active} status:
  1. Re-fetch fundamentals + peer metrics
  2. Parse the stored valuation_assessment for digit-bearing tokens
  3. Validate each token against the freshly-computed allowed_displays set
  4. If ANY token is unsourced (would be rejected by Phase 6 submit_thesis):
       - mark Thesis.status = 'invalidated_numerical_error'
       - set Thesis.conviction = 1 (blocks any execution via strategy gate)
       - persist the fresh snapshots so the dashboard shows the real numbers
       - log the offending token + the allowed set

The user runs this once after deploying Phase 6 to clean up the 7 existing
theses (NVDA, LLY, TSM, ANET, CEG, PLTR, AVGO) flagged by Claude Chat.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.pm_tools import (  # noqa: E402
    _LATEST_PEER_SNAPSHOT,
    _LATEST_VALUATION_SNAPSHOT,
    _peer_snapshot_display_strings,
    _snapshot_display_strings,
    get_fundamentals,
    get_peer_metrics,
)
from agents.pm_validators import validate_no_invented_digits  # noqa: E402
from core.db import Thesis, get_session  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)

INVALID_STATUS = "invalidated_numerical_error"
ACTIVE_STATUSES = ("candidate", "waiting", "active")


def audit_one(thesis: Thesis) -> dict:
    """Audit a single thesis. Returns {ticker, status, issues, fresh_snapshot, fresh_peer}."""
    ticker = thesis.ticker
    # Warm the snapshot caches (and capture the dicts to persist)
    try:
        get_fundamentals(ticker)
        get_peer_metrics(ticker)
    except Exception as exc:
        return {
            "ticker": ticker,
            "thesis_id": thesis.id,
            "status": "skipped",
            "issues": [f"could not refresh snapshots: {exc}"],
            "fresh_snapshot": None,
            "fresh_peer": None,
        }
    val_snap = _LATEST_VALUATION_SNAPSHOT.get(ticker)
    peer_snap = _LATEST_PEER_SNAPSHOT.get(ticker)
    allowed = (
        _snapshot_display_strings(val_snap)
        | _peer_snapshot_display_strings(peer_snap)
    )

    issues: list[str] = []
    for field_name in ("positioning_vs_theme", "execution_evidence", "valuation_assessment"):
        text = getattr(thesis, field_name, None)
        if not text:
            continue
        err = validate_no_invented_digits(text, allowed)
        if err:
            issues.append(f"{field_name}: {err}")

    return {
        "ticker": ticker,
        "thesis_id": thesis.id,
        "status": "FAIL" if issues else "OK",
        "issues": issues,
        "fresh_snapshot": val_snap,
        "fresh_peer": peer_snap,
        "conviction": thesis.conviction,
        "current_status": thesis.status,
    }


def main(*, dry_run: bool = False) -> None:
    log.info("=" * 70)
    log.info("Phase 6 — retroactive audit of active theses")
    log.info("=" * 70)

    with get_session() as s:
        theses = (
            s.query(Thesis)
            .filter(Thesis.status.in_(ACTIVE_STATUSES))
            .order_by(Thesis.opened_at)
            .all()
        )
        theses_data = [(t.id, t.ticker) for t in theses]

    log.info("Active theses to audit: %d", len(theses_data))
    for tid, tk in theses_data:
        log.info("  - id=%d ticker=%s", tid, tk)
    log.info("")

    results: list[dict] = []
    for tid, _tk in theses_data:
        with get_session() as s:
            t = s.query(Thesis).filter(Thesis.id == tid).first()
            if t is None:
                continue
            r = audit_one(t)
        results.append(r)
        log.info(
            "audit %-6s id=%-3d ticker=%-7s conviction=%s status=%s",
            r["status"], r["thesis_id"], r["ticker"],
            r.get("conviction", "?"), r.get("current_status", "?"),
        )
        for issue in r["issues"]:
            log.info("    └─ %s", issue[:200])

    failures = [r for r in results if r["status"] == "FAIL"]
    log.info("")
    log.info("=" * 70)
    log.info("Summary: %d total / %d FAIL / %d OK / %d skipped",
             len(results),
             sum(1 for r in results if r["status"] == "FAIL"),
             sum(1 for r in results if r["status"] == "OK"),
             sum(1 for r in results if r["status"] == "skipped"))
    log.info("=" * 70)

    if dry_run:
        log.info("(dry-run — no DB changes applied)")
        return

    if not failures:
        log.info("Nothing to mark — all active theses passed the audit.")
        return

    log.info("")
    log.info("Marking %d thesis(es) as %r and capping conviction to 1...",
             len(failures), INVALID_STATUS)
    with get_session() as s:
        for r in failures:
            t = s.query(Thesis).filter(Thesis.id == r["thesis_id"]).first()
            if t is None:
                continue
            t.status = INVALID_STATUS
            t.conviction = 1
            # Persist fresh snapshots so the dashboard shows real numbers
            if r["fresh_snapshot"]:
                t.valuation_snapshot = r["fresh_snapshot"]
            if r["fresh_peer"]:
                t.peer_snapshot = r["fresh_peer"]
            # Append to warnings_at_creation to document the invalidation
            existing_warnings = list(t.warnings_at_creation or [])
            existing_warnings.append(
                f"PHASE6_AUDIT: invalidated for numerical error. Issues: "
                + " | ".join(r["issues"])
            )
            t.warnings_at_creation = existing_warnings
            log.info("  - id=%d ticker=%s → status=%s conviction=1",
                     t.id, t.ticker, INVALID_STATUS)
        s.commit()

    log.info("")
    log.info("Done. Audit results JSON:")
    print(json.dumps([
        {k: v for k, v in r.items() if k not in ("fresh_snapshot", "fresh_peer")}
        for r in results
    ], indent=2, default=str))


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    main(dry_run=dry)
