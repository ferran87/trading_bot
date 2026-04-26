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
from core.broker import BrokerInterface
from core.config import CONFIG
from core.db import Bot, ErrorLog, RunLog, get_session
from core.portfolio import Portfolio
from strategies.base import Strategy, StrategyContext
from strategies.aggressive_momentum import AggressiveMomentumStrategy
from strategies.etf_momentum import EtfMomentumStrategy
from strategies.mean_reversion import MeanReversionStrategy
from strategies.rsi_accumulator import RsiAccumulatorStrategy
from strategies.rsi_compounder import RsiCompoundStrategy
from strategies.rsi_recovery import RsiRecoveryStrategy
from strategies.rsi_rotation import RsiRotationStrategy
from strategies.sharp_dip import SharpDipStrategy
from strategies.trend_momentum import TrendMomentumStrategy

log = logging.getLogger(__name__)


def validate_run_dates(today: date, as_of: date | None) -> None:
    """``as_of`` is the latest bar date to use; it cannot be after *today*."""
    if as_of is not None and as_of > today:
        raise ValueError(f"as_of={as_of} is after today={today}")


STRATEGY_REGISTRY: dict[str, Callable[[], Strategy]] = {
    "aggressive_momentum": AggressiveMomentumStrategy,
    "etf_momentum": EtfMomentumStrategy,
    "mean_reversion": MeanReversionStrategy,
    "rsi_accumulator": RsiAccumulatorStrategy,
    "rsi_compounder": RsiCompoundStrategy,
    "rsi_recovery": RsiRecoveryStrategy,
    "rsi_rotation": RsiRotationStrategy,
    "sharp_dip": SharpDipStrategy,
    "trend_momentum": TrendMomentumStrategy,
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

    # Include any auxiliary tickers needed by the strategy (e.g. market filter).
    aux = params.get("market_filter_ticker")
    if aux and aux not in universe:
        universe = universe + [aux]

    min_history = int(params.get("min_history_days") or params.get("lookback_days", 70))
    # Ensure we fetch enough history for any market-filter SMA.
    mkt_sma = int(params.get("market_filter_sma", 0))
    if mkt_sma:
        min_history = max(min_history, mkt_sma)
    bars = market_data.prefetch_since(universe, min_history, as_of=as_of)
    if not bars:
        raise RuntimeError(f"bot={bot.id}: no market data for universe {universe!r}")

    last_prices = market_data.last_prices_eur(bars)
    snapshot = Portfolio.snapshot(session, bot.id, last_prices)

    # Count open BUY trades per ticker so strategies can enforce add limits.
    open_tickers = set(snapshot.positions)
    buys_per_ticker: dict[str, int] = {}
    if open_tickers:
        from core.db import Trade as TradeModel
        from sqlalchemy import func
        rows = (
            session.query(TradeModel.ticker, func.count().label("n"))
            .filter(
                TradeModel.bot_id == bot.id,
                TradeModel.side == "BUY",
                TradeModel.ticker.in_(list(open_tickers)),
            )
            .group_by(TradeModel.ticker)
            .all()
        )
        buys_per_ticker = {r.ticker: r.n for r in rows}

    ctx = StrategyContext(
        bot_id=bot.id,
        today=today,
        bars=bars,
        params=params,
        force_rebalance=force_rebalance,
        buys_per_ticker=buys_per_ticker,
        prices_eur=last_prices,
    )
    strategy = strategy_cls()
    orders = strategy.propose_orders(snapshot, ctx)
    log.info("bot=%d proposed %d orders", bot.id, len(orders))

    report = executor.run_orders(session, broker, bot.id, orders, snapshot, today)

    # End-of-run equity snapshot (one per day — updates if called again same day).
    Portfolio.record_equity_snapshot(session, bot.id, today, last_prices)

    # Run log — one entry per bot per run, even when no trades fire.
    buys  = [o.ticker for o, _ in report.approved if o.side.value == "BUY"]
    sells = [o.ticker for o, _ in report.approved if o.side.value == "SELL"]
    parts: list[str] = []
    if buys:
        parts.append("COMPRA: " + ", ".join(buys))
    if sells:
        parts.append("VENDA: " + ", ".join(sells))
    if report.rejected:
        parts.append(f"{len(report.rejected)} rebutjades")
    summary = " | ".join(parts) if parts else "Cap acció"
    session.add(RunLog(
        bot_id=bot.id,
        run_date=today,
        n_buys=len(buys),
        n_sells=len(sells),
        n_rejected=len(report.rejected),
        summary=summary,
    ))

    return report


def _broker_for_bot(bot_id: int, trading_mode: str = "paper") -> "BrokerInterface":
    """Return a broker wired to the per-bot IBKR port, respecting paper/live mode."""
    bot_cfgs = {b["id"]: b for b in CONFIG.strategies.get("bots", [])}
    cfg = bot_cfgs.get(bot_id, {})
    if trading_mode == "live":
        ibkr_port = cfg.get("ibkr_port")
    else:
        ibkr_port = cfg.get("ibkr_port_paper") or cfg.get("ibkr_port")
    backend = CONFIG.broker_backend
    if backend == "mock":
        from core.broker import MockBroker
        return MockBroker()
    if backend == "ibkr":
        if ibkr_port is None:
            port_key = "ibkr_port" if trading_mode == "live" else "ibkr_port_paper"
            raise ValueError(
                f"bot_id={bot_id} trading_mode={trading_mode!r}: "
                f"'{port_key}' not set in strategies.yaml — cannot connect to IBKR. "
                f"Add the port number for this bot and re-run."
            )
        from core.broker import IBKRBroker
        return IBKRBroker(port=int(ibkr_port))
    raise ValueError(f"Unknown BROKER_BACKEND={backend!r}")


def run_once(
    today: date | None = None,
    *,
    force_rebalance: bool = False,
    as_of: date | None = None,
    skip_bot_ids: frozenset[int] = frozenset(),
) -> list[executor.ExecutionReport]:
    """Run one full cycle for every enabled bot.

    Each bot gets its own broker connection (different IBKR Gateway port per
    account). Any per-bot exception is logged and does NOT abort other bots.

    ``skip_bot_ids`` — bot IDs to skip even if enabled (used by --auto to
    avoid re-running bots that already completed today).
    """
    today = today or datetime.now(tz=timezone.utc).date()
    validate_run_dates(today, as_of)
    reports: list[executor.ExecutionReport] = []
    with get_session() as session:
        bots = session.query(Bot).order_by(Bot.id).all()
        for bot in bots:
            if not bot.enabled:
                log.info("bot=%d (%s) disabled, skipping", bot.id, bot.name)
                continue
            if bot.id in skip_bot_ids:
                log.info("bot=%d (%s) already ran today — skipping.", bot.id, bot.name)
                continue
            broker = _broker_for_bot(bot.id, getattr(bot, "trading_mode", "paper"))
            try:
                broker.connect()
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
