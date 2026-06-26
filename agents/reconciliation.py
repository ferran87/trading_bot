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


def _load_instrument_maps() -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """Load instrument mappings from the JSON files.

    Returns ``(t212_to_yf, yf_to_t212, yf_to_currency)``.  The override file
    wins over the base file (same precedence as the resolve script).
    """
    data_dir = Path(__file__).parents[1] / "data"
    t212_to_yf: dict[str, str] = {}
    yf_to_currency: dict[str, str] = {}
    for fname in ("t212_instruments.json", "t212_instruments_override.json"):
        path = data_dir / fname
        if not path.exists():
            continue
        instruments = json.loads(path.read_text(encoding="utf-8"))
        for yf_ticker, info in instruments.items():
            t2 = info.get("t212_ticker", "")
            if t2:
                t212_to_yf[t2] = yf_ticker
            cur = info.get("currency", "")
            if cur:
                yf_to_currency[yf_ticker] = cur
    yf_to_t212 = {v: k for k, v in t212_to_yf.items()}
    return t212_to_yf, yf_to_t212, yf_to_currency


def _parse_fill_date(raw: str):
    """Parse a T212 ISO timestamp into a ``date`` (today on failure)."""
    from datetime import date, datetime
    if raw:
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
        except ValueError:
            pass
    return date.today()


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

    # ── Build instrument maps (t212→yf for normalising, yf→t212 for display) ──
    t212_to_yf, yf_to_t212, _ = _load_instrument_maps()

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


def _audit_trade(session, bot_id: int, ticker: str, side: str,
                 qty: float, price_eur: float, when) -> None:
    """Record a synthetic Trade documenting a reconciliation adjustment.

    Marked with ``order_type='RECON'`` so it is distinguishable from real
    fills while still appearing in trade history.  ``side`` is BUY/SELL by the
    direction of the qty change so existing BUY/SELL aggregations stay valid.
    """
    from core.db import Trade
    session.add(Trade(
        bot_id=bot_id,
        timestamp=when,
        ticker=ticker,
        side=side,
        qty=round(qty, 6),
        price=price_eur,
        price_eur=price_eur,
        fx_rate=1.0,
        fee_eur=0.0,
        signal_reason="reconciliació T212 (T212 com a font de veritat)",
        order_type="RECON",
        broker_order_id=None,
        status="filled",
    ))


def sync_t212_positions(
    bot_ids: list[int],
    demo: bool = True,
    owner: str | None = None,
) -> dict:
    """Make the SQLite virtual books match the live T212 account.

    T212 is treated as the source of truth.  Three cases are handled:

      * **qty_mismatch** — exactly one bot holds the ticker → its qty is set to
        the T212 quantity.  Held by 2+ bots → *skipped* (ambiguous: cannot know
        which book to adjust).
      * **only_in_sqlite** — T212 holds none → every bot's position in that
        ticker is closed (removed).  Unambiguous regardless of bot count.
      * **only_in_t212** — no bot holds it → created **only** when the reconcile
        set contains exactly one bot (unambiguous owner); otherwise *skipped*.

    Cost basis for created positions uses T212 ``averagePrice`` (native ccy)
    converted to EUR via :mod:`core.fx`.  Every change writes a synthetic
    ``order_type='RECON'`` Trade for the audit trail.

    Returns ``{"error": str|None, "applied": [...], "skipped": [...]}``.
    """
    import core.fx as fx

    from core.db import Position, get_session, utcnow

    bot_ids = [int(b) for b in bot_ids]

    # ── Fetch live T212 portfolio ─────────────────────────────────────────────
    try:
        from core.broker import Trading212Broker
        broker = Trading212Broker(demo=demo, owner=owner)
        t212_items: list[dict] = broker._get("/equity/portfolio")
    except Exception as exc:
        log.warning("sync_t212: portfolio fetch failed: %s", exc)
        return {"error": str(exc), "applied": [], "skipped": []}

    t212_to_yf, _, yf_to_currency = _load_instrument_maps()

    # ── Normalise T212 positions: yf_ticker → {qty, avg_native, fill} ──────────
    t212: dict[str, dict] = {}
    for item in t212_items:
        t2_tick = item.get("ticker", "")
        yf_tick = t212_to_yf.get(t2_tick, t2_tick)
        qty = float(item.get("quantity", 0.0))
        avg = float(item.get("averagePrice", 0.0))
        if yf_tick in t212:  # rare: same yf ticker twice → qty-weighted avg
            prev = t212[yf_tick]
            total = prev["qty"] + qty
            prev["avg_native"] = (
                (prev["avg_native"] * prev["qty"] + avg * qty) / total
                if total else 0.0
            )
            prev["qty"] = total
        else:
            t212[yf_tick] = {
                "qty": qty,
                "avg_native": avg,
                "fill": item.get("initialFillDate", ""),
            }

    applied: list[dict] = []
    skipped: list[dict] = []

    with get_session() as s:
        rows = s.query(Position).filter(Position.bot_id.in_(bot_ids)).all()
        by_ticker: dict[str, list] = {}
        for p in rows:
            by_ticker.setdefault(p.ticker, []).append(p)

        now = utcnow()
        for ticker in sorted(set(by_ticker) | set(t212)):
            positions = by_ticker.get(ticker, [])
            sq_total = sum(p.qty for p in positions)
            tq = t212.get(ticker, {}).get("qty", 0.0)

            if abs(sq_total - tq) < 1e-4:
                continue  # already in sync

            # ── only_in_sqlite: T212 holds none → close every bot position ────
            if tq <= 1e-9:
                for p in positions:
                    _audit_trade(s, p.bot_id, ticker, "SELL", p.qty,
                                 p.avg_entry_eur, now)
                    applied.append({
                        "ticker": ticker, "bot_id": p.bot_id,
                        "action": "closed", "from": p.qty, "to": 0.0,
                    })
                    s.delete(p)
                continue

            # EUR cost basis from T212 averagePrice (native currency).
            cur = yf_to_currency.get(ticker, "EUR")
            avg_native = t212[ticker]["avg_native"]
            try:
                avg_eur = fx.to_eur(avg_native, cur)
            except Exception:
                avg_eur = avg_native  # fallback: assume already EUR

            if len(positions) == 1:
                # ── qty_mismatch (single bot) → adjust to T212 qty ────────────
                p = positions[0]
                delta = tq - p.qty
                _audit_trade(s, p.bot_id, ticker,
                             "BUY" if delta > 0 else "SELL",
                             abs(delta), avg_eur, now)
                applied.append({
                    "ticker": ticker, "bot_id": p.bot_id,
                    "action": "adjusted", "from": p.qty, "to": tq,
                })
                p.qty = tq
            elif not positions:
                # ── only_in_t212 → create, but only if unambiguous owner ──────
                if len(bot_ids) == 1:
                    bid = bot_ids[0]
                    s.add(Position(
                        bot_id=bid, ticker=ticker, qty=tq,
                        avg_entry_eur=avg_eur,
                        entry_date=_parse_fill_date(t212[ticker]["fill"]),
                    ))
                    _audit_trade(s, bid, ticker, "BUY", tq, avg_eur, now)
                    applied.append({
                        "ticker": ticker, "bot_id": bid,
                        "action": "created", "from": 0.0, "to": tq,
                    })
                else:
                    skipped.append({
                        "ticker": ticker, "reason": "orphan_ambiguous",
                        "sqlite_qty": 0.0, "t212_qty": tq,
                    })
            else:
                # qty_mismatch across 2+ bots → cannot attribute the delta
                skipped.append({
                    "ticker": ticker, "reason": "multi_bot_ambiguous",
                    "sqlite_qty": sq_total, "t212_qty": tq,
                })

        s.commit()

    log.info("sync_t212: applied=%d skipped=%d", len(applied), len(skipped))
    return {"error": None, "applied": applied, "skipped": skipped}
