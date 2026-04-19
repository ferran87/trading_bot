"""Executor integration: orders go through risk -> broker -> DB correctly."""
from __future__ import annotations

from datetime import date

from core import executor
from core.broker import MockBroker
from core.portfolio import Portfolio
from core.types import AssetClass, Order, Side


def _order(side: Side, ticker: str, qty: float, price: float, **kw) -> Order:
    return Order(
        bot_id=1, ticker=ticker, side=side, qty=qty,
        ref_price_eur=price, signal_reason="test",
        asset_class=kw.get("asset_class", AssetClass.ETF),
    )


def test_buy_then_sell_roundtrip(db_session):
    broker = MockBroker(seed=None)
    today = date.today()
    snap = Portfolio.snapshot(db_session, 1, {})
    assert snap.cash_eur == 1000.0

    # Buy 1 share at €200 (20% of book, within ETF 35% cap)
    report = executor.run_orders(
        db_session, broker, 1,
        [_order(Side.BUY, "SXR8.DE", 1, 200.0)],
        snap, today,
    )
    db_session.commit()
    assert len(report.approved) == 1
    assert len(report.rejected) == 0

    snap2 = Portfolio.snapshot(db_session, 1, {"SXR8.DE": 200.0})
    assert 0 < snap2.cash_eur < 1000.0
    assert "SXR8.DE" in snap2.positions

    # Sell it back
    report2 = executor.run_orders(
        db_session, broker, 1,
        [_order(Side.SELL, "SXR8.DE", 1, 200.0)],
        snap2, today,
    )
    db_session.commit()
    assert len(report2.approved) == 1

    final = Portfolio.snapshot(db_session, 1, {})
    # Two round trips of fees -> cash slightly below 1000.
    assert 996.0 < final.cash_eur < 1000.0
    assert "SXR8.DE" not in final.positions


def test_rejected_order_not_recorded(db_session):
    broker = MockBroker(seed=None)
    today = date.today()
    snap = Portfolio.snapshot(db_session, 1, {})
    # 50% of book as ETF -> blocked by 35% cap
    report = executor.run_orders(
        db_session, broker, 1,
        [_order(Side.BUY, "SXR8.DE", 1, 500.0)],
        snap, today,
    )
    db_session.commit()
    assert len(report.approved) == 0
    assert len(report.rejected) == 1
    final = Portfolio.snapshot(db_session, 1, {})
    assert final.cash_eur == 1000.0   # untouched
    assert not final.positions
