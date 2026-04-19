"""Shared value objects. Kept Pydantic-free for speed and simplicity."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class AssetClass(str, Enum):
    STOCK = "stock"
    ETF = "etf"
    CRYPTO = "crypto"


@dataclass(frozen=True)
class Order:
    """A strategy's proposal, before risk checks or execution."""

    bot_id: int
    ticker: str
    side: Side
    qty: float
    signal_reason: str
    ref_price_eur: float                # EUR price used for sizing / fee math
    expected_profit_eur: float = 0.0    # used by the fee-aware guardrail; 0 = unknown
    asset_class: AssetClass = AssetClass.STOCK


@dataclass(frozen=True)
class Fill:
    """What the broker actually did."""

    ticker: str
    side: Side
    qty: float
    price: float            # local-currency fill price
    price_eur: float        # converted to EUR
    fx_rate: float
    fee_eur: float
    timestamp: datetime
    broker_order_id: str | None = None


@dataclass
class PortfolioSnapshot:
    """Read-only view of a bot's virtual book at a moment in time."""

    bot_id: int
    cash_eur: float
    positions: dict[str, "PositionView"] = field(default_factory=dict)

    @property
    def positions_value_eur(self) -> float:
        return sum(p.market_value_eur for p in self.positions.values())

    @property
    def total_eur(self) -> float:
        return self.cash_eur + self.positions_value_eur


@dataclass
class PositionView:
    ticker: str
    qty: float
    avg_entry_eur: float
    last_price_eur: float
    entry_date: date = field(default_factory=date.today)

    @property
    def market_value_eur(self) -> float:
        return self.qty * self.last_price_eur
