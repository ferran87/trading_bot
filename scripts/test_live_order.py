"""End-to-end smoke test for IBKRBroker against the paper account.

Places ONE small BUY, verifies the Fill fields, then immediately SELLs to
flatten the position. Never touches the bot's SQLite DB — everything lives
in local variables so a single failed test leaves zero side effects on the
production ledger.

Requires:
  * IB Gateway running, logged into a PAPER account (DU*/DF*)
  * data/contracts.json populated (run scripts/resolve_contracts.py)
  * .env set correctly (IBKR_HOST, IBKR_PORT)

Usage:
    python scripts/test_live_order.py                  # default: 1 share AAPL
    python scripts/test_live_order.py --ticker SXR8.DE --qty 1
    python scripts/test_live_order.py --yes            # skip confirmation prompt
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from core.broker import IBKRBroker
from core.types import AssetClass, Order, Side


def _print_fill(label: str, fill) -> None:
    print(f"  {label}:")
    print(f"    ticker         {fill.ticker}")
    print(f"    side           {fill.side.value}")
    print(f"    qty            {fill.qty}")
    print(f"    price (local)  {fill.price:.4f}")
    print(f"    price (EUR)    {fill.price_eur:.4f}")
    print(f"    fx_rate        {fill.fx_rate:.6f}")
    print(f"    fee (EUR)      {fill.fee_eur:.4f}")
    print(f"    timestamp      {fill.timestamp.isoformat()}")
    print(f"    broker_order_id {fill.broker_order_id}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", default="AAPL",
                        help="yfinance-style ticker from contracts.json")
    parser.add_argument("--qty", type=float, default=1.0,
                        help="Shares to buy then sell (default: 1)")
    parser.add_argument("--yes", action="store_true",
                        help="Skip confirmation prompt")
    args = parser.parse_args()

    print(f"Paper-account smoke test: BUY {args.qty} {args.ticker} @ MKT, "
          f"then SELL {args.qty} {args.ticker} @ MKT.")
    print("This bypasses the bot DB. Positions settle in the IBKR paper "
          "account only.")
    if not args.yes:
        resp = input("Proceed? [y/N] ").strip().lower()
        if resp not in ("y", "yes"):
            print("Aborted.")
            return 1

    broker = IBKRBroker()
    try:
        broker.connect()
    except Exception as e:
        print(f"ERROR connecting to IBKR: {e}")
        return 2

    # Delayed market data is usually fine for paper accounts without a
    # real-time subscription; Gateway can still price market orders.
    try:
        broker._ib.reqMarketDataType(3)
    except Exception:
        pass

    buy = Order(
        bot_id=0, ticker=args.ticker, side=Side.BUY, qty=args.qty,
        signal_reason="live_smoke_test_buy", ref_price_eur=0.0,
        asset_class=AssetClass.STOCK,
    )
    sell = Order(
        bot_id=0, ticker=args.ticker, side=Side.SELL, qty=args.qty,
        signal_reason="live_smoke_test_sell", ref_price_eur=0.0,
        asset_class=AssetClass.STOCK,
    )

    exit_code = 0
    try:
        print("\nPlacing BUY...")
        buy_fill = broker.place_market_order(buy)
        _print_fill("BUY fill", buy_fill)

        print("\nPlacing SELL to flatten...")
        sell_fill = broker.place_market_order(sell)
        _print_fill("SELL fill", sell_fill)

        realised_pnl_eur = (
            sell_fill.price_eur * sell_fill.qty
            - buy_fill.price_eur * buy_fill.qty
            - buy_fill.fee_eur - sell_fill.fee_eur
        )
        print(f"\nRound-trip P&L (fees included): {realised_pnl_eur:+.4f} EUR")
        print("OK - live smoke test completed successfully.")
    except Exception as e:
        print(f"\nERROR during smoke test: {e}")
        exit_code = 3
    finally:
        broker.disconnect()

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
