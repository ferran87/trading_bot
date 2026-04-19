"""Connection smoke test for IB Gateway / TWS.

Exits 0 if:
  * we can connect,
  * the connected account is a PAPER account (IBKR flags these
    with ``accountType == 'PAPER'`` or account id starting with ``DU``),
  * we can fetch at least the buying-power summary value.

Exits non-zero otherwise. Safe to run repeatedly — uses clientId=99
which should not collide with the bot (which uses 100+bot_id).

Usage:
    python scripts/check_ibkr.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")


def main() -> int:
    from ib_async import IB, util

    util.logToConsole(level="WARNING")

    host = os.getenv("IBKR_HOST", "127.0.0.1")
    port = int(os.getenv("IBKR_PORT", "4002"))
    client_id = 99

    print(f"Connecting to {host}:{port} (clientId={client_id}) ...")
    ib = IB()
    try:
        ib.connect(host, port, clientId=client_id, timeout=15)
    except Exception as e:
        print(f"FAIL: could not connect: {e}")
        print("Checklist:")
        print("  - Is IB Gateway / TWS running and logged in?")
        print("  - Configure -> Settings -> API -> Settings:")
        print("      Enable ActiveX and Socket Clients: YES")
        print("      Read-Only API: NO")
        print(f"      Socket port: {port}")
        print("      Trusted IPs: 127.0.0.1")
        return 1

    try:
        accounts = ib.managedAccounts()
        if not accounts:
            print("FAIL: connected but no managed accounts returned.")
            return 2
        account = accounts[0]
        is_paper = account.startswith(("DU", "DF"))  # IBKR paper prefixes
        print(f"Connected. Account: {account} (paper={is_paper})")
        if not is_paper:
            print(
                "WARNING: account does NOT look like paper (DU/DF prefix). "
                "Refusing to proceed — edit .env only once we confirm paper creds."
            )
            return 3

        summary = {row.tag: row for row in ib.accountSummary(account)}
        bp = summary.get("BuyingPower")
        avail = summary.get("AvailableFunds")
        nl = summary.get("NetLiquidation")

        def _fmt(s):
            return f"{s.value} {s.currency}" if s else "n/a"

        print(f"  NetLiquidation : {_fmt(nl)}")
        print(f"  AvailableFunds : {_fmt(avail)}")
        print(f"  BuyingPower    : {_fmt(bp)}")

        if bp is None:
            print("FAIL: could not read BuyingPower. Check API permissions.")
            return 4
    finally:
        ib.disconnect()

    print("\nOK - IBKR paper connection is healthy.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
