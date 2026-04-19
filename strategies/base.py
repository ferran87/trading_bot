"""Strategy base class.

A strategy is a pure function of (portfolio, market data, config) -> list[Order].

No I/O, no DB access, no broker calls. The executor drives everything else.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date
from typing import Any

from analysis.market_data import Bars
from core.types import Order, PortfolioSnapshot


@dataclass
class StrategyContext:
    bot_id: int
    today: date
    bars: dict[str, Bars]
    params: dict[str, Any]          # this strategy's block from strategies.yaml
    force_rebalance: bool = False   # skip Monday-only gate (weekend / manual run)


class Strategy(ABC):
    name: str = "base"

    @abstractmethod
    def propose_orders(
        self,
        snapshot: PortfolioSnapshot,
        ctx: StrategyContext,
    ) -> list[Order]:
        """Return a list of proposed orders for this run.

        The list may be empty (no action). The executor applies risk checks
        and handles rejections; strategies should not defensively skip
        orders on cap / floor grounds — that's risk.py's job.
        """
        ...
