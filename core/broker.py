"""Broker abstraction.

- `BrokerInterface`  : protocol all brokers implement.
- `MockBroker`       : fills at `ref_price_eur` + configured slippage, computes
                       fee from `settings.yaml` fee table by venue.
- `IBKRBroker`       : paper trading via `ib_async`, contracts from
                       `data/contracts.json`, fills + commission converted to EUR.
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


# --- IBKRBroker --------------------------------------------------------------


class IBKRBroker:
    """ib_async-backed broker for paper trading.

    Responsibilities:
      * Connect to IB Gateway/TWS, refuse any non-paper account.
      * Resolve Contract from ``data/contracts.json`` (populated by
        ``scripts/resolve_contracts.py``). If a ticker isn't cached, fail
        loud rather than guessing.
      * Submit MarketOrder, wait for fill, grab the actual commission from
        the CommissionReport event, convert both price and commission to
        EUR at the fill-timestamp FX rate.

    One IBKRBroker instance is used for ALL bots per ``run_once`` call;
    clientId is taken from ``IBKR_CLIENT_ID_BASE`` plus an offset we bump
    per session so repeated runs during the same Gateway session don't
    collide.
    """

    _next_client_offset: int = 0

    def __init__(self, port: int | None = None, client_id: int | None = None, timeout: float | None = None) -> None:
        self._ib = None
        if timeout is None:
            timeout = float(os.environ.get("IBKR_ORDER_TIMEOUT_SEC", "120"))
        self._timeout = timeout
        self._port = port  # overrides IBKR_PORT env var when set
        self._client_id = client_id
        self._contracts_cache: dict | None = None
        self._account: str | None = None

    # -- setup / teardown --

    def connect(self) -> None:
        from ib_async import IB

        host = os.environ.get("IBKR_HOST", "127.0.0.1")
        port = self._port if self._port is not None else int(os.environ.get("IBKR_PORT", "4002"))
        base = int(os.environ.get("IBKR_CLIENT_ID_BASE", "100"))

        if self._client_id is None:
            IBKRBroker._next_client_offset += 1
            self._client_id = base + IBKRBroker._next_client_offset

        self._ib = IB()
        self._ib.connect(host, port, clientId=self._client_id, timeout=self._timeout)

        accounts = self._ib.managedAccounts()
        if not accounts:
            self._ib.disconnect()
            raise RuntimeError("IBKRBroker: connected but no managed accounts returned.")
        self._account = accounts[0]
        # Known IBKR paper-account prefixes. DU = US/global paper, DF = advisor
        # demo, DW = some EU-domiciled paper accounts.
        _PAPER_PREFIXES = ("DU", "DF", "DW")
        is_known_paper = self._account.startswith(_PAPER_PREFIXES)
        require_paper = os.environ.get("IBKR_REQUIRE_PAPER", "1") == "1"
        if not is_known_paper:
            msg = (
                f"IBKRBroker: account {self._account!r} does not match known paper "
                f"prefixes {_PAPER_PREFIXES}. If this IS a paper account with a "
                "non-standard prefix (common for EU IBKR accounts), set "
                "IBKR_REQUIRE_PAPER=0 in .env to bypass this check. "
                "NEVER set this on a live account."
            )
            if require_paper:
                self._ib.disconnect()
                raise RuntimeError(msg)
            log.warning(msg)
        log.info("IBKRBroker connected: account=%s clientId=%d",
                 self._account, self._client_id)

    def disconnect(self) -> None:
        if self._ib is not None and self._ib.isConnected():
            self._ib.disconnect()
        self._ib = None

    # -- contract resolution --

    def _load_contracts(self) -> dict:
        if self._contracts_cache is None:
            import json
            from core.config import DATA_DIR

            path = DATA_DIR / "contracts.json"
            if not path.exists():
                raise RuntimeError(
                    f"IBKRBroker: {path} missing. Run "
                    f"`python scripts/resolve_contracts.py` first."
                )
            self._contracts_cache = json.loads(path.read_text(encoding="utf-8"))
        return self._contracts_cache

    def _contract_for(self, yf_ticker: str):
        from ib_async import Stock

        cache = self._load_contracts()
        entry = cache.get(yf_ticker)
        if entry is None:
            raise RuntimeError(
                f"IBKRBroker: no contract for {yf_ticker!r} in contracts.json. "
                f"Re-run scripts/resolve_contracts.py."
            )
        c = Stock(
            symbol=entry["symbol"],
            exchange=entry["exchange"],
            currency=entry["currency"],
        )
        c.conId = entry["con_id"]
        if entry.get("primary_exchange"):
            c.primaryExchange = entry["primary_exchange"]
        return c, entry

    # -- orders --

    def place_market_order(self, order: Order) -> Fill:
        from ib_async import MarketOrder

        if self._ib is None or not self._ib.isConnected():
            raise RuntimeError("IBKRBroker: not connected. Call connect() first.")

        contract, entry = self._contract_for(order.ticker)
        # Use SMART routing so IBKR picks the best venue.
        # Direct-exchange routing (e.g. SBF, IBIS) triggers Precautionary
        # Settings error 10311.  conId already uniquely identifies the instrument
        # so SMART routing resolves correctly.
        contract.exchange = "SMART"
        action = "BUY" if order.side is Side.BUY else "SELL"
        qty = abs(order.qty)

        # IBKR API rejects fractional quantities (error 10243) for most
        # instruments. Floor to whole shares; if result is 0, skip the order.
        import math
        qty = math.floor(qty)
        if qty == 0:
            log.warning(
                "IBKRBroker: %s %s qty rounded down to 0 — skipping "
                "(increase capital or check per_position_pct)",
                action, order.ticker,
            )
            # Return a zero fill so the caller doesn't crash.
            from datetime import datetime, timezone
            from core.types import Fill, Side as _Side
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

        ib_order = MarketOrder(action=action, totalQuantity=qty)
        ib_order.tif = "DAY"
        ib_order.outsideRth = False
        # Required when the Gateway manages multiple sub-accounts (IBKR error 435).
        if self._account:
            ib_order.account = self._account

        log.info(
            "IBKRBroker: placing %s %.0f %s @MKT (%s/%s %s)",
            action, qty, order.ticker, entry["symbol"], entry["exchange"],
            entry["currency"],
        )
        trade = self._ib.placeOrder(contract, ib_order)

        # ── Phase 1: brief wait to let IBKR classify the order ────────────────
        # PreSubmitted  → order accepted by IB but exchange not open yet
        #                 (typical for US stocks placed before 15:30 CEST).
        #                 We return a *pending* fill immediately so the virtual
        #                 book reserves the capital; the order stays live at
        #                 IBKR and will fill when the exchange opens.
        # Submitted     → order is live at the exchange; wait for fill normally.
        PRESUBMIT_WAIT_SEC = 5
        initial_wait = 0.0
        while initial_wait < PRESUBMIT_WAIT_SEC and not trade.isDone():
            self._ib.waitOnUpdate(timeout=1.0)
            initial_wait += 1.0
            if trade.orderStatus.status in ("Submitted", "Filled"):
                break  # it's live — proceed to normal fill wait

        if trade.orderStatus.status == "PreSubmitted" and not trade.isDone():
            log.info(
                "IBKRBroker: %s order PreSubmitted (market not open yet) — "
                "recording as pending; will fill when exchange opens",
                order.ticker,
            )
            return self._build_pending_fill(order, trade, entry)

        # ── Phase 2: wait for the confirmed fill ───────────────────────────────
        deadline = self._timeout - initial_wait
        while not trade.isDone():
            self._ib.waitOnUpdate(timeout=1.0)
            deadline -= 1.0
            if deadline <= 0:
                current_status = trade.orderStatus.status
                # "Submitted" means the order is live at IBKR but hasn't filled yet.
                # This happens when the exchange is closed (e.g. EU orders at 08:30
                # before 09:00 open, or US orders before 15:30 CEST).  Don't cancel —
                # leave the order alive at IBKR and record a pending fill so the
                # virtual book reserves the capital.  The reconciliation agent will
                # update the actual fill price once the exchange opens.
                if current_status == "Submitted":
                    log.info(
                        "IBKRBroker: %s still Submitted after %.0fs — market likely "
                        "closed, recording as pending fill (order stays live at IBKR)",
                        order.ticker, self._timeout,
                    )
                    return self._build_pending_fill(order, trade, entry)
                log.warning(
                    "IBKRBroker: %s order still %s after %.0fs — canceling at broker",
                    order.ticker, current_status, self._timeout,
                )
                self._ib.cancelOrder(ib_order)
                for _ in range(20):
                    self._ib.waitOnUpdate(timeout=0.5)
                    if trade.isDone():
                        break
                # The order may have filled naturally during the cancel window
                # (cancel is a no-op if IBKR already processed all fills).
                if trade.orderStatus.status == "Filled":
                    log.info(
                        "IBKRBroker: %s filled during cancel window — recording fill",
                        order.ticker,
                    )
                    break  # exit the while-not-done loop; _build_fill below
                raise RuntimeError(
                    f"IBKRBroker: order for {order.ticker} did not fill within "
                    f"{self._timeout}s (last status={trade.orderStatus.status!r}). "
                    "MKT on Xetra listings needs the exchange open (Mon–Fri RTH)."
                )

        status = trade.orderStatus.status
        if status != "Filled":
            # "Cancelled" with 0 fills and 0 avg price means IBKR rejected the order
            # before any exchange activity — most common cause is that the paper
            # simulation cancelled a "Submitted" US-stock order because the exchange
            # simulation is unavailable outside US RTH.  Treat as pending fill so
            # the virtual book is consistent; the reconciliation agent corrects it.
            if (
                status in ("Cancelled", "ApiCancelled")
                and trade.orderStatus.filled == 0
                and trade.orderStatus.avgFillPrice == 0.0
            ):
                log.info(
                    "IBKRBroker: %s cancelled by IBKR with 0 fills (exchange likely "
                    "closed) — recording as pending fill",
                    order.ticker,
                )
                return self._build_pending_fill(order, trade, entry)
            raise RuntimeError(
                f"IBKRBroker: {order.ticker} ended with status={status!r} "
                f"(avgFillPrice={trade.orderStatus.avgFillPrice})"
            )

        return self._build_fill(order, trade, entry)

    # -- fill aggregation --

    def _build_pending_fill(self, order: Order, trade, contract_entry: dict) -> Fill:
        """Return an *estimated* Fill for a PreSubmitted order.

        The order is live at IBKR and will fill when the exchange opens.
        We use ``order.ref_price_eur`` as the price estimate and the planned
        (floored) qty so the virtual book reserves the capital immediately.
        The reconciliation agent will correct these values once the actual
        fill arrives.

        We store the IBKR ``permId`` (not the session-scoped orderId) in
        ``broker_order_id`` so reconciliation can look it up across sessions.
        """
        from core import fx

        ccy = contract_entry["currency"]
        fx_rate = fx.eur_per_unit(ccy)
        # estimated local-currency price
        est_local = order.ref_price_eur / fx_rate if fx_rate > 0 else order.ref_price_eur
        planned_qty = float(trade.order.totalQuantity)  # already floored
        perm_id = str(trade.order.permId) if trade.order.permId else None

        return Fill(
            ticker=order.ticker,
            side=order.side,
            qty=planned_qty,
            price=est_local,
            price_eur=order.ref_price_eur,
            fx_rate=fx_rate,
            fee_eur=estimate_fee_eur(order.ticker, planned_qty, order.ref_price_eur),
            timestamp=datetime.now(tz=timezone.utc),
            broker_order_id=perm_id,
            is_pending=True,
        )

    def _build_fill(self, order: Order, trade, contract_entry: dict) -> Fill:
        """Aggregate executions from `trade.fills` into a single Fill record.

        A single market order may be split into multiple partial fills;
        we take the qty-weighted average fill price and the sum of
        commissions (with their own FX conversion).
        """
        from core import fx

        if not trade.fills:
            raise RuntimeError(
                f"IBKRBroker: {order.ticker} status=Filled but trade.fills is empty"
            )

        total_qty = 0.0
        total_notional_local = 0.0
        commission_eur = 0.0
        last_ts = None
        order_id = None

        for f in trade.fills:
            q = float(f.execution.shares)
            p = float(f.execution.price)
            total_qty += q
            total_notional_local += q * p
            last_ts = f.time or last_ts
            order_id = order_id or str(f.execution.orderId)

            cr = getattr(f, "commissionReport", None)
            if cr is not None and cr.commission:
                commission_eur += fx.to_eur(
                    float(cr.commission),
                    cr.currency or contract_entry["currency"],
                )

        if total_qty <= 0:
            raise RuntimeError(f"IBKRBroker: zero total qty filled for {order.ticker}")

        avg_price_local = total_notional_local / total_qty
        ccy = contract_entry["currency"]
        fx_rate = fx.eur_per_unit(ccy)
        avg_price_eur = avg_price_local * fx_rate

        # If commission was missing from every fill (can happen on paper,
        # IBKR sometimes drops CommissionReport), fall back to our fee
        # estimate so the trade isn't recorded as "free".
        if commission_eur <= 0:
            commission_eur = estimate_fee_eur(order.ticker, total_qty, avg_price_eur)
            log.warning(
                "IBKRBroker: no commission report for %s; using estimate €%.2f",
                order.ticker, commission_eur,
            )

        from datetime import datetime, timezone
        ts = last_ts or datetime.now(tz=timezone.utc)

        return Fill(
            ticker=order.ticker,
            side=order.side,
            qty=total_qty,
            price=avg_price_local,
            price_eur=avg_price_eur,
            fx_rate=fx_rate,
            fee_eur=commission_eur,
            timestamp=ts,
            broker_order_id=order_id,
        )


def get_broker() -> BrokerInterface:
    """Factory driven by BROKER_BACKEND env var."""
    backend = CONFIG.broker_backend
    if backend == "mock":
        return MockBroker()
    if backend == "ibkr":
        return IBKRBroker()
    if backend == "t212":
        return Trading212Broker()
    raise ValueError(f"Unknown BROKER_BACKEND={backend!r}; expected 'mock', 'ibkr', or 't212'.")


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
        consistency with IBKR behavior.
    """

    _ORDER_POLL_INTERVAL_SEC = 2.0
    _ORDER_POLL_TIMEOUT_SEC = 120.0

    def __init__(self, demo: bool | None = None) -> None:
        if demo is None:
            demo = os.environ.get("T212_DEMO", "1") == "1"
        self._demo = demo
        self._instruments_cache: dict[str, dict] | None = None  # yf_ticker → T212 entry

    @property
    def _base_url(self) -> str:
        return (
            "https://demo.trading212.com/api/v0"
            if self._demo
            else "https://live.trading212.com/api/v0"
        )

    def _credentials(self) -> tuple[str, str]:
        """Return (api_key, api_secret) for the current environment."""
        suffix = "PAPER" if self._demo else "LIVE"
        key = (
            os.environ.get(f"T212_API_KEY_{suffix}", "").strip()
            or os.environ.get("T212_API_KEY", "").strip()
        )
        secret = (
            os.environ.get(f"T212_API_SECRET_{suffix}", "").strip()
            or os.environ.get("T212_API_SECRET", "").strip()
        )
        if not key:
            raise RuntimeError(
                f"T212_API_KEY_{suffix} not set in .env -- cannot connect to Trading 212. "
                "Generate a key from T212 -> Settings -> API (Beta) and save both the key AND secret."
            )
        if not secret:
            raise RuntimeError(
                f"T212_API_SECRET_{suffix} not set in .env -- the secret is shown only once "
                "at key creation time. Delete the old key, generate a new one, and save both values."
            )
        return key, secret

    def _headers(self) -> dict[str, str]:
        """Build HTTP Basic Auth header: base64(api_key:api_secret)."""
        import base64
        key, secret = self._credentials()
        token = base64.b64encode(f"{key}:{secret}".encode()).decode()
        return {
            "Authorization": f"Basic {token}",
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

        # Floor to whole shares for IBKR-compatible behavior.
        # T212 supports fractional shares, but strategies size in whole units.
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
        order_data = self._post("/equity/orders/market", payload)
        order_id = order_data["id"]

        # Poll until filled — market orders on T212 usually fill in <5 s during
        # market hours.  Outside hours (pre-market / post-market) the order sits
        # as 'NEW' until the exchange opens.  We treat 'NEW' at timeout the same
        # way as IBKR's 'Submitted': record a pending fill at the reference price
        # rather than raising — the order stays live at T212 and will fill when
        # the market opens.
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
        but the market is closed.  Mirrors IBKRBroker._build_pending_fill."""
        from datetime import datetime, timezone
        return Fill(
            ticker=order.ticker,
            side=order.side,
            qty=order.qty,
            price=order.ref_price_eur,
            price_eur=order.ref_price_eur,
            fx_rate=1.0,
            fee_eur=0.0,
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
