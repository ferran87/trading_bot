"""One-off correction: fix T212 fill prices stored in native currency (USD) as EUR.

Background
----------
The first run of _resolve_t212_pending_orders() stored T212 fill prices directly
as price_eur without applying the FX conversion.  For EUR-denominated stocks
(ASML.AS, BNP.PA) this was correct.  For USD-denominated stocks (MSFT, CRM,
COST, PG, GS, JPM, NVDA) the USD price was stored as-is, inflating cost by ~17%.

For example, MSFT was filled at $408.18 with fxRate=1.175, so the correct EUR
price is $408.18 / 1.175 = €347.38. The DB stored €408.18 — wrong.

This script:
  1. Fetches the full T212 demo order history.
  2. For each filled trade in the DB that has a broker_order_id:
       - Extracts fxRate from walletImpact (1.0 for EUR instruments).
       - Corrects price_eur = fill_price_native / fxRate.
       - Corrects fee_eur from actual wallet taxes.
  3. Recomputes each affected position.
  4. Prints a before/after summary.

Run once:
    python scripts/recorrect_t212_prices.py
"""
from __future__ import annotations

import sys
from pathlib import Path

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config import CONFIG  # noqa: F401 — loads .env / ANTHROPIC_API_KEY etc.
from core.db import Bot, Position, Trade, get_session
from core.broker import Trading212Broker


def _fetch_full_history(broker: Trading212Broker, max_pages: int = 20) -> dict[str, dict]:
    """Fetch all pages of /equity/history/orders, indexed by order ID."""
    history: dict[str, dict] = {}
    url: str | None = "/equity/history/orders"
    pages = 0
    while url and pages < max_pages:
        try:
            data = broker._get(url, params={"limit": 50})
        except Exception as exc:
            print(f"  ERROR fetching history page: {exc}")
            break
        for item in data.get("items", []):
            order = item.get("order", {})
            oid = str(order.get("id", ""))
            if oid:
                history[oid] = item
        next_path = data.get("nextPagePath")
        url = next_path if next_path else None
        pages += 1
    return history


def run() -> None:
    print("=== T212 price correction ===\n")

    with get_session() as session:
        # Find all filled trades that have a broker_order_id (T212 orders)
        filled_trades = (
            session.query(Trade)
            .filter(
                Trade.status == "filled",
                Trade.broker_order_id.isnot(None),
            )
            .all()
        )
        if not filled_trades:
            print("No filled trades with broker_order_id found. Nothing to correct.")
            return

        print(f"Found {len(filled_trades)} filled trade(s) with T212 order IDs.\n")

        # Determine demo vs live per bot
        bot_mode: dict[int, str] = {
            b.id: getattr(b, "trading_mode", "paper")
            for b in session.query(Bot).all()
        }

        # Fetch T212 history (one call per account mode)
        broker_paper = Trading212Broker(demo=True)
        history = _fetch_full_history(broker_paper)
        print(f"Fetched {len(history)} T212 order(s) from history.\n")

        corrected = 0
        skipped   = 0
        errors    = 0
        affected_positions: set[tuple[int, str]] = set()

        for trade in filled_trades:
            order_id = str(trade.broker_order_id)
            item = history.get(order_id)
            if item is None:
                print(f"  SKIP  {trade.ticker}: order {order_id} not in T212 history")
                skipped += 1
                continue

            order_data = item.get("order", {})
            fill_data  = item.get("fill", {})
            wallet     = fill_data.get("walletImpact", {})
            taxes      = wallet.get("taxes", [])

            # FX rate: "1 EUR = fxRate USD" (absent / 1.0 for EUR instruments)
            fx_rate = float(wallet.get("fxRate", 1) or 1)

            fill_price_native = float(
                fill_data.get("price")
                or order_data.get("filledPrice")
                or 0
            )
            if fill_price_native == 0:
                print(f"  SKIP  {trade.ticker}: no fill price in T212 response")
                skipped += 1
                continue

            correct_price_eur = (
                fill_price_native / fx_rate if fx_rate > 0 else fill_price_native
            )

            # Actual fee from wallet taxes.
            # T212 returns tax quantities as negative (wallet deductions),
            # so we take abs() to store fees as positive costs.
            fee_eur = abs(sum(
                float(t.get("quantity") or t.get("value") or 0)
                for t in taxes
            ))
            if fee_eur == 0:
                net_abs = abs(float(wallet.get("netValue") or 0))
                if net_abs > 0:
                    implied = float(fill_data.get("quantity") or trade.qty) * correct_price_eur
                    if abs(net_abs - implied) > 0.005:
                        fee_eur = abs(net_abs - implied)

            old_price = trade.price_eur
            old_fee   = trade.fee_eur
            delta     = correct_price_eur - old_price

            if abs(delta) < 0.005 and abs(fee_eur - old_fee) < 0.005:
                print(
                    f"  OK    {trade.ticker:8s}  price_eur={old_price:.4f}  fee={old_fee:.4f}  "
                    f"(no change needed)"
                )
                skipped += 1
                continue

            print(
                f"  FIX   {trade.ticker:8s}  "
                f"native={fill_price_native:.4f}  fx={fx_rate:.6f}  "
                f"price_eur {old_price:.4f} -> {correct_price_eur:.4f}  "
                f"fee {old_fee:.4f} -> {fee_eur:.4f}"
            )

            trade.price_eur = correct_price_eur
            if fee_eur > 0:
                trade.fee_eur = fee_eur
            corrected += 1
            affected_positions.add((trade.bot_id, trade.ticker))

        if corrected > 0:
            session.flush()  # push changes so _recompute_position sees them
            print(f"\nRecomputing {len(affected_positions)} position(s)...")
            from core.runner import _recompute_position
            for bot_id, ticker in sorted(affected_positions):
                _recompute_position(session, bot_id, ticker)
                pos = (
                    session.query(Position)
                    .filter(Position.bot_id == bot_id, Position.ticker == ticker)
                    .one_or_none()
                )
                if pos:
                    print(
                        f"  Position bot={bot_id} {ticker}: "
                        f"qty={pos.qty:.4f}  avg_entry_eur={pos.avg_entry_eur:.4f}"
                    )
                else:
                    print(f"  Position bot={bot_id} {ticker}: removed (net qty=0)")
            session.commit()
            print(f"\nCommitted. {corrected} trade(s) corrected, {skipped} skipped.")
        else:
            print(f"\nNo changes needed. {skipped} trade(s) already correct.")

        print("\n=== Done ===")


if __name__ == "__main__":
    run()
