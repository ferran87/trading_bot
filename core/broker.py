"""Broker abstraction.

- `BrokerInterface`     : protocol all brokers implement.
- `MockBroker`          : fills at `ref_price_eur` + configured slippage; fee from
                          `settings.yaml` fee table by venue.
- `Trading212Broker`    : REST API trading via Trading 212; instrument map from
                          `data/t212_instruments.json`; fills polled for 120 seconds.

Two broker models — key differences at a glance
-----------------------------------------------
Property              MockBroker          Trading212Broker
--------------------  ------------------  --------------------------
Ticker input          yfinance as-is      yfinance → t212_instruments.json
Fill timing           Immediate           120-second poll; pending on NEW
                                          status at timeout
Pending fills?        Never               Yes
Qty rounding          Allows floats       Floors to whole shares
Fill price currency   Input currency      Local ccy → EUR at fill
Fees                  settings.yaml table T212 taxes array (may be empty
                                          in demo; falls back to 0.15% FX)
Required data file    None                data/t212_instruments.json

Pending fills (T212)
--------------------
`broker.place_market_order()` returns `Fill(is_pending=True)` when the order has not
yet been confirmed at timeout. `executor.run_orders()` logs these but does NOT call
`Portfolio.apply_fill()` — the virtual book is left unchanged. On the next bot run,
`_resolve_t212_pending_orders()` in `core/runner.py` polls for completion and
applies the fill retroactively. MockBroker never returns a pending fill.

EU orders on T212 commonly return HTTP 404 for the first ~10 polls right after
creation — this is normal T212 API behavior, not an error.
"""
from __future__ import annotations

import logging
import os
import random
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Protocol

from core.config import CONFIG
from core.types import Fill, Order, Side

log = logging.getLogger(__name__)


def venue_for(ticker: str) -> str:
    """Resolve a ticker's venue tag from watchlists.yaml.

    Defaults to xetra_eur if the ticker is unknown — caller should have
    already validated but we prefer a sensible default over a crash.
    """
    venue_map: dict = CONFIG.watchlists.get("venue", {})
    entry = venue_map.get(ticker)
    if entry is None:
        return "xetra_eur"
    return entry.get("venue", "xetra_eur")


def estimate_fee_eur(ticker: str, qty: float, price_eur: float, backend: str | None = None) -> float:
    """Estimate the *one-way* fee in EUR for the given ticker/size.

    Used by MockBroker (to stamp the fill) and risk.py (for the fee-aware
    skip, where it's multiplied by 2 for a round trip).

    ``backend`` overrides the BROKER_BACKEND env-var lookup — pass it
    explicitly in tests or when the env var isn't set up.  Defaults to
    ``CONFIG.broker_backend``.

    T212 fee model: zero commission; 0.15% FX conversion on non-EUR venues.
    """
    if backend is None:
        backend = CONFIG.broker_backend
    venue = venue_for(ticker)
    fee_table_key = "fees_t212" if backend == "t212" else "fees"
    # Fall back to the standard table if the t212 section is missing (e.g. old config).
    fees_section = CONFIG.settings.get(fee_table_key) or CONFIG.settings["fees"]
    # Fall back venue-by-venue too (e.g. new venue added to fees but not fees_t212 yet).
    fees = fees_section.get(venue) or CONFIG.settings["fees"].get(venue, {})
    if not fees:
        log.warning("estimate_fee_eur: no fee entry for venue=%s backend=%s; using 0", venue, backend)
        return 0.0
    notional = qty * price_eur
    raw = fees["per_trade_eur"] + notional * fees["pct"]
    return max(raw, fees["min_eur"])


class BrokerInterface(Protocol):
    """All brokers expose this shape; strategies talk to `executor`, not to
    the broker directly, so this surface can stay tiny."""

    def connect(self) -> None: ...
    def disconnect(self) -> None: ...
    def place_market_order(self, order: Order) -> Fill: ...


# --- MockBroker --------------------------------------------------------------


@dataclass
class MockBroker:
    """Deterministic-ish mock: fills at ref_price_eur * (1 + slippage_bps/10_000).

    Slippage sign is randomized uniformly in [-slippage_bps, +slippage_bps]
    unless `seed` is None (then 0 slippage is used for reproducibility).

    Set `sim_date` to a date before running a backtest day so fill timestamps
    reflect the simulated date rather than the real current time.
    """

    seed: int | None = 42
    sim_date: "date | None" = None  # set by backtest engine per simulated day

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed) if self.seed is not None else None

    def connect(self) -> None:
        return None

    def disconnect(self) -> None:
        return None

    def place_market_order(self, order: Order) -> Fill:
        cfg = CONFIG.settings["broker"]["mock"]
        slippage_bps_max = float(cfg["slippage_bps"])
        if self._rng is not None and slippage_bps_max > 0:
            bps = self._rng.uniform(-slippage_bps_max, slippage_bps_max)
        else:
            bps = 0.0

        direction = 1 if order.side is Side.BUY else -1
        fill_price_eur = order.ref_price_eur * (1 + direction * bps / 10_000.0)
        fee = estimate_fee_eur(order.ticker, order.qty, fill_price_eur)

        if self.sim_date is not None:
            ts = datetime(
                self.sim_date.year, self.sim_date.month, self.sim_date.day,
                16, 0, 0, tzinfo=timezone.utc,
            )
        else:
            ts = datetime.now(tz=timezone.utc)

        return Fill(
            ticker=order.ticker,
            side=order.side,
            qty=order.qty,
            price=fill_price_eur,      # MockBroker ignores FX and prices in EUR
            price_eur=fill_price_eur,
            fx_rate=1.0,
            fee_eur=fee,
            timestamp=ts,
            broker_order_id=None,
        )



def get_broker() -> BrokerInterface:
    """Factory driven by BROKER_BACKEND env var."""
    backend = CONFIG.broker_backend
    if backend == "mock":
        return MockBroker()
    if backend == "t212":
        return Trading212Broker()
    raise ValueError(f"Unknown BROKER_BACKEND={backend!r}; expected 'mock' or 't212'.")


# --- Trading212Broker --------------------------------------------------------


class Trading212Broker:
    """Trading 212 REST API broker.

    Authentication: long-lived API key via ``Authorization`` header.
    No local process needed — pure stateless HTTP. Works with both the
    T212 demo environment (paper trading) and the live environment.

    Requires:
      - ``T212_API_KEY`` env var (generate from T212 → Settings → API)
      - ``T212_DEMO`` env var: "1" (default) for demo, "0" for live
      - ``data/t212_instruments.json`` (built by
        ``scripts/resolve_t212_instruments.py``)

    Key constraints to be aware of:
      - Order endpoints are NOT idempotent in the beta API: a retried POST
        = a duplicate order. Never auto-retry POSTs; poll GET first.
      - T212 uses negative quantity to indicate a SELL.
      - FX conversion fee: 0.15% on every trade in a non-account currency
        (e.g. USD stock in an EUR account).
      - Fractional shares are supported; we floor to whole shares for
        consistency with the rest of the system (positions, fills, P&L).
    """

    _ORDER_POLL_INTERVAL_SEC = 2.0
    _ORDER_POLL_TIMEOUT_SEC = 120.0

    def __init__(self, demo: bool | None = None, owner: str | None = None) -> None:
        if demo is None:
            demo = os.environ.get("T212_DEMO", "1") == "1"
        self._demo = demo
        # ``owner`` selects which set of T212 credentials to use.  When set,
        # ``_credentials()`` looks up ``T212_API_KEY_{PAPER|LIVE}_{OWNER.upper()}``
        # first and falls back to the unsuffixed key for backward compat with
        # the original single-account setup (Ferran's keys, pre-Antonio).
        self._owner = (owner or "").strip() or None
        self._instruments_cache: dict[str, dict] | None = None  # yf_ticker → T212 entry

    @property
    def _base_url(self) -> str:
        from core.t212_auth import t212_base_url
        return t212_base_url(self._demo)

    def _credentials(self) -> tuple[str, str]:
        """Return ``(api_key, api_secret)`` for this broker's env + owner.

        Delegates to :func:`core.t212_auth.resolve_t212_credentials` — see
        that module for the lookup order and fallback-warning behaviour.
        """
        from core.t212_auth import resolve_t212_credentials
        return resolve_t212_credentials(self._demo, self._owner)

    def _headers(self) -> dict[str, str]:
        """Build HTTP Basic Auth header: base64(api_key:api_secret).

        Uses :func:`resolve_t212_credentials` directly (not :func:`t212_headers`)
        because the broker needs the original RuntimeError with the actionable
        env-var name to bubble up — the dashboard variant swallows it.
        """
        from core.t212_auth import resolve_t212_credentials, t212_basic_auth_header
        key, secret = resolve_t212_credentials(self._demo, self._owner)
        return {
            "Authorization": t212_basic_auth_header(key, secret),
            "Content-Type": "application/json",
        }

    def _get(self, path: str, params: dict | None = None) -> dict | list:
        import requests as _requests
        url = self._base_url + path
        resp = _requests.get(url, headers=self._headers(), params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, payload: dict) -> dict:
        import requests as _requests
        url = self._base_url + path
        resp = _requests.post(url, json=payload, headers=self._headers(), timeout=30)
        resp.raise_for_status()
        return resp.json()

    # -- connection lifecycle --

    def _fetch_account(self) -> dict:
        """Fetch account summary and return {cash_eur, total_eur, currency}.

        Used by connect() for logging and by _sync_t212_initial_capital() to
        distribute the account balance across bots before each run.
        """
        info = self._get("/equity/account/summary")
        cash_block = info.get("cash", {})
        total_block = info.get("totalValue", info.get("total", {}))
        free = float(cash_block.get("availableToTrade", cash_block.get("free", 0)))
        total = float(
            total_block if isinstance(total_block, (int, float))
            else total_block.get("amount", free)
        )
        currency = info.get("currency", cash_block.get("currencyCode", "EUR"))
        return {"cash_eur": free, "total_eur": total, "currency": currency}

    def _fetch_total_deposited(self, since_date: date | None = None) -> float:
        """Return net EUR deposited into this T212 account (deposits − withdrawals).

        Args:
            since_date: If set, only count transactions whose date is on or after
                this date.  Used for live bots whose pre-existing manual portfolio
                deposits must not be included in the bot's budget.
                None = count all deposits (default for paper bots).

        Paginates through the full transaction history so additional deposits made
        after the bot started are automatically picked up.  This is the correct
        baseline for calculating investment returns — not the current account value
        which already includes unrealised P&L.

        Transaction types seen in the wild: DEPOSIT, WITHDRAWAL.
        Any other type (dividends, interest, etc.) is ignored so the figure
        reflects only capital the user explicitly put in.
        """
        deposited = 0.0
        path: str | None = "/history/transactions"
        while path:
            data = self._get(path, params={"limit": 50} if "?" not in path else None)
            items = data.get("items", data) if isinstance(data, dict) else data
            for tx in items:
                tx_type = tx.get("type", "").upper()
                if tx_type not in ("DEPOSIT", "WITHDRAWAL"):
                    continue
                # Date filtering: try common T212 date field names
                if since_date is not None:
                    raw_date = (
                        tx.get("dateModified")
                        or tx.get("date")
                        or tx.get("timestamp")
                        or tx.get("createdAt")
                    )
                    if raw_date:
                        try:
                            tx_date = date.fromisoformat(str(raw_date)[:10])
                            if tx_date < since_date:
                                continue  # deposit predates the bot — skip
                        except (ValueError, TypeError):
                            pass  # can't parse date — include conservatively
                amount = float(tx.get("amount", 0))
                if tx_type == "DEPOSIT":
                    deposited += amount
                else:
                    deposited -= amount
            next_path = data.get("nextPagePath") if isinstance(data, dict) else None
            path = next_path  # None → loop exits
        return deposited

    def connect(self) -> None:
        """Validate credentials by fetching account summary."""
        acct = self._fetch_account()
        log.info(
            "Trading212Broker connected (%s): free_cash=%.2f %s",
            "DEMO" if self._demo else "LIVE",
            acct["cash_eur"],
            acct["currency"],
        )

    def disconnect(self) -> None:
        """No-op: REST API is stateless; nothing to close."""
        pass

    # -- instrument resolution --

    def _load_instruments(self) -> dict[str, dict]:
        """Return {yfinance_ticker: T212_instrument_entry} from the cache file."""
        if self._instruments_cache is None:
            import json
            from core.config import DATA_DIR

            path = DATA_DIR / "t212_instruments.json"
            if not path.exists():
                raise RuntimeError(
                    f"Trading212Broker: {path} missing. "
                    "Run `python scripts/resolve_t212_instruments.py` first."
                )
            self._instruments_cache = json.loads(path.read_text(encoding="utf-8"))
        return self._instruments_cache

    def _resolve_ticker(self, yf_ticker: str) -> str:
        """Map a yfinance ticker to the T212 instrument ticker string."""
        entry = self._load_instruments().get(yf_ticker)
        if entry is None:
            raise RuntimeError(
                f"Trading212Broker: no T212 instrument for {yf_ticker!r}. "
                "Re-run scripts/resolve_t212_instruments.py or add a manual mapping."
            )
        return entry["t212_ticker"]

    def _instrument_currency(self, yf_ticker: str) -> str:
        entry = self._load_instruments().get(yf_ticker, {})
        return entry.get("currency", "EUR")

    # -- orders --

    def place_market_order(self, order: Order) -> Fill:
        """Place a T212 market order and wait for the fill.

        T212 market orders typically fill within seconds during market hours.
        The response is polled until status FILLED (or timeout / error).

        IMPORTANT: this POST must never be auto-retried — T212's beta order
        endpoints are not idempotent. A duplicate POST = a duplicate trade.
        """
        import math
        import time

        t212_ticker = self._resolve_ticker(order.ticker)
        ccy = self._instrument_currency(order.ticker)

        # Floor to whole shares — T212 supports fractional shares but our
        # strategies size in whole units.
        qty = math.floor(abs(order.qty))
        if qty == 0:
            log.warning(
                "Trading212Broker: %s %s qty rounded to 0 — skipping",
                order.side.value, order.ticker,
            )
            return Fill(
                ticker=order.ticker,
                side=order.side,
                qty=0.0,
                price=0.0,
                price_eur=0.0,
                fx_rate=1.0,
                fee_eur=0.0,
                timestamp=datetime.now(tz=timezone.utc),
                broker_order_id=None,
            )

        # T212 convention: positive qty = BUY, negative qty = SELL.
        signed_qty = float(qty) if order.side is Side.BUY else -float(qty)
        payload = {"quantity": signed_qty, "ticker": t212_ticker}

        log.info(
            "Trading212Broker: placing %s %.0f %s (T212: %s, %s)",
            order.side.value, qty, order.ticker, t212_ticker, ccy,
        )

        # IMPORTANT: do NOT wrap in a retry loop — see docstring.
        try:
            order_data = self._post("/equity/orders/market", payload)
        except Exception as post_exc:
            # T212 returns HTTP 400 when the exchange is completely closed (e.g.
            # overnight before the market opens).  Unlike the "NEW after 120s"
            # case (where T212 accepted the order), here no order was created at
            # T212, so there is no order_id to track.  We log the rejection and
            # return a zero-qty Fill so the executor treats the order as skipped
            # rather than raising — this lets other pending orders on the same
            # run still execute, and ensures RunLog is always written.
            import requests as _req_exc_mod
            is_400 = (
                isinstance(post_exc, _req_exc_mod.HTTPError)
                and post_exc.response is not None
                and post_exc.response.status_code == 400
            )
            if is_400:
                log.warning(
                    "Trading212Broker: POST /equity/orders/market returned 400 for "
                    "%s %s — exchange likely closed, skipping order",
                    order.side.value, order.ticker,
                )
                return Fill(
                    ticker=order.ticker,
                    side=order.side,
                    qty=0.0,
                    price=0.0,
                    price_eur=0.0,
                    fx_rate=1.0,
                    fee_eur=0.0,
                    timestamp=datetime.now(tz=timezone.utc),
                    broker_order_id=None,
                )
            raise  # non-400 errors propagate as before
        order_id = order_data["id"]

        # Poll until filled — market orders on T212 usually fill in <5 s during
        # market hours.  Outside hours (pre-market / post-market) the order sits
        # as 'NEW' until the exchange opens.  We treat 'NEW' at timeout the same
        # way as a pending fill: record at the reference price rather than
        # raising — the order stays live at T212 and will fill when the
        # market opens.
        deadline = time.monotonic() + self._ORDER_POLL_TIMEOUT_SEC
        while order_data.get("status") not in ("FILLED", "CANCELLED", "REJECTED"):
            if time.monotonic() >= deadline:
                last_status = order_data.get("status")
                if last_status == "NEW":
                    log.info(
                        "Trading212Broker: order %s for %s still NEW after %.0fs "
                        "— market likely closed, recording as pending fill "
                        "(order stays live at T212)",
                        order_id, order.ticker, self._ORDER_POLL_TIMEOUT_SEC,
                    )
                    return self._build_pending_fill(order, str(order_id))
                raise RuntimeError(
                    f"Trading212Broker: order {order_id} for {order.ticker} "
                    f"did not fill within {self._ORDER_POLL_TIMEOUT_SEC:.0f}s "
                    f"(last status={last_status!r})"
                )
            time.sleep(self._ORDER_POLL_INTERVAL_SEC)
            try:
                order_data = self._get(f"/equity/orders/{order_id}")
            except Exception as exc:
                log.warning(
                    "Trading212Broker: GET /equity/orders/%s failed: %s — retrying",
                    order_id, exc,
                )

        status = order_data.get("status")
        if status != "FILLED":
            raise RuntimeError(
                f"Trading212Broker: order {order_id} for {order.ticker} "
                f"ended with status={status!r}"
            )

        return self._build_fill(order, order_data, ccy, str(order_id))

    def _build_pending_fill(self, order: Order, order_id: str) -> Fill:
        """Return a pending Fill using the reference price when the order is live
        but the market is closed.

        IMPORTANT: use the floored integer qty (same as what was sent to T212),
        not order.qty (the strategy's fractional target).  T212 only executes
        whole shares; recording a fractional qty here would cause the virtual
        book to diverge from the actual T212 position.
        """
        import math
        from datetime import datetime, timezone
        floored_qty = float(math.floor(abs(order.qty)))
        return Fill(
            ticker=order.ticker,
            side=order.side,
            qty=floored_qty,
            price=order.ref_price_eur,
            price_eur=order.ref_price_eur,
            fx_rate=1.0,
            fee_eur=estimate_fee_eur(order.ticker, floored_qty, order.ref_price_eur),
            timestamp=datetime.now(timezone.utc),
            broker_order_id=order_id,
            is_pending=True,
        )

    def _build_fill(
        self,
        order: Order,
        order_data: dict,
        ccy: str,
        order_id: str,
    ) -> Fill:
        """Convert a T212 filled-order dict to our internal Fill type."""
        from core import fx

        filled_qty = abs(float(order_data.get("filledQuantity") or order_data.get("quantity", 0)))
        filled_price_local = float(order_data.get("filledPrice") or order.ref_price_eur)

        # Extract the FX conversion fee from the T212 taxes array.
        # T212 charges 0.15% on trades in a currency other than the account's
        # base currency (EUR).  In the demo environment the taxes array may be
        # absent or empty — fall back to the 0.15% estimate in that case.
        fee_eur = 0.0
        for tax in order_data.get("taxes", []):
            tax_val = float(tax.get("quantity") or tax.get("value") or 0)
            tax_ccy = tax.get("currencyCode", "EUR")
            if tax_val and tax_ccy:
                try:
                    fee_eur += fx.to_eur(tax_val, tax_ccy)
                except Exception:
                    fee_eur += tax_val  # best-effort if FX lookup fails

        if fee_eur <= 0 and ccy != "EUR":
            # Demo / early beta: taxes array absent.  Estimate the FX fee so
            # the virtual book and risk checks aren't silently free.
            notional_local = filled_qty * filled_price_local
            try:
                notional_eur = fx.to_eur(notional_local, ccy)
            except Exception:
                notional_eur = notional_local
            fee_eur = notional_eur * 0.0015
            log.debug(
                "Trading212Broker: no tax data for %s — estimated FX fee €%.4f",
                order.ticker, fee_eur,
            )

        # EUR conversion
        fx_rate = 1.0
        if ccy != "EUR":
            try:
                fx_rate = fx.eur_per_unit(ccy)
            except Exception:
                log.warning(
                    "Trading212Broker: FX lookup failed for %s — using rate=1.0", ccy
                )
        price_eur = filled_price_local * fx_rate

        return Fill(
            ticker=order.ticker,
            side=order.side,
            qty=filled_qty,
            price=filled_price_local,
            price_eur=price_eur,
            fx_rate=fx_rate,
            fee_eur=fee_eur,
            timestamp=datetime.now(tz=timezone.utc),
            broker_order_id=order_id,
        )
