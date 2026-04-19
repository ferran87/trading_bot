"""Glue: for each enabled bot, fetch data, build snapshot, run strategy,
execute orders, record equity snapshot.

Separated from main.py so it's importable from tests.
"""
from __future__ import annotations

import logging
import traceback
from datetime import date, datetime, timezone
from typing import Callable

from sqlalchemy.orm import Session

from analysis import market_data
from core import executor
from core.broker import BrokerInterface, get_broker
from core.config import CONFIG
from core.db import Bot, ErrorLog, get_session
from core.portfolio import Portfolio
from strategies.base import Strategy, StrategyContext
from strategies.etf_momentum import EtfMomentumStrategy
from strategies.mean_reversion import MeanReversionStrategy

log = logging.getLogger(__name__)


def validate_run_dates(today: date, as_of: date | None) -> None:
    """``as_of`` is the latest bar date to use; it cannot be after *today*."""
    if as_of is not None and as_of > today:
        raise ValueError(f"as_of={as_of} is after today={today}")


STRATEGY_REGISTRY: dict[str, Callable[[], Strategy]] = {
    "etf_momentum": EtfMomentumStrategy,
    "mean_reversion": MeanReversionStrategy,
    # wired in Phase 3:
    # "news_sentiment": NewsSentimentStrategy,
}


def _universe_tickers(strategy_name: str, params: dict) -> list[str]:
    """Resolve the strategy's `universe` key against watchlists.yaml."""
    watchlists = CONFIG.watchlists
    uni = params.get("universe")
    if isinstance(uni, str):
        return list(watchlists[uni])
    if isinstance(uni, list):
        result: list[str] = []
        for group in uni:
            result.extend(watchlists[group])
        return result
    raise ValueError(f"strategy {strategy_name!r}: bad universe spec {uni!r}")


def run_bot(
    session: Session,
    broker: BrokerInterface,
    bot: Bot,
    today: date,
    *,
    force_rebalance: bool = False,
    as_of: date | None = None,
) -> executor.ExecutionReport | None:
    """Run one cycle for one bot. Returns the report, or None if the bot
    is disabled / its strategy isn't wired yet."""
    if not bot.enabled:
        log.info("bot=%d (%s) disabled, skipping", bot.id, bot.name)
        return None
    strategy_cls = STRATEGY_REGISTRY.get(bot.strategy)
    if strategy_cls is None:
        log.info("bot=%d strategy=%s not wired yet, skipping", bot.id, bot.strategy)
        return None

    params = CONFIG.strategies["strategies"][bot.strategy]
    universe = _universe_tickers(bot.strategy, params)

    min_history = int(params.get("min_history_days") or params.get("lookback_days", 70))
    bars = market_data.prefetch_since(universe, min_history, as_of=as_of)
    if not bars:
        raise RuntimeError(f"bot={bot.id}: no market data for universe {universe!r}")

    last_prices = market_data.last_prices_eur(bars)
    snapshot = Portfolio.snapshot(session, bot.id, last_prices)

    ctx = StrategyContext(
        bot_id=bot.id,
        today=today,
        bars=bars,
        params=params,
        force_rebalance=force_rebalance,
    )
    strategy = strategy_cls()
    orders = strategy.propose_orders(snapshot, ctx)
    log.info("bot=%d proposed %d orders", bot.id, len(orders))

    report = executor.run_orders(session, broker, bot.id, orders, snapshot, today)

    # End-of-run equity snapshot (one per day — updates if called again same day).
    Portfolio.record_equity_snapshot(session, bot.id, today, last_prices)

    return report


def run_once(
    today: date | None = None,
    *,
    force_rebalance: bool = False,
    as_of: date | None = None,
) -> list[executor.ExecutionReport]:
    """Run one full cycle for every enabled bot.

    Any per-bot exception is logged to the `errors` table and does NOT abort
    other bots (fail-safe, per the project plan).
    """
    today = today or datetime.now(tz=timezone.utc).date()
    validate_run_dates(today, as_of)
    broker = get_broker()
    broker.connect()
    reports: list[executor.ExecutionReport] = []
    try:
        with get_session() as session:
            bots = session.query(Bot).order_by(Bot.id).all()
            for bot in bots:
                try:
                    r = run_bot(
                        session,
                        broker,
                        bot,
                        today,
                        force_rebalance=force_rebalance,
                        as_of=as_of,
                    )
                    if r is not None:
                        reports.append(r)
                        log.info("REPORT %s", r.summary_line())
                        session.commit()
                except Exception as e:
                    session.rollback()
                    log.exception("bot=%d run failed: %s", bot.id, e)
                    session.add(
                        ErrorLog(
                            bot_id=bot.id,
                            component="runner.run_bot",
                            message=str(e),
                            traceback=traceback.format_exc(),
                        )
                    )
                    session.commit()
    finally:
        broker.disconnect()
    return reports
