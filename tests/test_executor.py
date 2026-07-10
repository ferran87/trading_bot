"""Executor integration: orders go through risk -> broker -> DB correctly."""
from __future__ import annotations

from datetime import date, datetime, timezone

from core import executor
from core.broker import MockBroker
from core.portfolio import Portfolio
from core.types import AssetClass, Fill, Order, Side


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


class _SellRejectingBroker(MockBroker):
    """MockBroker whose SELLs are rejected by the broker (qty=0 fill), as T212
    does pre-market (HTTP 400). BUYs fill normally."""

    def place_market_order(self, order: Order) -> Fill:
        if order.side is Side.SELL:
            return Fill(
                ticker=order.ticker, side=order.side, qty=0.0,
                price=0.0, price_eur=0.0, fx_rate=1.0, fee_eur=0.0,
                timestamp=datetime.now(tz=timezone.utc),
            )
        return super().place_market_order(order)


def test_cap_holds_when_slot_freeing_sell_is_rejected(db_session):
    """Regression for 2026-06-30: a 1-for-1 rotation where the exit SELL is
    rejected by the broker must NOT let the entry BUY push the book past
    max_concurrent. With max_concurrent=1 and one position already open, the
    failed SELL leaves no slot, so the new-ticker BUY is rejected."""
    broker = _SellRejectingBroker(seed=None)
    today = date.today()

    # Open one position (fills the single slot).
    snap = Portfolio.snapshot(db_session, 1, {})
    executor.run_orders(
        db_session, broker, 1,
        [_order(Side.BUY, "SXR8.DE", 1, 200.0)],
        snap, today, max_concurrent=1,
    )
    db_session.commit()
    snap2 = Portfolio.snapshot(db_session, 1, {"SXR8.DE": 200.0})
    assert "SXR8.DE" in snap2.positions

    # Rotation: SELL the held name (broker rejects it) + BUY a new name.
    report = executor.run_orders(
        db_session, broker, 1,
        [
            _order(Side.SELL, "SXR8.DE", 1, 200.0),
            _order(Side.BUY, "IWDA.AS", 1, 100.0),
        ],
        snap2, today, max_concurrent=1,
    )
    db_session.commit()

    final = Portfolio.snapshot(db_session, 1, {"SXR8.DE": 200.0, "IWDA.AS": 100.0})
    # The rejected SELL left SXR8.DE open; the cap guard must block the entry.
    assert "SXR8.DE" in final.positions
    assert "IWDA.AS" not in final.positions
    assert len(final.positions) == 1
    assert any("position cap reached" in r for _, r in report.rejected)


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


class _OrphanOpenOrderBroker(MockBroker):
    """Reports an unrecorded open BUY order for SXR8.DE — an orphan a crashed run
    left live at the broker, so the executor must not place another."""

    def find_unrecorded_open_order(self, ticker, side, recorded_ids):
        if ticker == "SXR8.DE" and side == "BUY":
            return "ORPHAN-123"
        return None


def test_dedup_guard_skips_unrecorded_open_broker_order(db_session):
    broker = _OrphanOpenOrderBroker(seed=None)
    today = date.today()
    snap = Portfolio.snapshot(db_session, 1, {})

    report = executor.run_orders(
        db_session, broker, 1,
        [_order(Side.BUY, "SXR8.DE", 1, 200.0)],
        snap, today,
    )
    db_session.commit()

    # Placement was skipped — no fill; recorded as a rejection with the guard reason.
    assert len(report.approved) == 0
    assert len(report.rejected) == 1
    assert "unrecorded broker order" in report.rejected[0][1]
    # Book untouched — no duplicate position created.
    assert "SXR8.DE" not in Portfolio.snapshot(db_session, 1, {}).positions
