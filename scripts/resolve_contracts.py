"""Resolve each ticker in watchlists.yaml against IBKR and cache the result.

IBKR does NOT accept yfinance's suffixed tickers (``SXR8.DE``). We have to
ask ``reqContractDetails`` for the canonical (symbol, exchange, currency,
secType) tuple and persist it to ``data/contracts.json``. IBKRBroker then
looks up contracts from that cache rather than hitting the API every run.

Heuristics used to guess the initial contract from a yfinance symbol:

   ``FOO.DE``  -> Stock(FOO, exchange=IBIS,   currency=EUR)   # Xetra
   ``FOO.L``   -> Stock(FOO, exchange=LSE,    currency=GBP)
   ``FOO.AS``  -> Stock(FOO, exchange=AEB,    currency=EUR)
   ``FOO.PA``  -> Stock(FOO, exchange=SBF,    currency=EUR)   # Euronext Paris
   ``FOO.SW``  -> Stock(FOO, exchange=EBS,    currency=CHF)   # SIX Swiss
   plain ``FOO`` -> Stock(FOO, exchange=SMART, currency=USD)

SMART routing is used for US stocks — IBKR picks the best venue.

Any ticker whose ``reqContractDetails`` returns zero candidates is
reported but does NOT abort the run; fix the watchlists then re-run.

Usage:
    python scripts/resolve_contracts.py
    python scripts/resolve_contracts.py --refresh   # ignore cache
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

from core.config import CONFIG, DATA_DIR

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

CONTRACTS_PATH = DATA_DIR / "contracts.json"


@dataclass
class ResolvedContract:
    """What we cache per yfinance ticker."""

    yf_ticker: str
    symbol: str
    exchange: str
    primary_exchange: str
    currency: str
    sec_type: str
    con_id: int
    local_symbol: str
    long_name: str


SUFFIX_MAP = {
    ".DE": ("IBIS", "EUR"),
    ".L":  ("LSEETF", "GBP"),
    ".AS": ("AEB", "EUR"),
    ".PA": ("SBF", "EUR"),
    ".SW": ("EBS", "CHF"),
    ".MI": ("BVME", "EUR"),
    ".MC": ("BM", "EUR"),
}


def _guess_candidates(yf_ticker: str, asset_class: str):
    """Return ib_async Contract objects to try, in order of preference.

    Multiple candidates because IBKR sometimes stores the same security
    under slightly different venue codes (e.g. IBIS vs IBIS2 for Xetra).
    """
    from ib_async import Forex, Stock

    # Forex pairs never appear in watchlists, but let's be safe.
    if asset_class == "forex":
        return [Forex(yf_ticker)]

    candidates = []
    for suffix, (exchange, currency) in SUFFIX_MAP.items():
        if yf_ticker.endswith(suffix):
            symbol = yf_ticker[: -len(suffix)]
            if suffix == ".DE":
                for ex in ("IBIS", "SMART", "IBIS2", "GETTEX", "TRADEGATE"):
                    candidates.append(Stock(symbol, ex, currency))
            elif suffix == ".L":
                for ex in ("LSE", "LSEETF", "SMART"):
                    candidates.append(Stock(symbol, ex, currency))
            else:
                candidates.append(Stock(symbol, exchange, currency))
                candidates.append(Stock(symbol, "SMART", currency))
            return candidates

    # No suffix -> assume US listing, use SMART routing.
    candidates.append(Stock(yf_ticker, "SMART", "USD"))
    return candidates


def _collect_tickers() -> list[tuple[str, str]]:
    """Return list of (yf_ticker, asset_class) from watchlists.yaml."""
    venue_map = CONFIG.watchlists.get("venue", {})
    out = []
    for t, info in venue_map.items():
        out.append((t, info.get("class", "stock")))
    return out


def _to_resolved(yf_ticker: str, cd) -> ResolvedContract:
    c = cd.contract
    return ResolvedContract(
        yf_ticker=yf_ticker,
        symbol=c.symbol,
        exchange=c.exchange,
        primary_exchange=c.primaryExchange or "",
        currency=c.currency,
        sec_type=c.secType,
        con_id=c.conId,
        local_symbol=c.localSymbol or "",
        long_name=cd.longName or "",
    )


def _expected_currency(yf_ticker: str) -> str | None:
    for suffix, (_, currency) in SUFFIX_MAP.items():
        if yf_ticker.endswith(suffix):
            return currency
    return "USD" if "." not in yf_ticker else None


def resolve_one(ib, yf_ticker: str, asset_class: str) -> ResolvedContract | None:
    expected_ccy = _expected_currency(yf_ticker)
    for candidate in _guess_candidates(yf_ticker, asset_class):
        details = ib.reqContractDetails(candidate)
        if details:
            return _to_resolved(yf_ticker, details[0])

    # Fuzzy symbol search. Reject matches whose currency doesn't match the
    # suffix-implied currency — we don't want "CEUG.DE" to silently resolve
    # to a London GBP-hedged share class.
    suffix_match = next((s for s in SUFFIX_MAP if yf_ticker.endswith(s)), None)
    bare_symbol = yf_ticker[: -len(suffix_match)] if suffix_match else yf_ticker
    matches = ib.reqMatchingSymbols(bare_symbol) or []
    for m in matches:
        c = m.contract
        if c.secType not in ("STK", "ETF"):
            continue
        if expected_ccy and c.currency != expected_ccy:
            continue
        from ib_async import Stock
        probe = Stock(conId=c.conId, exchange=c.primaryExchange or c.exchange)
        details = ib.reqContractDetails(probe)
        if details:
            return _to_resolved(yf_ticker, details[0])
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh", action="store_true",
                        help="Ignore existing cache and re-resolve all tickers.")
    args = parser.parse_args()

    from ib_async import IB, util

    util.logToConsole(level="WARNING")

    host = os.getenv("IBKR_HOST", "127.0.0.1")
    port = int(os.getenv("IBKR_PORT", "4002"))

    cache: dict[str, dict] = {}
    if CONTRACTS_PATH.exists() and not args.refresh:
        cache = json.loads(CONTRACTS_PATH.read_text(encoding="utf-8"))

    tickers = _collect_tickers()
    to_resolve = [(t, cls) for t, cls in tickers if t not in cache]
    if not to_resolve and not args.refresh:
        print(f"Cache has all {len(tickers)} tickers. Nothing to do. "
              f"(Pass --refresh to force.)")
        return 0

    ib = IB()
    ib.connect(host, port, clientId=98, timeout=15)
    account = ib.managedAccounts()[0] if ib.managedAccounts() else "?"
    if not account.startswith(("DU", "DF")):
        print(f"REFUSING: connected account {account} is not paper.")
        ib.disconnect()
        return 1
    print(f"Connected to paper account {account}. Resolving "
          f"{len(to_resolve) if not args.refresh else len(tickers)} tickers...")

    resolved: list[str] = []
    missing: list[str] = []
    target = tickers if args.refresh else to_resolve
    try:
        for yf_ticker, asset_class in target:
            try:
                rc = resolve_one(ib, yf_ticker, asset_class)
            except Exception as e:
                print(f"  [ERR ] {yf_ticker:10s}: {e}")
                missing.append(yf_ticker)
                continue
            if rc is None:
                print(f"  [MISS] {yf_ticker:10s}: no contract details returned")
                missing.append(yf_ticker)
                continue
            cache[yf_ticker] = asdict(rc)
            resolved.append(yf_ticker)
            print(f"  [ OK ] {yf_ticker:10s} -> {rc.symbol}/{rc.exchange}"
                  f" {rc.currency} conId={rc.con_id}  ({rc.long_name})")
    finally:
        ib.disconnect()

    CONTRACTS_PATH.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")

    print()
    print(f"Resolved: {len(resolved)}   Missing: {len(missing)}   "
          f"Total cached: {len(cache)}")
    print(f"Cache written to {CONTRACTS_PATH}")
    if missing:
        print("Missing tickers (fix in watchlists.yaml and re-run):")
        for t in missing:
            print(f"  - {t}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
