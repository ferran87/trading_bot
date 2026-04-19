"""MockBroker sanity: fill price near ref, fee stamped from venue table."""
from __future__ import annotations

from core.broker import MockBroker, estimate_fee_eur, venue_for
from core.types import AssetClass, Order, Side


def test_venue_resolution():
    assert venue_for("SXR8.DE") == "xetra_eur"
    assert venue_for("AAPL") == "nasdaq_usd"
    assert venue_for("BTCE.DE") == "crypto_etp_eur"
    assert venue_for("UNKNOWN") == "xetra_eur"  # safe default


def test_fee_minimum_applied():
    # Xetra EUR: per_trade 1.25 + 0.05% * 50 = 1.275 -> 1.275 > 1.25 min -> 1.275
    fee = estimate_fee_eur("SXR8.DE", qty=1, price_eur=50.0)
    assert fee == 1.25 + 0.0005 * 50

    # Crypto ETP: per_trade 2.50 + 0.1% * 10 = 2.51 > 2.50 min
    fee = estimate_fee_eur("BTCE.DE", qty=1, price_eur=10.0)
    assert abs(fee - (2.50 + 0.0010 * 10)) < 1e-9


def test_mock_fill_near_ref_price():
    broker = MockBroker(seed=0)  # deterministic
    order = Order(
        bot_id=1, ticker="SXR8.DE", side=Side.BUY, qty=1,
        ref_price_eur=100.0, signal_reason="t", asset_class=AssetClass.ETF,
    )
    fill = broker.place_market_order(order)
    # Slippage is capped at 5 bps -> fill within [99.95, 100.05]
    assert 99.94 <= fill.price_eur <= 100.06
    assert fill.fee_eur > 0
    assert fill.fx_rate == 1.0


def test_mock_zero_slippage_when_seed_none():
    broker = MockBroker(seed=None)
    order = Order(
        bot_id=1, ticker="SXR8.DE", side=Side.BUY, qty=2,
        ref_price_eur=100.0, signal_reason="t", asset_class=AssetClass.ETF,
    )
    fill = broker.place_market_order(order)
    assert fill.price_eur == 100.0
