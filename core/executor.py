"""Executor: routes proposed orders through risk -> broker -> portfolio -> DB.

The strategy's job ends when it hands a list of Orders to the executor.
Everything I/O-ish happens here:

  for order in proposed:
      risk_check(order)           # risk.py
      if approved:
          fill = broker.place(order)
          portfolio.apply_fill(fill)
          log trade
      else:
          log rejection (ErrorLog? no — it's a normal outcome; stdout + log)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Iterable

from sqlalchemy import func
from sqlalchemy.orm import Session

from core import risk
from core.broker import BrokerInterface, estimate_fee_eur
from core.db import Trade as TradeModel
from core.portfolio import Portfolio
from core.types import Fill, Order, PortfolioSnapshot

log = logging.getLogger(__name__)


@dataclass
class ExecutionReport:
    """Per-run summary so main.py / tests can assert on what happened."""

    bot_id: int
    ts: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))
    approved: list[tuple[Order, Fill]] = field(default_factory=list)
    rejected: list[tuple[Order, str]] = field(default_factory=list)

    def summary_line(self) -> str:
        return (
            f"bot={self.bot_id} "
            f"filled={len(self.approved)} "
            f"rejected={len(self.rejected)}"
        )


def run_orders(
    session: Session,
    broker: BrokerInterface,
    bot_id: int,
    orders: Iterable[Order],
    snapshot: PortfolioSnapshot,
    today: date,
    *,
    max_concurrent: int | None = None,
) -> ExecutionReport:
    """Process all proposed orders for ONE bot, sequentially.

    The snapshot is refreshed between orders (via Portfolio.snapshot) so
    each order sees the state after the previous fills of the same run.
    We mutate the passed-in `snapshot` in place to keep it cheap.

    ``max_concurrent`` — if set, a hard ceiling on the number of distinct open
    positions for this bot.  A BUY that would OPEN A NEW ticker is rejected once
    the book already holds ``max_concurrent`` names.  This is defence-in-depth
    against the strategy's slot accounting: the strategy frees a slot by pairing
    an exit SELL with an entry BUY, but if the broker rejects that SELL (market
    closed, T212 400, dust) the slot is never actually freed.  Without this
    guard the BUY still fills and the book creeps past the cap (observed on
    2026-06-30: SELL AAPL rejected by T212, BUY NOW filled -> 11/10).  Because
    SELLs are processed first and the snapshot is refreshed after every fill,
    ``len(snapshot.positions)`` here reflects only the exits that truly executed.
    """
    report = ExecutionReport(bot_id=bot_id)

    # Process SELLs first so freed cash is available for BUYs in the same run.
    sorted_orders = sorted(orders, key=lambda o: 0 if o.side.value == "SELL" else 1)

    for order in sorted_orders:
        # Guard: never place a second BUY for the same ticker on the same day.
        # This prevents duplicate orders when the bot runs multiple times (e.g.
        # scheduler fires + manual run) and the first order is still pending.
        if order.side.value == "BUY":
            already = (
                session.query(TradeModel)
                .filter(
                    TradeModel.bot_id == bot_id,
                    TradeModel.ticker == order.ticker,
                    TradeModel.side == "BUY",
                    func.date(TradeModel.timestamp) == today,
                )
                .first()
            )
            if already:
                log.info(
                    "SKIPPED  bot=%d BUY %s — already bought today (trade_id=%d)",
                    bot_id, order.ticker, already.id,
                )
                report.rejected.append((order, "already bought this ticker today"))
                continue

            # Hard position-cap guard. Only blocks BUYs that OPEN A NEW ticker —
            # scale-ins / pyramid adds to an existing holding don't consume a
            # slot. snapshot.positions reflects exits that actually filled this
            # run (SELLs sorted first + refreshed after each fill), so a
            # slot-freeing SELL the broker rejected leaves no room here.
            if (
                max_concurrent is not None
                and order.ticker not in snapshot.positions
                and len(snapshot.positions) >= max_concurrent
            ):
                log.warning(
                    "SKIPPED  bot=%d BUY %s — position cap reached (%d/%d open); "
                    "a slot-freeing SELL did not execute this run",
                    bot_id, order.ticker, len(snapshot.positions), max_concurrent,
                )
                report.rejected.append((order, "position cap reached"))
                continue

        decision = risk.check(session, order, snapshot, today)

        # If the only problem is insufficient cash, resize to available cash
        # and retry once. T212 supports fractional shares so this is always
        # safe — the broker's own dust-check (qty < 0.01) guards the floor.
        if not decision.approved and decision.reason.startswith("insufficient cash"):
            # Estimate fee at the FULL cash size, not at 1 share — for percentage
            # fees (T212 0.15% FX charge on USD stocks) the fee scales with
            # notional, so a 1-share estimate undershoots and the resized order
            # still fails the cash check by ~the fee delta. Pad with 1% extra
            # safety margin to absorb fill-price slippage between estimate and
            # actual broker fill.
            cash_qty_proxy = snapshot.cash_eur / order.ref_price_eur if order.ref_price_eur > 0 else 0.0
            fee_est = estimate_fee_eur(order.ticker, cash_qty_proxy, order.ref_price_eur)
            affordable = snapshot.cash_eur * 0.99 - fee_est
            if affordable > 0 and order.ref_price_eur > 0:
                resized_qty = round(affordable / order.ref_price_eur, 4)
                resized = Order(
                    bot_id=order.bot_id,
                    ticker=order.ticker,
                    side=order.side,
                    qty=resized_qty,
                    signal_reason=order.signal_reason,
                    ref_price_eur=order.ref_price_eur,
                    expected_profit_eur=order.expected_profit_eur,
                    asset_class=order.asset_class,
                )
                decision = risk.check(session, resized, snapshot, today)
                if decision.approved:
                    log.info(
                        "RESIZED  bot=%d BUY %s %.4f→%.4f (cash-fit: €%.2f available)",
                        bot_id, order.ticker, order.qty, resized_qty, snapshot.cash_eur,
                    )
                    order = resized

        if not decision.approved:
            report.rejected.append((order, decision.reason))
            log.info(
                "REJECTED bot=%d %s %s qty=%.4f ref=%.2f -- %s",
                bot_id, order.side.value, order.ticker, order.qty,
                order.ref_price_eur, decision.reason,
            )
            continue

        fill = broker.place_market_order(order)

        if fill.qty == 0:
            log.warning(
                "SKIPPED  bot=%d %s %s — qty rounded to 0 (increase capital or "
                "check per_position_pct)",
                bot_id, order.side.value, order.ticker,
            )
            continue

        Portfolio.apply_fill(session, bot_id, fill, order.signal_reason)
        # Commit immediately so a crash after broker fill cannot lose this trade.
        # Later commits in runner (equity snapshot, RunLog) are independent of fills.
        session.commit()
        report.approved.append((order, fill))

        if fill.is_pending:
            log.info(
                "PENDING  bot=%d %s %s qty=%.0f est.price=%.4f EUR -- %s "
                "(order queued at broker, will fill when market opens)",
                bot_id, order.side.value, order.ticker, fill.qty, fill.price_eur,
                order.signal_reason,
            )
        else:
            log.info(
                "FILLED   bot=%d %s %s qty=%.4f @ %.4f EUR fee=%.2f -- %s",
                bot_id, order.side.value, order.ticker, fill.qty, fill.price_eur,
                fill.fee_eur, order.signal_reason,
            )

        # Refresh snapshot in place. Cheap: reads a handful of rows.
        refreshed = Portfolio.snapshot(
            session, bot_id, {t: v.last_price_eur for t, v in snapshot.positions.items()}
        )
        snapshot.cash_eur = refreshed.cash_eur
        snapshot.positions = refreshed.positions

    return report
