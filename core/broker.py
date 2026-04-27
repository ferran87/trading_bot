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


def estimate_fee_eur(ticker: str, qty: float, price_eur: float) -> float:
    """Estimate the *one-way* fee in EUR for the given ticker/size.

    Used by both the MockBroker (to stamp the fill) and risk.py (for the
    fee-aware skip, where it's multiplied by 2 for a round trip).
    """
    venue = venue_for(ticker)
    fees = CONFIG.settings["fees"][venue]
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

        log.info(
            "IBKRBroker: placing %s %.4f %s @MKT (%s/%s %s)",
            action, qty, order.ticker, entry["symbol"], entry["exchange"],
            entry["currency"],
        )
        trade = self._ib.placeOrder(contract, ib_order)

        # Block until the order reaches a terminal state.
        deadline = self._timeout
        while not trade.isDone():
            self._ib.waitOnUpdate(timeout=1.0)
            deadline -= 1.0
            if deadline <= 0:
                log.warning(
                    "IBKRBroker: %s order still %s after %.0fs — canceling at broker",
                    order.ticker, trade.orderStatus.status, self._timeout,
                )
                self._ib.cancelOrder(ib_order)
                for _ in range(20):
                    self._ib.waitOnUpdate(timeout=0.5)
                    if trade.isDone():
                        break
                last_status = trade.orderStatus.status
                hint = (
                    " Order was PreSubmitted (market not open yet — US stocks don't "
                    "open until 15:30 CEST). The cancel was sent; re-run after market open."
                    if last_status == "PreSubmitted"
                    else " MKT on Xetra/US listings usually needs the exchange open "
                    "(Mon–Fri RTH). Weekend runs will queue or time out."
                )
                raise RuntimeError(
                    f"IBKRBroker: order for {order.ticker} did not fill within "
                    f"{self._timeout}s (last status={last_status!r}).{hint}"
                )

        status = trade.orderStatus.status
        if status != "Filled":
            raise RuntimeError(
                f"IBKRBroker: {order.ticker} ended with status={status!r} "
                f"(avgFillPrice={trade.orderStatus.avgFillPrice})"
            )

        return self._build_fill(order, trade, entry)

    # -- fill aggregation --

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
    raise ValueError(f"Unknown BROKER_BACKEND={backend!r}; expected 'mock' or 'ibkr'.")
