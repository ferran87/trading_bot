"""Virtual book reset."""
from __future__ import annotations

from datetime import date

from core import executor
from core.broker import MockBroker
from core.db import EquitySnapshot, Position, Trade
from core.portfolio import Portfolio
from core.types import AssetClass, Order, Side


def test_reset_virtual_book_clears_ledger(db_session):
    broker = MockBroker(seed=None)
    today = date(2026, 4, 1)
    snap = Portfolio.snapshot(db_session, 1, {})
    executor.run_orders(
        db_session,
        broker,
        1,
        [
            Order(
                bot_id=1,
                ticker="SXR8.DE",
                side=Side.BUY,
                qty=1.0,
                ref_price_eur=200.0,
                signal_reason="t",
                asset_class=AssetClass.ETF,
            )
        ],
        snap,
        today,
    )
    db_session.commit()
    assert db_session.query(Trade).filter(Trade.bot_id == 1).count() == 1
    assert db_session.query(Position).filter(Position.bot_id == 1).count() == 1

    Portfolio.reset_virtual_book(db_session, 1)
    db_session.commit()

    assert db_session.query(Trade).filter(Trade.bot_id == 1).count() == 0
    assert db_session.query(Position).filter(Position.bot_id == 1).count() == 0
    assert db_session.query(EquitySnapshot).filter(EquitySnapshot.bot_id == 1).count() == 0

    snap2 = Portfolio.snapshot(db_session, 1, {})
    assert snap2.cash_eur == 1000.0
    assert snap2.positions == {}
