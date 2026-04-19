"""Guardrail tests — one per rule in PROJECT_PLAN.md."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from core import risk
from core.portfolio import Portfolio
from core.types import AssetClass, Fill, Order, PortfolioSnapshot, PositionView, Side


def _buy(ticker: str, qty: float, price: float, **kw) -> Order:
    return Order(
        bot_id=1,
        ticker=ticker,
        side=Side.BUY,
        qty=qty,
        ref_price_eur=price,
        signal_reason="test",
        asset_class=kw.pop("asset_class", AssetClass.STOCK),
        expected_profit_eur=kw.pop("expected_profit_eur", 0.0),
    )


def _sell(ticker: str, qty: float, price: float) -> Order:
    return Order(
        bot_id=1, ticker=ticker, side=Side.SELL, qty=qty,
        ref_price_eur=price, signal_reason="exit",
        asset_class=AssetClass.STOCK,
    )


def _snap(cash: float, positions: dict[str, PositionView] | None = None) -> PortfolioSnapshot:
    return PortfolioSnapshot(bot_id=1, cash_eur=cash, positions=positions or {})


# --- Rule 1: position caps ---

def test_stock_cap_20pct_allows_200eur_on_1000eur_book(db_session):
    snap = _snap(1000.0)
    order = _buy("AAPL", 1, 200.0)  # 200/1000 = 20.0% -> ok
    assert risk.check(db_session, order, snap, date.today()).approved


def test_stock_cap_20pct_blocks_over_20(db_session):
    snap = _snap(1000.0)
    order = _buy("AAPL", 1, 250.0)  # 25%
    r = risk.check(db_session, order, snap, date.today())
    assert not r.approved
    assert "position cap" in r.reason


def test_etf_cap_35pct_allows_350eur(db_session):
    snap = _snap(1000.0)
    order = _buy("SXR8.DE", 1, 350.0, asset_class=AssetClass.ETF)
    assert risk.check(db_session, order, snap, date.today()).approved


def test_etf_cap_35pct_blocks_360(db_session):
    snap = _snap(1000.0)
    order = _buy("SXR8.DE", 1, 360.0, asset_class=AssetClass.ETF)
    r = risk.check(db_session, order, snap, date.today())
    assert not r.approved and "etf" in r.reason.lower()


def test_crypto_cap_10pct_blocks_150(db_session):
    snap = _snap(1000.0)
    order = _buy("BTCE.DE", 1, 150.0, asset_class=AssetClass.CRYPTO)
    r = risk.check(db_session, order, snap, date.today())
    assert not r.approved and "crypto" in r.reason.lower()


def test_stock_cap_considers_existing_position(db_session):
    existing = {
        "AAPL": PositionView(ticker="AAPL", qty=1, avg_entry_eur=150.0, last_price_eur=150.0),
    }
    snap = _snap(cash=850.0, positions=existing)
    # Adding 100 more => 250/1000 = 25% > 20%
    order = _buy("AAPL", 1, 100.0)
    assert not risk.check(db_session, order, snap, date.today()).approved


# --- Rule 2: portfolio floor ---

def test_floor_breach_blocks_buys(db_session):
    snap = _snap(499.0)
    order = _buy("AAPL", 1, 50.0)
    r = risk.check(db_session, order, snap, date.today())
    assert not r.approved and "floor" in r.reason


def test_floor_breach_blocks_sells_too(db_session):
    """Per project plan, a bot below the floor stops trading entirely."""
    positions = {
        "AAPL": PositionView(ticker="AAPL", qty=5, avg_entry_eur=100.0, last_price_eur=80.0),
    }
    snap = _snap(cash=0.0, positions=positions)  # total = 400 < 500
    order = _sell("AAPL", 5, 80.0)
    r = risk.check(db_session, order, snap, date.today())
    assert not r.approved and "floor" in r.reason


# --- Rule 3: daily trade limit ---

def test_daily_trade_limit(db_session):
    from core.db import Trade

    today = date.today()
    for i in range(5):
        db_session.add(
            Trade(
                bot_id=1,
                timestamp=datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc)
                          + timedelta(minutes=i),
                ticker="AAPL", side="BUY", qty=1, price=100.0,
                price_eur=100.0, fx_rate=1.0, fee_eur=1.0, signal_reason="t",
            )
        )
    db_session.commit()

    snap = _snap(1000.0)
    order = _buy("MSFT", 1, 100.0)
    r = risk.check(db_session, order, snap, today)
    assert not r.approved and "daily trade limit" in r.reason


# --- Rule 4: fee-aware skip ---

def test_fee_aware_skip_kills_tiny_profit(db_session):
    snap = _snap(1000.0)
    # Small €5 target profit, ~€1.25 one-way fee on a €100 Xetra trade.
    # Round-trip = €2.50 which is 50% of €5 profit -> blocked (>25% cap).
    order = _buy("SXR8.DE", 1, 100.0, asset_class=AssetClass.ETF, expected_profit_eur=5.0)
    r = risk.check(db_session, order, snap, date.today())
    assert not r.approved and "fee/profit" in r.reason


def test_fee_aware_allows_fat_profit(db_session):
    snap = _snap(1000.0)
    order = _buy("SXR8.DE", 1, 100.0, asset_class=AssetClass.ETF, expected_profit_eur=50.0)
    r = risk.check(db_session, order, snap, date.today())
    assert r.approved


# --- Cash check ---

def test_insufficient_cash(db_session):
    # Cash-poor but above floor (positions make up equity). Want to buy an
    # amount that fits the 35% ETF cap but exceeds available cash.
    positions = {
        "ZPRR.DE": PositionView(ticker="ZPRR.DE", qty=10, avg_entry_eur=90.0, last_price_eur=90.0),
    }
    snap = _snap(cash=50.0, positions=positions)  # total = 950, above floor
    order = _buy("SXR8.DE", 1, 100.0, asset_class=AssetClass.ETF)  # 100/950 = 10.5%
    r = risk.check(db_session, order, snap, date.today())
    assert not r.approved and "insufficient cash" in r.reason


# --- Sells pass basic sanity ---

def test_sell_requires_position(db_session):
    snap = _snap(1000.0)
    order = _sell("AAPL", 1, 100.0)
    r = risk.check(db_session, order, snap, date.today())
    assert not r.approved and "exceeds held" in r.reason


def test_sell_qty_bounded(db_session):
    positions = {
        "AAPL": PositionView(ticker="AAPL", qty=1, avg_entry_eur=100.0, last_price_eur=100.0),
    }
    snap = _snap(cash=1000.0, positions=positions)
    order = _sell("AAPL", 5, 100.0)
    assert not risk.check(db_session, order, snap, date.today()).approved


def test_sell_ok(db_session):
    positions = {
        "AAPL": PositionView(ticker="AAPL", qty=5, avg_entry_eur=100.0, last_price_eur=100.0),
    }
    snap = _snap(cash=1000.0, positions=positions)
    order = _sell("AAPL", 5, 100.0)
    assert risk.check(db_session, order, snap, date.today()).approved
