"""Reconciliation Agent — compares SQLite virtual books vs IBKR real positions.

Detects discrepancies between what the bots believe they hold (SQLite positions)
and what IBKR Gateway actually shows. Designed to catch:
  - Partial fills that didn't update SQLite
  - Manual trades placed directly in IBKR
  - Crashes that left broker and DB out of sync

Usage
-----
from agents.reconciliation import reconcile_positions, format_report

discrepancies = reconcile_positions(bot_ids=[7, 10], ibkr_port=4002)
print(format_report(discrepancies))
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

log = logging.getLogger(__name__)

Severity = Literal["OK", "WARN", "ERROR"]


@dataclass
class Discrepancy:
    ticker: str
    sqlite_qty: float       # aggregated across all given bot_ids
    ibkr_qty: float         # what IBKR actually holds
    diff: float             # ibkr_qty - sqlite_qty
    severity: Severity      # WARN = small rounding diff, ERROR = real mismatch

    @property
    def direction(self) -> str:
        if self.diff > 0:
            return f"IBKR té {self.diff:+.4f} de MÉS"
        return f"IBKR té {self.diff:+.4f} de MENYS"


def _sqlite_positions(bot_ids: list[int]) -> dict[str, float]:
    """Return {ticker: total_qty} aggregated across all given bot_ids."""
    from core.db import Position, get_session
    with get_session() as s:
        rows = s.query(Position).filter(Position.bot_id.in_(bot_ids)).all()
    result: dict[str, float] = {}
    for p in rows:
        result[p.ticker] = result.get(p.ticker, 0.0) + p.qty
    return result


def _ibkr_positions(ibkr_port: int) -> dict[str, float] | None:
    """Fetch live positions from IBKR Gateway.

    Returns {ibkr_symbol: qty} or None if Gateway is unreachable.
    The symbol is the raw IBKR local symbol (e.g. "ASML", "BMW").
    """
    try:
        from ib_async import IB
        ib = IB()
        ib.connect("127.0.0.1", ibkr_port, clientId=55, timeout=5)
        ib.sleep(1)
        positions = ib.positions()
        ib.disconnect()

        result: dict[str, float] = {}
        for pos in positions:
            symbol = pos.contract.localSymbol or pos.contract.symbol
            result[symbol] = result.get(symbol, 0.0) + float(pos.position)
        return result
    except Exception as exc:
        log.warning("reconciliation: cannot connect to IBKR port %d: %s", ibkr_port, exc)
        return None


def _build_ticker_map(bot_ids: list[int]) -> dict[str, str]:
    """Build {ibkr_local_symbol: our_ticker} from contracts.json.

    Falls back to identity mapping (symbol == ticker) when not found.
    """
    from core.config import DATA_DIR
    import json

    path = DATA_DIR / "contracts.json"
    if not path.exists():
        return {}

    data = json.loads(path.read_text(encoding="utf-8"))
    # contracts.json structure: {ticker: {local_symbol: ..., ...}}
    mapping: dict[str, str] = {}
    for ticker, info in data.items():
        local = info.get("local_symbol") or info.get("symbol") or ticker
        mapping[local] = ticker
    return mapping


def _external_positions() -> set[str]:
    """Return the set of tickers to ignore (held in IBKR outside the bot)."""
    try:
        from core.config import CONFIG
        return set(CONFIG.settings.get("reconciliation", {}).get("external_positions", []))
    except Exception:
        return set()


def reconcile_positions(
    bot_ids: list[int],
    ibkr_port: int,
) -> list[Discrepancy]:
    """Compare SQLite virtual books vs IBKR real positions.

    Parameters
    ----------
    bot_ids   : list of bot IDs whose positions are aggregated from SQLite.
    ibkr_port : IBKR Gateway port (paper or live).

    Returns
    -------
    List of Discrepancy objects (empty = everything matches).
    Returns a single Discrepancy with severity ERROR and ticker "IBKR_UNREACHABLE"
    when the Gateway cannot be reached.
    """
    sqlite_pos = _sqlite_positions(bot_ids)
    ibkr_pos = _ibkr_positions(ibkr_port)

    if ibkr_pos is None:
        return [Discrepancy(
            ticker="IBKR_UNREACHABLE",
            sqlite_qty=0, ibkr_qty=0, diff=0,
            severity="ERROR",
        )]

    ticker_map = _build_ticker_map(bot_ids)
    external   = _external_positions()

    # Normalise IBKR symbols → our tickers
    ibkr_by_ticker: dict[str, float] = {}
    for ibkr_sym, qty in ibkr_pos.items():
        our_ticker = ticker_map.get(ibkr_sym, ibkr_sym)
        ibkr_by_ticker[our_ticker] = ibkr_by_ticker.get(our_ticker, 0.0) + qty

    # Union of all tickers, minus externally-held ones
    all_tickers = (set(sqlite_pos) | set(ibkr_by_ticker)) - external
    if external:
        log.debug("reconciliation: ignoring external positions: %s", external)
    discrepancies: list[Discrepancy] = []

    for ticker in sorted(all_tickers):
        sq = sqlite_pos.get(ticker, 0.0)
        iq = ibkr_by_ticker.get(ticker, 0.0)
        diff = iq - sq

        if abs(diff) < 1e-4:
            continue  # perfect match

        severity: Severity = "ERROR" if abs(diff) >= 1.0 else "WARN"
        discrepancies.append(Discrepancy(
            ticker=ticker, sqlite_qty=sq, ibkr_qty=iq,
            diff=diff, severity=severity,
        ))
        log.warning(
            "reconciliation: %s mismatch — SQLite=%.4f IBKR=%.4f diff=%.4f [%s]",
            ticker, sq, iq, diff, severity,
        )

    if not discrepancies:
        log.info(
            "reconciliation: OK — %d tickers match between SQLite and IBKR",
            len(all_tickers),
        )

    return discrepancies


def format_report(discrepancies: list[Discrepancy]) -> str:
    """Format discrepancies as a human-readable string for logs / dashboard."""
    if not discrepancies:
        return "✅ Tot correcte — SQLite i IBKR coincideixen."

    lines = ["⚠️  Discrepàncies detectades:", ""]
    for d in discrepancies:
        if d.ticker == "IBKR_UNREACHABLE":
            lines.append("❌ No s'ha pogut connectar al Gateway IBKR.")
            continue
        icon = "❌" if d.severity == "ERROR" else "⚠️"
        lines.append(
            f"{icon} {d.ticker}: SQLite={d.sqlite_qty:.4f}  IBKR={d.ibkr_qty:.4f}"
            f"  ({d.direction})"
        )
    return "\n".join(lines)
