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

from sqlalchemy.orm import Session

from core import risk
from core.broker import BrokerInterface
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
) -> ExecutionReport:
    """Process all proposed orders for ONE bot, sequentially.

    The snapshot is refreshed between orders (via Portfolio.snapshot) so
    each order sees the state after the previous fills of the same run.
    We mutate the passed-in `snapshot` in place to keep it cheap.
    """
    report = ExecutionReport(bot_id=bot_id)

    # Process SELLs first so freed cash is available for BUYs in the same run.
    sorted_orders = sorted(orders, key=lambda o: 0 if o.side.value == "SELL" else 1)

    for order in sorted_orders:
        decision = risk.check(session, order, snapshot, today)
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
                "(order queued at IBKR, will fill when market opens)",
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
