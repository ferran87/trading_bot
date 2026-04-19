"""Unit tests for IBKRBroker fill parsing — ib_async fully mocked.

We never touch a real Gateway here. Each test crafts a fake ``Trade``
object with the subset of attributes the broker actually reads:

    trade.fills[i].execution.shares
    trade.fills[i].execution.price
    trade.fills[i].execution.orderId
    trade.fills[i].time
    trade.fills[i].commissionReport.commission
    trade.fills[i].commissionReport.currency
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from core.broker import IBKRBroker
from core.types import AssetClass, Order, Side


@pytest.fixture
def contracts_json(tmp_path, monkeypatch):
    """Write a tiny contracts.json into tmp DATA_DIR and repoint core.config."""
    from core import config as config_mod

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "contracts.json").write_text(json.dumps({
        "AAPL": {
            "yf_ticker": "AAPL", "symbol": "AAPL", "exchange": "SMART",
            "primary_exchange": "NASDAQ", "currency": "USD", "sec_type": "STK",
            "con_id": 265598, "local_symbol": "AAPL", "long_name": "APPLE INC",
        },
        "SXR8.DE": {
            "yf_ticker": "SXR8.DE", "symbol": "SXR8", "exchange": "SMART",
            "primary_exchange": "IBIS", "currency": "EUR", "sec_type": "STK",
            "con_id": 75776072, "local_symbol": "SXR8",
            "long_name": "ISHARES CORE S&P 500",
        },
    }), encoding="utf-8")
    monkeypatch.setattr(config_mod, "DATA_DIR", data_dir)
    return data_dir


def _make_fill(qty, price, order_id=1, commission=None, currency="USD",
               ts=None):
    """Fabricate an ib_async-shaped Fill."""
    cr = None
    if commission is not None:
        cr = SimpleNamespace(commission=commission, currency=currency)
    return SimpleNamespace(
        execution=SimpleNamespace(shares=qty, price=price, orderId=order_id),
        commissionReport=cr,
        time=ts or datetime(2026, 4, 18, 14, 30, tzinfo=timezone.utc),
    )


def _fake_trade(fills, status="Filled"):
    return SimpleNamespace(
        fills=fills,
        orderStatus=SimpleNamespace(status=status, avgFillPrice=sum(
            f.execution.shares * f.execution.price for f in fills
        ) / max(sum(f.execution.shares for f in fills), 1)),
        isDone=lambda: status in ("Filled", "Cancelled"),
    )


@pytest.fixture
def no_fx(monkeypatch):
    """Freeze FX: 1 USD = 0.9 EUR, 1 EUR = 1 EUR, everything else = 1."""
    from core import fx

    def fake_eur_per_unit(ccy, as_of=None):
        return {"USD": 0.9, "EUR": 1.0, "GBP": 1.15, "CHF": 1.05}.get(ccy, 1.0)

    def fake_to_eur(amount, ccy, as_of=None):
        if amount == 0 or ccy == "EUR":
            return amount
        return amount * fake_eur_per_unit(ccy)

    monkeypatch.setattr(fx, "eur_per_unit", fake_eur_per_unit)
    monkeypatch.setattr(fx, "to_eur", fake_to_eur)


def test_build_fill_single_fill_usd(contracts_json, no_fx):
    broker = IBKRBroker()
    order = Order(
        bot_id=1, ticker="AAPL", side=Side.BUY, qty=10,
        signal_reason="test", ref_price_eur=180.0, asset_class=AssetClass.STOCK,
    )
    trade = _fake_trade([_make_fill(qty=10, price=200.0, commission=1.0,
                                     currency="USD")])
    _, entry = broker._contract_for("AAPL")

    fill = broker._build_fill(order, trade, entry)

    assert fill.ticker == "AAPL"
    assert fill.side is Side.BUY
    assert fill.qty == 10
    assert fill.price == pytest.approx(200.0)
    assert fill.price_eur == pytest.approx(200.0 * 0.9)   # USD->EUR @ 0.9
    assert fill.fx_rate == pytest.approx(0.9)
    assert fill.fee_eur == pytest.approx(1.0 * 0.9)


def test_build_fill_partial_fills_qty_weighted(contracts_json, no_fx):
    broker = IBKRBroker()
    order = Order(
        bot_id=1, ticker="AAPL", side=Side.BUY, qty=10,
        signal_reason="test", ref_price_eur=180.0, asset_class=AssetClass.STOCK,
    )
    # Two partials: 3@198, 7@202. Avg = (3*198 + 7*202)/10 = 200.80
    trade = _fake_trade([
        _make_fill(qty=3, price=198.0, commission=0.3, currency="USD"),
        _make_fill(qty=7, price=202.0, commission=0.7, currency="USD"),
    ])
    _, entry = broker._contract_for("AAPL")

    fill = broker._build_fill(order, trade, entry)

    assert fill.qty == pytest.approx(10)
    assert fill.price == pytest.approx(200.80)
    assert fill.fee_eur == pytest.approx(1.0 * 0.9)


def test_build_fill_missing_commission_falls_back_to_estimate(contracts_json, no_fx, monkeypatch):
    """IBKR paper sometimes omits commissionReport — we must still book a cost."""
    from core import broker as broker_mod

    monkeypatch.setattr(broker_mod, "estimate_fee_eur",
                        lambda ticker, qty, price: 3.14)

    broker = IBKRBroker()
    order = Order(
        bot_id=1, ticker="AAPL", side=Side.BUY, qty=5,
        signal_reason="test", ref_price_eur=180.0, asset_class=AssetClass.STOCK,
    )
    trade = _fake_trade([_make_fill(qty=5, price=200.0, commission=None)])
    _, entry = broker._contract_for("AAPL")

    fill = broker._build_fill(order, trade, entry)

    assert fill.fee_eur == pytest.approx(3.14)


def test_build_fill_eur_contract_no_conversion(contracts_json, no_fx):
    broker = IBKRBroker()
    order = Order(
        bot_id=1, ticker="SXR8.DE", side=Side.BUY, qty=2,
        signal_reason="test", ref_price_eur=500.0, asset_class=AssetClass.ETF,
    )
    trade = _fake_trade([_make_fill(qty=2, price=500.0, commission=1.0,
                                     currency="EUR")])
    _, entry = broker._contract_for("SXR8.DE")

    fill = broker._build_fill(order, trade, entry)

    assert fill.price_eur == pytest.approx(500.0)
    assert fill.fx_rate == pytest.approx(1.0)
    assert fill.fee_eur == pytest.approx(1.0)


def test_build_fill_raises_on_empty_fills(contracts_json, no_fx):
    broker = IBKRBroker()
    order = Order(
        bot_id=1, ticker="AAPL", side=Side.BUY, qty=10,
        signal_reason="test", ref_price_eur=180.0, asset_class=AssetClass.STOCK,
    )
    trade = _fake_trade([])
    _, entry = broker._contract_for("AAPL")

    with pytest.raises(RuntimeError, match="trade.fills is empty"):
        broker._build_fill(order, trade, entry)


def test_contract_for_unknown_ticker_raises(contracts_json):
    broker = IBKRBroker()
    with pytest.raises(RuntimeError, match="no contract for 'NFLX'"):
        broker._contract_for("NFLX")


def test_contract_for_missing_cache_raises(tmp_path, monkeypatch):
    """Fresh repo with no contracts.json should point user to the script."""
    from core import config as config_mod

    empty_data_dir = tmp_path / "data"
    empty_data_dir.mkdir()
    monkeypatch.setattr(config_mod, "DATA_DIR", empty_data_dir)

    broker = IBKRBroker()
    with pytest.raises(RuntimeError, match="resolve_contracts.py"):
        broker._contract_for("AAPL")


def test_place_market_order_requires_connect(contracts_json):
    broker = IBKRBroker()
    order = Order(
        bot_id=1, ticker="AAPL", side=Side.BUY, qty=1,
        signal_reason="test", ref_price_eur=180.0, asset_class=AssetClass.STOCK,
    )
    with pytest.raises(RuntimeError, match="not connected"):
        broker.place_market_order(order)
