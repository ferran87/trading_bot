"""Build data/t212_instruments.json — the yfinance ticker → T212 instrument map.

Run this once before switching to BROKER_BACKEND=t212, and re-run whenever
the watchlist changes or T212 adds new instruments.

Usage
-----
    # Against the demo environment (safe — read-only):
    python scripts/resolve_t212_instruments.py

    # Against the live environment:
    python scripts/resolve_t212_instruments.py --live

The script:
  1. Downloads every available instrument from GET /equity/metadata/instruments
     (paginated in batches of 50).
  2. Builds lookup tables by ISIN and by bare symbol.
  3. Iterates through every ticker in watchlists.yaml and tries to match:
       a. By ISIN   — most reliable for EU tickers (SXR8.DE etc.)
       b. By symbol — used for US tickers (AAPL → AAPL_US_EQ)
  4. Writes data/t212_instruments.json with the matched entries.
  5. Prints a summary of matches and any unmatched tickers that need a
     manual mapping.

ISINs for EU instruments are fetched from yfinance's .info dict (slow but
only done once). They are cached to data/yf_isin_cache.json so subsequent
runs are fast.

Manual overrides
----------------
Create data/t212_instruments_override.json with entries like:
    {
      "BTCE.DE": {"t212_ticker": "BTCE_EV_EQ", "currency": "EUR", "name": "..."}
    }
These take precedence over the auto-matched entries. Useful for tickers that
yfinance or T212 name differently.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

# Make the project root importable.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env", override=True)

from core.config import DATA_DIR, CONFIG  # noqa: E402  (after sys.path fix)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

INSTRUMENTS_OUT = DATA_DIR / "t212_instruments.json"
ISIN_CACHE = DATA_DIR / "yf_isin_cache.json"
OVERRIDE_FILE = DATA_DIR / "t212_instruments_override.json"

DEMO_URL = "https://demo.trading212.com/api/v0"
LIVE_URL = "https://live.trading212.com/api/v0"


# ---------------------------------------------------------------------------
# T212 API helpers
# ---------------------------------------------------------------------------

def _credentials(demo: bool = True) -> tuple[str, str]:
    suffix = "PAPER" if demo else "LIVE"
    key = os.environ.get(f"T212_API_KEY_{suffix}", "").strip() or os.environ.get("T212_API_KEY", "").strip()
    secret = os.environ.get(f"T212_API_SECRET_{suffix}", "").strip() or os.environ.get("T212_API_SECRET", "").strip()
    if not key:
        raise SystemExit(f"ERROR: T212_API_KEY_{suffix} not set in .env")
    if not secret:
        raise SystemExit(
            f"ERROR: T212_API_SECRET_{suffix} not set in .env\n"
            "The secret is only shown once at key creation time. Delete the key in T212, "
            "generate a new one, and save both the key AND secret."
        )
    return key, secret


def _headers(demo: bool = True) -> dict:
    import base64
    key, secret = _credentials(demo)
    token = base64.b64encode(f"{key}:{secret}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def fetch_all_instruments(base_url: str, demo: bool = True) -> list[dict]:
    """Paginate through GET /equity/metadata/instruments and return all entries."""
    import requests

    instruments: list[dict] = []
    cursor: str | None = None
    page = 0

    while True:
        params: dict = {}
        if cursor:
            params["cursor"] = cursor
        url = f"{base_url}/equity/metadata/instruments"
        resp = requests.get(url, headers=_headers(demo), params=params, timeout=30)
        resp.raise_for_status()
        batch: list[dict] = resp.json()
        if not batch:
            break
        instruments.extend(batch)
        page += 1
        log.info("  page %d: %d instruments (total so far: %d)", page, len(batch), len(instruments))
        # Cursor is the ticker of the last item in the batch.
        cursor = batch[-1].get("ticker")
        if len(batch) < 50:
            # Last page.
            break
        time.sleep(0.2)  # be polite to the T212 API

    log.info("Fetched %d instruments total from T212.", len(instruments))
    return instruments


# ---------------------------------------------------------------------------
# Watchlist helpers
# ---------------------------------------------------------------------------

def _all_watchlist_tickers() -> list[str]:
    """Return every unique ticker referenced in any watchlist group."""
    wl = CONFIG.watchlists
    seen: set[str] = set()
    tickers: list[str] = []
    # Top-level lists (stocks_us, etfs_ucits, etc.)
    for key, val in wl.items():
        if key == "venue":
            continue
        if isinstance(val, list):
            for t in val:
                if isinstance(t, str) and t not in seen:
                    seen.add(t)
                    tickers.append(t)
    return tickers


# ---------------------------------------------------------------------------
# ISIN fetching (yfinance, cached)
# ---------------------------------------------------------------------------

def _load_isin_cache() -> dict[str, str | None]:
    if ISIN_CACHE.exists():
        return json.loads(ISIN_CACHE.read_text(encoding="utf-8"))
    return {}


def _save_isin_cache(cache: dict) -> None:
    ISIN_CACHE.write_text(json.dumps(cache, indent=2), encoding="utf-8")


def get_isin(ticker: str, cache: dict) -> str | None:
    """Return the ISIN for *ticker* via yfinance, using/updating the file cache."""
    if ticker in cache:
        return cache[ticker]
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).get_info()
        isin = info.get("isin") or None
        log.debug("  yfinance ISIN for %s: %s", ticker, isin)
    except Exception as exc:
        log.warning("  yfinance ISIN lookup failed for %s: %s", ticker, exc)
        isin = None
    cache[ticker] = isin
    return isin


# ---------------------------------------------------------------------------
# Matching logic
# ---------------------------------------------------------------------------

def _bare_symbol(yf_ticker: str) -> str:
    """Strip exchange suffixes: 'SXR8.DE' → 'SXR8', 'AAPL' → 'AAPL'."""
    return yf_ticker.split(".")[0].upper()


def build_lookups(
    instruments: list[dict],
) -> tuple[dict[str, dict], dict[str, list[dict]]]:
    """Return (by_isin, by_symbol) lookup dicts."""
    by_isin: dict[str, dict] = {}
    by_symbol: dict[str, list[dict]] = {}

    for inst in instruments:
        isin = inst.get("isin", "")
        if isin:
            by_isin[isin] = inst

        # T212 ticker format: AAPL_US_EQ → bare symbol is "AAPL"
        t212_ticker: str = inst.get("ticker", "")
        bare = t212_ticker.split("_")[0].upper()
        by_symbol.setdefault(bare, []).append(inst)

    return by_isin, by_symbol


def match_ticker(
    yf_ticker: str,
    by_isin: dict,
    by_symbol: dict,
    isin_cache: dict,
) -> dict | None:
    """Try to find the T212 instrument for the given yfinance ticker.

    Strategy:
      1. ISIN match  — fetched from yfinance (most reliable for EU tickers).
      2. Symbol match — strip the exchange suffix and match T212's bare symbol.
         When multiple T212 instruments share the same bare symbol we prefer
         the one whose currency matches the expected venue currency.
    """
    # 1. ISIN match
    isin = get_isin(yf_ticker, isin_cache)
    if isin and isin in by_isin:
        return by_isin[isin]

    # 2. Symbol match
    bare = _bare_symbol(yf_ticker)
    candidates = by_symbol.get(bare, [])
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    # Multiple candidates — pick by expected currency from venue map.
    from analysis.market_data import _venue_currency
    expected_ccy = _venue_currency(yf_ticker)
    for c in candidates:
        if c.get("currencyCode", "").upper() == expected_ccy.upper():
            return c

    # Fall back to the first candidate.
    log.warning(
        "  %s: multiple T212 matches for symbol %r — picking first (%s)",
        yf_ticker, bare, candidates[0].get("ticker"),
    )
    return candidates[0]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(demo: bool = True) -> None:
    base_url = DEMO_URL if demo else LIVE_URL
    env = "DEMO" if demo else "LIVE"
    log.info("Fetching T212 instrument universe from %s (%s) ...", base_url, env)

    instruments = fetch_all_instruments(base_url, demo=demo)
    by_isin, by_symbol = build_lookups(instruments)

    tickers = _all_watchlist_tickers()
    log.info("Matching %d watchlist tickers ...", len(tickers))

    isin_cache = _load_isin_cache()
    out: dict[str, dict] = {}
    unmatched: list[str] = []

    for yf_ticker in tickers:
        inst = match_ticker(yf_ticker, by_isin, by_symbol, isin_cache)
        if inst is None:
            log.warning("  %-20s  → NOT FOUND in T212 universe", yf_ticker)
            unmatched.append(yf_ticker)
        else:
            entry = {
                "t212_ticker": inst["ticker"],
                "isin": inst.get("isin", ""),
                "name": inst.get("shortName") or inst.get("name", ""),
                "currency": inst.get("currencyCode", "EUR"),
                "type": inst.get("type", "STOCK"),
            }
            out[yf_ticker] = entry
            log.info("  %-20s  → %s  (%s)", yf_ticker, inst["ticker"], inst.get("currencyCode", ""))

    # Save ISIN cache (avoids re-fetching on next run).
    _save_isin_cache(isin_cache)

    # Apply manual overrides.
    if OVERRIDE_FILE.exists():
        overrides: dict = json.loads(OVERRIDE_FILE.read_text(encoding="utf-8"))
        for yf_ticker, entry in overrides.items():
            if yf_ticker in unmatched:
                unmatched.remove(yf_ticker)
            out[yf_ticker] = entry
            log.info("  %-20s  → %s  (MANUAL OVERRIDE)", yf_ticker, entry.get("t212_ticker", "?"))

    # Write output.
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    INSTRUMENTS_OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Wrote %d entries to %s", len(out), INSTRUMENTS_OUT)

    if unmatched:
        log.warning(
            "\n%d ticker(s) NOT matched — add them to %s:\n  %s\n"
            "Or check if they are available in T212's instrument list.",
            len(unmatched),
            OVERRIDE_FILE,
            "\n  ".join(unmatched),
        )
        sys.exit(1)
    else:
        log.info("All %d tickers matched successfully.", len(tickers))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build data/t212_instruments.json")
    parser.add_argument(
        "--live", action="store_true",
        help="Fetch from the live T212 environment (default: demo)",
    )
    args = parser.parse_args()
    main(demo=not args.live)
