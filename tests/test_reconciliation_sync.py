"""Tests for agents.reconciliation.sync_t212_positions (T212 as source of truth).

Covers the four attribution outcomes:
  * qty_mismatch held by exactly one bot  → adjusted
  * only_in_sqlite (T212 holds none)      → closed
  * only_in_t212 with a single-bot set    → created
  * ambiguous (ticker in 2+ bots, or orphan with several bots) → skipped
"""
from __future__ import annotations

from datetime import date

import pytest

from core.db import Position, Trade


@pytest.fixture
def _patched(monkeypatch):
    """Stub the broker portfolio, instrument maps and FX so the test is hermetic."""
    portfolio = [
        {"ticker": "AAA_EQ", "quantity": 7.0, "averagePrice": 100.0, "initialFillDate": ""},
        {"ticker": "BBB_EQ", "quantity": 9.0, "averagePrice": 100.0, "initialFillDate": ""},
        {"ticker": "DDD_EQ", "quantity": 6.0, "averagePrice": 100.0, "initialFillDate": ""},
        # CCC deliberately absent → bot holding it should be closed.
    ]

    class _FakeBroker:
        def __init__(self, *a, **k):
            pass

        def _get(self, path):
            return portfolio

    import core.broker as broker_mod
    import core.fx as fx_mod
    import agents.reconciliation as recon

    monkeypatch.setattr(broker_mod, "Trading212Broker", _FakeBroker)
    monkeypatch.setattr(fx_mod, "to_eur", lambda amount, currency, **k: amount)
    monkeypatch.setattr(
        recon, "_load_instrument_maps",
        lambda: (
            {"AAA_EQ": "AAA", "BBB_EQ": "BBB", "CCC_EQ": "CCC", "DDD_EQ": "DDD"},
            {"AAA": "AAA_EQ", "BBB": "BBB_EQ", "CCC": "CCC_EQ", "DDD": "DDD_EQ"},
            {"AAA": "EUR", "BBB": "EUR", "CCC": "EUR", "DDD": "EUR"},
        ),
    )


def test_sync_attribution_cases(db_session, _patched):
    from agents.reconciliation import sync_t212_positions

    # bot1: AAA 5 (T212 7), BBB 3, CCC 4 (T212 0).  bot2: BBB 2 (total BBB 5, T212 9).
    today = date.today()
    db_session.add_all([
        Position(bot_id=1, ticker="AAA", qty=5.0, avg_entry_eur=90.0, entry_date=today),
        Position(bot_id=1, ticker="BBB", qty=3.0, avg_entry_eur=90.0, entry_date=today),
        Position(bot_id=1, ticker="CCC", qty=4.0, avg_entry_eur=90.0, entry_date=today),
        Position(bot_id=2, ticker="BBB", qty=2.0, avg_entry_eur=90.0, entry_date=today),
    ])
    db_session.commit()

    result = sync_t212_positions([1, 2], demo=True, owner="X")
    assert result["error"] is None

    actions = {(a["ticker"], a["action"]) for a in result["applied"]}
    assert ("AAA", "adjusted") in actions     # single bot → adjust to 7
    assert ("CCC", "closed") in actions       # gone from T212 → close
    reasons = {(s["ticker"], s["reason"]) for s in result["skipped"]}
    assert ("BBB", "multi_bot_ambiguous") in reasons   # held by 2 bots
    assert ("DDD", "orphan_ambiguous") in reasons      # orphan, 2-bot set

    # ── Verify the DB reflects only the unambiguous changes ───────────────────
    db_session.expire_all()
    aaa = db_session.query(Position).filter_by(bot_id=1, ticker="AAA").one()
    assert aaa.qty == pytest.approx(7.0)
    assert db_session.query(Position).filter_by(bot_id=1, ticker="CCC").first() is None
    # Ambiguous tickers untouched.
    assert db_session.query(Position).filter_by(bot_id=1, ticker="BBB").one().qty == 3.0
    assert db_session.query(Position).filter_by(ticker="DDD").first() is None
    # Audit trail written for each applied change.
    recon_trades = db_session.query(Trade).filter_by(order_type="RECON").all()
    assert {t.ticker for t in recon_trades} == {"AAA", "CCC"}


def test_sync_creates_orphan_for_single_bot(db_session, _patched):
    from agents.reconciliation import sync_t212_positions

    # bot3 holds nothing; a single-bot set makes the DDD orphan unambiguous.
    result = sync_t212_positions([3], demo=True, owner="X")
    assert result["error"] is None
    created = {a["ticker"]: a for a in result["applied"] if a["action"] == "created"}
    assert "DDD" in created and created["DDD"]["to"] == pytest.approx(6.0)

    db_session.expire_all()
    ddd = db_session.query(Position).filter_by(bot_id=3, ticker="DDD").one()
    assert ddd.qty == pytest.approx(6.0)
    assert ddd.avg_entry_eur == pytest.approx(100.0)
