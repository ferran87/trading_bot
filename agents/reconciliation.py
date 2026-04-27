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


def import_manual_positions(
    bot_ids: list[int],
    ibkr_port: int,
    primary_bot_id: int | None = None,
) -> int:
    """Import IBKR positions that are not tracked in any bot's SQLite records.

    For each position in the IBKR account that has no matching SQLite Position
    row (within a 1-share tolerance), we create:

    * A ``Trade`` record with ``signal_reason='manual — imported from IBKR'``,
      ``order_type='MANUAL'``, ``status='filled'``.
    * A ``Position`` row with qty and avg_entry_eur from IBKR's ``avgCost``.

    The trade is attributed to ``primary_bot_id`` (defaults to ``bot_ids[0]``).
    Because these bots share the same paper account the cash impact is secondary;
    IBKR-backed KPIs use IBKR's NetLiquidation as the source of truth anyway.

    Positions listed in ``settings.yaml`` under
    ``reconciliation.external_positions`` are skipped.

    Returns the number of positions imported.
    """
    from core.db import Trade as TradeModel, Position as PositionModel, get_session
    from datetime import datetime, timezone

    if primary_bot_id is None and bot_ids:
        primary_bot_id = bot_ids[0]
    if primary_bot_id is None:
        log.warning("import_manual_positions: no bot_id provided — skipping")
        return 0

    sqlite_pos = _sqlite_positions(bot_ids)   # {our_ticker: qty}
    ibkr_pos   = _ibkr_positions(ibkr_port)  # {localSymbol: qty} or None
    if ibkr_pos is None:
        log.warning("import_manual_positions: cannot reach IBKR port %d", ibkr_port)
        return 0

    ticker_map = _build_ticker_map(bot_ids)   # {localSymbol: our_ticker}
    external   = _external_positions()

    # Fetch avgCost and currency per symbol
    ibkr_avg_cost: dict[str, float] = {}
    ibkr_ccy:      dict[str, str]   = {}
    try:
        from ib_async import IB
        ib = IB()
        ib.connect("127.0.0.1", ibkr_port, clientId=58, timeout=5)
        ib.sleep(1)
        for item in ib.portfolio():
            sym = item.contract.localSymbol or item.contract.symbol
            ibkr_avg_cost[sym] = float(item.averageCost)
            ibkr_ccy[sym]      = item.contract.currency or "EUR"
        ib.disconnect()
    except Exception as exc:
        log.warning("import_manual_positions: portfolio fetch failed: %s", exc)
        return 0

    from core import fx

    imported = 0
    now   = datetime.now(tz=timezone.utc)
    today = now.date()

    with get_session() as s:
        for ibkr_sym, ibkr_qty in ibkr_pos.items():
            if ibkr_qty <= 0:
                continue

            our_ticker = ticker_map.get(ibkr_sym, ibkr_sym)

            # Skip if SQLite already tracks this within 1 share
            if abs(sqlite_pos.get(our_ticker, 0.0) - ibkr_qty) < 1.0:
                continue

            # Skip explicitly excluded tickers
            if our_ticker in external or ibkr_sym in external:
                log.debug("import_manual_positions: skipping external %s", ibkr_sym)
                continue

            avg_cost_local = ibkr_avg_cost.get(ibkr_sym, 0.0)
            ccy            = ibkr_ccy.get(ibkr_sym, "EUR")
            fx_rate        = fx.eur_per_unit(ccy)
            avg_cost_eur   = avg_cost_local * fx_rate if fx_rate > 0 else avg_cost_local

            log.info(
                "import_manual_positions: %s qty=%.2f @ €%.4f ccy=%s → bot=%d",
                our_ticker, ibkr_qty, avg_cost_eur, ccy, primary_bot_id,
            )

            # Trade record — audit trail only; order_type=MANUAL flags it clearly
            s.add(TradeModel(
                bot_id=primary_bot_id,
                timestamp=now,
                ticker=our_ticker,
                side="BUY",
                qty=ibkr_qty,
                price=avg_cost_local,
                price_eur=avg_cost_eur,
                fx_rate=fx_rate,
                fee_eur=0.0,
                signal_reason=f"manual — imported from IBKR ({ibkr_sym})",
                order_type="MANUAL",
                broker_order_id=None,
                status="filled",
            ))

            # Position row — upsert
            pos = (
                s.query(PositionModel)
                .filter(
                    PositionModel.bot_id == primary_bot_id,
                    PositionModel.ticker == our_ticker,
                )
                .one_or_none()
            )
            if pos is None:
                s.add(PositionModel(
                    bot_id=primary_bot_id,
                    ticker=our_ticker,
                    qty=ibkr_qty,
                    avg_entry_eur=avg_cost_eur,
                    entry_date=today,
                ))
            else:
                pos.qty           = ibkr_qty
                pos.avg_entry_eur = avg_cost_eur

            imported += 1

        if imported:
            s.commit()

    return imported


def resolve_pending_orders(
    bot_ids: list[int],
    ibkr_port: int,
) -> int:
    """Resolve pending DB trades against actual IBKR positions.

    For each bot in ``bot_ids`` we look at trades with ``status='pending'``
    and compare them against live IBKR positions.  If IBKR holds the expected
    qty (within 1 share tolerance) we mark the trade as ``'filled'`` and
    correct the fill price using IBKR's ``avgCost``.

    Returns the number of trades resolved.

    Strategy
    --------
    We use IBKR **positions** (not executions) because:
    * Positions are always fresh regardless of clientId / session.
    * We just need to confirm the shares landed — we update price with avgCost.
    * Works even if the previous session crashed before recording the fill.
    """
    from core.db import Trade as TradeModel, Position as PositionModel, get_session
    from core.config import DATA_DIR
    import json

    with get_session() as s:
        pending = (
            s.query(TradeModel)
            .filter(
                TradeModel.status == "pending",
                TradeModel.bot_id.in_(bot_ids),
            )
            .all()
        )

    if not pending:
        return 0

    # Load contracts.json for local-symbol → ticker mapping
    contracts_path = DATA_DIR / "contracts.json"
    contracts: dict = {}
    if contracts_path.exists():
        contracts = json.loads(contracts_path.read_text(encoding="utf-8"))
    # Build localSymbol → ticker reverse map
    local_to_ticker: dict[str, str] = {
        info.get("local_symbol") or info.get("symbol", tk): tk
        for tk, info in contracts.items()
    }

    ibkr_pos = _ibkr_positions(ibkr_port)  # {localSymbol: qty}
    if ibkr_pos is None:
        log.warning("resolve_pending_orders: cannot reach IBKR — will retry next run")
        return 0

    # Fetch avgCost per symbol from a separate IBKR call (ib.portfolio())
    ibkr_avg_cost: dict[str, float] = {}
    try:
        from ib_async import IB
        ib = IB()
        ib.connect("127.0.0.1", ibkr_port, clientId=57, timeout=5)
        ib.sleep(1)
        for item in ib.portfolio():
            sym = item.contract.localSymbol or item.contract.symbol
            ibkr_avg_cost[sym] = float(item.averageCost)
        ib.disconnect()
    except Exception as exc:
        log.warning("resolve_pending_orders: could not fetch avgCost: %s", exc)

    resolved = 0
    with get_session() as s:
        # Re-query inside this session for proper ORM tracking
        pending_ids = [t.id for t in pending]
        for trade in s.query(TradeModel).filter(TradeModel.id.in_(pending_ids)).all():
            ticker = trade.ticker
            # Find the IBKR local symbol for this ticker
            entry = contracts.get(ticker, {})
            local_sym = entry.get("local_symbol") or entry.get("symbol") or ticker
            ibkr_qty = ibkr_pos.get(local_sym, 0.0)

            if ibkr_qty < 1:
                log.debug(
                    "resolve_pending_orders: %s still not in IBKR positions (qty=%.2f)",
                    ticker, ibkr_qty,
                )
                continue

            # Confirm qty matches (within 2 shares to allow partial fills)
            qty_ok = abs(ibkr_qty - trade.qty) <= 2
            if not qty_ok:
                log.warning(
                    "resolve_pending_orders: %s expected qty=%.0f but IBKR has %.0f "
                    "— leaving as pending for manual review",
                    ticker, trade.qty, ibkr_qty,
                )
                continue

            # Update trade with actual fill data
            avg_cost_local = ibkr_avg_cost.get(local_sym)
            if avg_cost_local and avg_cost_local > 0:
                from core import fx
                ccy = entry.get("currency", "EUR")
                fx_rate = fx.eur_per_unit(ccy)
                trade.price = avg_cost_local
                trade.price_eur = avg_cost_local * fx_rate
                trade.fx_rate = fx_rate
                # Recalculate fee with actual price
                trade.fee_eur = estimate_fee_eur(ticker, ibkr_qty, trade.price_eur)

            trade.qty = ibkr_qty
            trade.status = "filled"

            # Update the Position row with corrected qty and avg_entry
            pos = (
                s.query(PositionModel)
                .filter(
                    PositionModel.bot_id == trade.bot_id,
                    PositionModel.ticker == ticker,
                )
                .one_or_none()
            )
            if pos is not None:
                pos.qty = ibkr_qty
                pos.avg_entry_eur = trade.price_eur
            else:
                from datetime import date
                s.add(PositionModel(
                    bot_id=trade.bot_id,
                    ticker=ticker,
                    qty=ibkr_qty,
                    avg_entry_eur=trade.price_eur,
                    entry_date=trade.timestamp.date(),
                ))

            log.info(
                "resolve_pending_orders: resolved %s bot=%d qty=%.0f avg_eur=%.4f",
                ticker, trade.bot_id, ibkr_qty, trade.price_eur,
            )
            resolved += 1

        if resolved:
            s.commit()

    return resolved


def cancel_orphan_orders(
    bot_ids: list[int],
    ibkr_port: int,
) -> int:
    """Cancel any open IBKR orders that have no matching pending DB trade.

    These 'orphan' orders are typically left behind by bot runs that crashed
    after sending the order to IBKR but before writing the fill to the DB.
    If not cancelled they can double-fill on the next market session.

    Returns the number of orders cancelled.
    """
    from core.db import Trade as TradeModel, get_session

    # Collect permIds of legitimately pending DB trades
    with get_session() as s:
        pending_trades = (
            s.query(TradeModel)
            .filter(
                TradeModel.status == "pending",
                TradeModel.bot_id.in_(bot_ids),
            )
            .all()
        )
    known_perm_ids: set[int] = set()
    for t in pending_trades:
        try:
            known_perm_ids.add(int(t.broker_order_id))
        except (TypeError, ValueError):
            pass

    try:
        from ib_async import IB
        ib = IB()
        ib.connect("127.0.0.1", ibkr_port, clientId=102, timeout=5)
        ib.sleep(2)
        open_trades = ib.reqAllOpenOrders()
        ib.sleep(1)

        cancelled = 0
        for trade in open_trades:
            perm_id = trade.order.permId
            if perm_id and perm_id not in known_perm_ids:
                log.warning(
                    "cancel_orphan_orders: cancelling orphan order "
                    "orderId=%d permId=%d %s %s %s (not in DB pending trades)",
                    trade.order.orderId, perm_id,
                    trade.order.action, trade.order.totalQuantity,
                    trade.contract.symbol,
                )
                ib.cancelOrder(trade.order)
                cancelled += 1

        if cancelled:
            ib.sleep(2)  # let cancels propagate
        ib.disconnect()
        return cancelled

    except Exception as exc:
        log.warning("cancel_orphan_orders: failed on port %d: %s", ibkr_port, exc)
        return 0


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
