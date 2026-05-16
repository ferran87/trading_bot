"""Reconciliation Agent — compares SQLite virtual books vs live T212 positions.

Detects discrepancies between what the bots believe they hold (SQLite positions)
and what the Trading 212 account actually shows.  Catches:
  - Partial fills that didn't update SQLite
  - Manual trades placed directly in T212
  - Crashes that left broker and DB out of sync

Usage
-----
from agents.reconciliation import reconcile_t212_positions

discrepancies = reconcile_t212_positions(bot_ids=[7, 10], demo=True, owner="Ferran")
for d in discrepancies:
    print(d)
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)


def _sqlite_positions(bot_ids: list[int]) -> dict[str, float]:
    """Return ``{ticker: total_qty}`` aggregated across all given bot_ids."""
    from core.db import Position, get_session
    with get_session() as s:
        rows = s.query(Position).filter(Position.bot_id.in_(bot_ids)).all()
    result: dict[str, float] = {}
    for p in rows:
        result[p.ticker] = result.get(p.ticker, 0.0) + p.qty
    return result


def reconcile_t212_positions(
    bot_ids: list[int],
    demo: bool = True,
    owner: str | None = None,
) -> list[dict]:
    """Compare SQLite positions against the live T212 account portfolio.

    Uses the T212 ``/equity/portfolio`` endpoint (requires Portfolio scope on
    the API key).  Maps T212 internal tickers back to yfinance tickers using
    ``data/t212_instruments.json`` + ``data/t212_instruments_override.json``.

    Parameters
    ----------
    bot_ids : list of bot IDs whose SQLite positions are aggregated.
    demo    : True = paper (demo.trading212.com), False = live.
    owner   : Optional T212 account owner name (e.g. 'Antonio').  When set,
              uses ``T212_API_KEY_{SUFFIX}_{OWNER}`` credentials.  Defaults
              to the unsuffixed env vars (single-account / Ferran).

    Returns
    -------
    List of dicts, each with keys:
        yf_ticker   : str   — our yfinance ticker
        t212_ticker : str   — T212 internal instrument ID (or '' if unknown)
        sqlite_qty  : float — total qty across all given bot_ids
        t212_qty    : float — qty in T212 account
        diff        : float — sqlite_qty - t212_qty  (positive = more in SQLite)
        severity    : str   — 'ERROR' (≥1 share diff) or 'WARN' (<1 share diff)
        issue       : str   — 'only_in_sqlite' | 'only_in_t212' | 'qty_mismatch'

    Returns a single entry with yf_ticker='T212_UNREACHABLE' if the API fails.
    """
    sqlite_pos = _sqlite_positions(bot_ids)  # {yf_ticker: qty}

    # ── T212 portfolio fetch ──────────────────────────────────────────────────
    try:
        from core.broker import Trading212Broker
        broker = Trading212Broker(demo=demo, owner=owner)
        t212_items: list[dict] = broker._get("/equity/portfolio")
    except Exception as exc:
        log.warning("reconcile_t212: portfolio fetch failed: %s", exc)
        return [{
            "yf_ticker":  "T212_UNREACHABLE",
            "t212_ticker": "",
            "sqlite_qty": 0.0, "t212_qty": 0.0, "diff": 0.0,
            "severity": "ERROR",
            "issue": "api_error",
            "detail": str(exc),
        }]

    # ── Build reverse map: t212_ticker → yf_ticker ────────────────────────────
    data_dir = Path(__file__).parents[1] / "data"
    instr_path = data_dir / "t212_instruments.json"
    override_path = data_dir / "t212_instruments_override.json"

    t212_to_yf: dict[str, str] = {}
    if instr_path.exists():
        instruments = json.loads(instr_path.read_text(encoding="utf-8"))
        for yf_ticker, info in instruments.items():
            t2 = info.get("t212_ticker", "")
            if t2:
                t212_to_yf[t2] = yf_ticker
    if override_path.exists():
        overrides = json.loads(override_path.read_text(encoding="utf-8"))
        for yf_ticker, info in overrides.items():
            t2 = info.get("t212_ticker", "")
            if t2:
                t212_to_yf[t2] = yf_ticker

    # Also build forward map: yf_ticker → t212_ticker (for display)
    yf_to_t212: dict[str, str] = {v: k for k, v in t212_to_yf.items()}

    # ── Normalise T212 positions ──────────────────────────────────────────────
    t212_pos: dict[str, float] = {}   # {yf_ticker: qty}
    t212_raw: dict[str, str]   = {}   # {yf_ticker: t212_ticker}
    for item in t212_items:
        t2_tick  = item.get("ticker", "")
        qty      = float(item.get("quantity", 0.0))
        yf_tick  = t212_to_yf.get(t2_tick, t2_tick)  # fall back to raw if unknown
        t212_pos[yf_tick] = t212_pos.get(yf_tick, 0.0) + qty
        t212_raw[yf_tick] = t2_tick

    # ── Compare ───────────────────────────────────────────────────────────────
    all_tickers = set(sqlite_pos) | set(t212_pos)
    discrepancies: list[dict] = []

    for ticker in sorted(all_tickers):
        sq = sqlite_pos.get(ticker, 0.0)
        tq = t212_pos.get(ticker, 0.0)
        diff = sq - tq  # positive = more in SQLite than T212

        if abs(diff) < 1e-4:
            continue  # perfect match

        if sq > 0 and tq == 0:
            issue = "only_in_sqlite"
        elif tq > 0 and sq == 0:
            issue = "only_in_t212"
        else:
            issue = "qty_mismatch"

        severity = "ERROR" if abs(diff) >= 1.0 else "WARN"
        discrepancies.append({
            "yf_ticker":  ticker,
            "t212_ticker": t212_raw.get(ticker, yf_to_t212.get(ticker, "")),
            "sqlite_qty": sq,
            "t212_qty":   tq,
            "diff":       diff,
            "severity":   severity,
            "issue":      issue,
        })
        log.warning(
            "reconcile_t212: %s mismatch — SQLite=%.4f T212=%.4f diff=%.4f [%s] (%s)",
            ticker, sq, tq, diff, severity, issue,
        )

    if not discrepancies:
        log.info(
            "reconcile_t212: OK — %d ticker(s) match between SQLite and T212",
            len(all_tickers),
        )

    return discrepancies
