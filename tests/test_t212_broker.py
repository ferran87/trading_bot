"""Unit tests for Trading212Broker — all HTTP calls mocked via unittest.mock.

No real network calls are made. Each test patches ``requests.get`` /
``requests.post`` at the level they're imported inside ``core.broker``.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from core.broker import Trading212Broker, estimate_fee_eur
from core.types import AssetClass, Fill, Order, Side


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def instruments_json(tmp_path, monkeypatch):
    """Write a tiny t212_instruments.json and point DATA_DIR at it."""
    from core import config as config_mod

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "t212_instruments.json").write_text(
        json.dumps({
            "AAPL": {
                "t212_ticker": "AAPL_US_EQ",
                "isin": "US0378331005",
                "name": "Apple",
                "currency": "USD",
                "type": "STOCK",
            },
            "SXR8.DE": {
                "t212_ticker": "SXR8D_EV_EQ",
                "isin": "IE00B5BMR087",
                "name": "iShares Core S&P 500 UCITS",
                "currency": "EUR",
                "type": "ETF",
            },
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_mod, "DATA_DIR", data_dir)
    return data_dir


@pytest.fixture
def broker(instruments_json, monkeypatch):
    """Return a Trading212Broker wired to demo mode with fake credentials."""
    monkeypatch.setenv("T212_API_KEY_PAPER", "test-key-123")
    monkeypatch.setenv("T212_API_SECRET_PAPER", "test-secret-abc")
    monkeypatch.setenv("T212_DEMO", "1")
    b = Trading212Broker(demo=True)
    return b


def _order(ticker="AAPL", side=Side.BUY, qty=5.0, ref_price_eur=180.0):
    return Order(
        bot_id=1,
        ticker=ticker,
        side=side,
        qty=qty,
        signal_reason="test",
        ref_price_eur=ref_price_eur,
        asset_class=AssetClass.STOCK,
    )


def _mock_response(data, status=200):
    """Build a mock requests.Response."""
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = data
    resp.raise_for_status = MagicMock()
    return resp


# ---------------------------------------------------------------------------
# connect()
# ---------------------------------------------------------------------------

class TestConnect:
    def test_connect_logs_cash(self, broker, capsys):
        summary = {"cash": {"free": 9500.0, "total": 10000.0, "currencyCode": "EUR"}}
        with patch("requests.get", return_value=_mock_response(summary)):
            broker.connect()  # should not raise

    def test_connect_missing_key_raises(self, instruments_json, monkeypatch):
        monkeypatch.delenv("T212_API_KEY_PAPER", raising=False)
        monkeypatch.delenv("T212_API_KEY", raising=False)
        monkeypatch.setenv("T212_API_SECRET_PAPER", "some-secret")
        b = Trading212Broker(demo=True)
        with pytest.raises(RuntimeError, match="T212_API_KEY_PAPER not set"):
            b.connect()

    def test_connect_missing_secret_raises(self, instruments_json, monkeypatch):
        monkeypatch.setenv("T212_API_KEY_PAPER", "some-key")
        monkeypatch.delenv("T212_API_SECRET_PAPER", raising=False)
        monkeypatch.delenv("T212_API_SECRET", raising=False)
        b = Trading212Broker(demo=True)
        with pytest.raises(RuntimeError, match="T212_API_SECRET_PAPER not set"):
            b.connect()

    def test_disconnect_is_noop(self, broker):
        broker.disconnect()  # no exception


# ---------------------------------------------------------------------------
# _resolve_ticker
# ---------------------------------------------------------------------------

class TestInstrumentResolution:
    def test_known_us_ticker(self, broker):
        assert broker._resolve_ticker("AAPL") == "AAPL_US_EQ"

    def test_known_eu_ticker(self, broker):
        assert broker._resolve_ticker("SXR8.DE") == "SXR8D_EV_EQ"

    def test_unknown_ticker_raises(self, broker):
        with pytest.raises(RuntimeError, match="no T212 instrument"):
            broker._resolve_ticker("UNKNOWN.XY")

    def test_missing_instruments_file_raises(self, monkeypatch, tmp_path):
        from core import config as config_mod
        monkeypatch.setattr(config_mod, "DATA_DIR", tmp_path)
        monkeypatch.setenv("T212_API_KEY", "k")
        b = Trading212Broker(demo=True)
        with pytest.raises(RuntimeError, match="t212_instruments.json missing"):
            b._resolve_ticker("AAPL")


# ---------------------------------------------------------------------------
# place_market_order — BUY
# ---------------------------------------------------------------------------

class TestPlaceMarketOrderBuy:
    def test_eur_buy_no_fx_fee(self, broker):
        """EUR-denominated ETF: no FX fee, price in EUR."""
        order_resp = {
            "id": 42,
            "ticker": "SXR8D_EV_EQ",
            "status": "FILLED",
            "quantity": 3.0,
            "filledQuantity": 3.0,
            "filledPrice": 521.0,
            "taxes": [],
        }
        order = _order(ticker="SXR8.DE", side=Side.BUY, qty=3.0, ref_price_eur=521.0)

        with patch("requests.post", return_value=_mock_response(order_resp)):
            fill = broker.place_market_order(order)

        assert fill.ticker == "SXR8.DE"
        assert fill.side is Side.BUY
        assert fill.qty == 3.0
        assert fill.price == pytest.approx(521.0)
        assert fill.fx_rate == pytest.approx(1.0)
        assert fill.fee_eur == pytest.approx(0.0)
        assert fill.broker_order_id == "42"

    def test_usd_buy_estimates_fx_fee(self, broker, monkeypatch):
        """USD stock: if taxes array is empty, 0.15% FX fee is estimated."""
        order_resp = {
            "id": 99,
            "ticker": "AAPL_US_EQ",
            "status": "FILLED",
            "quantity": 5.0,
            "filledQuantity": 5.0,
            "filledPrice": 195.0,
            "taxes": [],
        }
        order = _order(ticker="AAPL", side=Side.BUY, qty=5.0, ref_price_eur=175.0)

        # Patch fx.eur_per_unit to return a fixed rate
        from core import fx as fx_mod
        monkeypatch.setattr(fx_mod, "eur_per_unit", lambda ccy, **kw: 0.90)

        with patch("requests.post", return_value=_mock_response(order_resp)):
            fill = broker.place_market_order(order)

        assert fill.ticker == "AAPL"
        assert fill.qty == 5.0
        assert fill.fx_rate == pytest.approx(0.90)
        # price_eur = 195 * 0.90 = 175.5
        assert fill.price_eur == pytest.approx(175.5)
        # estimated fee: 5 * 195 * 0.90 * 0.0015 = 1.31625
        expected_fee = 5 * 195 * 0.90 * 0.0015
        assert fill.fee_eur == pytest.approx(expected_fee, rel=1e-4)

    def test_usd_buy_uses_actual_tax(self, broker, monkeypatch):
        """If T212 returns a taxes entry, we use it instead of the estimate."""
        order_resp = {
            "id": 77,
            "ticker": "AAPL_US_EQ",
            "status": "FILLED",
            "quantity": 2.0,
            "filledQuantity": 2.0,
            "filledPrice": 200.0,
            "taxes": [{"name": "Currency conversion fee", "quantity": 0.54, "currencyCode": "EUR"}],
        }
        order = _order(ticker="AAPL", side=Side.BUY, qty=2.0, ref_price_eur=180.0)

        from core import fx as fx_mod
        monkeypatch.setattr(fx_mod, "eur_per_unit", lambda ccy, **kw: 0.90)

        with patch("requests.post", return_value=_mock_response(order_resp)):
            fill = broker.place_market_order(order)

        # Tax from response (EUR, no conversion needed)
        assert fill.fee_eur == pytest.approx(0.54)


# ---------------------------------------------------------------------------
# place_market_order — SELL
# ---------------------------------------------------------------------------

class TestPlaceMarketOrderSell:
    def test_sell_uses_negative_quantity(self, broker):
        """T212 SELL = negative quantity in the POST payload."""
        order_resp = {
            "id": 55,
            "ticker": "AAPL_US_EQ",
            "status": "FILLED",
            "quantity": -3.0,
            "filledQuantity": 3.0,
            "filledPrice": 185.0,
            "taxes": [],
        }
        order = _order(ticker="AAPL", side=Side.SELL, qty=3.0, ref_price_eur=185.0)
        captured_payload: list[dict] = []

        def fake_post(url, json=None, **kwargs):
            captured_payload.append(json or {})
            return _mock_response(order_resp)

        from core import fx as fx_mod
        with patch("requests.post", side_effect=fake_post), \
             patch.object(fx_mod, "eur_per_unit", return_value=0.91):
            fill = broker.place_market_order(order)

        assert captured_payload[0]["quantity"] == pytest.approx(-3.0)
        assert fill.side is Side.SELL
        assert fill.qty == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# place_market_order — edge cases
# ---------------------------------------------------------------------------

class TestPlaceMarketOrderEdgeCases:
    def test_zero_qty_returns_empty_fill(self, broker):
        """qty < 1 after floor returns a zero fill rather than raising."""
        order = _order(ticker="AAPL", qty=0.3)
        # Should not call requests.post at all
        with patch("requests.post") as mock_post:
            fill = broker.place_market_order(order)
        mock_post.assert_not_called()
        assert fill.qty == 0.0

    def test_order_not_filled_raises(self, broker):
        """A REJECTED order should raise RuntimeError."""
        order_resp = {
            "id": 11,
            "ticker": "AAPL_US_EQ",
            "status": "REJECTED",
            "quantity": 1.0,
        }
        order = _order(ticker="AAPL", qty=1.0)
        with patch("requests.post", return_value=_mock_response(order_resp)):
            with pytest.raises(RuntimeError, match="REJECTED"):
                broker.place_market_order(order)

    def test_polls_until_filled(self, broker, monkeypatch):
        """If order is PENDING on POST, broker polls GET until FILLED."""
        pending_resp = {"id": 33, "ticker": "AAPL_US_EQ", "status": "PENDING", "quantity": 1.0}
        filled_resp = {
            "id": 33, "ticker": "AAPL_US_EQ", "status": "FILLED",
            "quantity": 1.0, "filledQuantity": 1.0, "filledPrice": 180.0, "taxes": [],
        }
        monkeypatch.setattr("time.sleep", lambda _: None)

        from core import fx as fx_mod
        monkeypatch.setattr(fx_mod, "eur_per_unit", lambda ccy, **kw: 0.90)

        call_count = 0

        def fake_get(url, **kwargs):
            nonlocal call_count
            call_count += 1
            return _mock_response(filled_resp)

        with patch("requests.post", return_value=_mock_response(pending_resp)), \
             patch("requests.get", side_effect=fake_get):
            fill = broker.place_market_order(_order(ticker="AAPL", qty=1.0))

        assert fill.qty == 1.0
        assert call_count >= 1  # at least one poll


# ---------------------------------------------------------------------------
# estimate_fee_eur — T212 fee table
# ---------------------------------------------------------------------------

class TestT212Fees:
    def test_eur_venue_zero_fee(self, monkeypatch):
        """EUR-denominated positions cost nothing on T212."""
        monkeypatch.setenv("BROKER_BACKEND", "t212")
        # SXR8.DE is on xetra_eur venue → 0 fee
        from core.broker import venue_for
        assert venue_for("SXR8.DE") == "xetra_eur"
        fee = estimate_fee_eur("SXR8.DE", qty=10, price_eur=500.0, backend="t212")
        assert fee == pytest.approx(0.0)

    def test_usd_venue_015pct_fee(self):
        """US stocks incur 0.15% FX fee on T212."""
        # AAPL is on nasdaq_usd venue → 0.15% of notional
        fee = estimate_fee_eur("AAPL", qty=5, price_eur=180.0, backend="t212")
        expected = 5 * 180.0 * 0.0015
        assert fee == pytest.approx(expected, rel=1e-6)

    def test_ibkr_fee_unchanged(self):
        """IBKR fee table is not affected."""
        fee = estimate_fee_eur("SXR8.DE", qty=10, price_eur=500.0, backend="ibkr")
        # xetra_eur: per_trade=1.25 + 0.05% = 1.25 + 2.50 = 3.75; min=1.25 → 3.75
        assert fee == pytest.approx(3.75, rel=1e-4)
