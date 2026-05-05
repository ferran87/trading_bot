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
    trigger: str = "auto",
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

    # Connect to the broker NOW — market data is already downloaded so the
    # ib_async asyncio event loop won't be running while yfinance makes its
    # HTTP requests (which causes a silent hang on Windows ProactorEventLoop).
    # Callers must NOT call broker.connect() before run_bot(); disconnect()
    # is still handled by run_once()'s finally block (it's a safe no-op if
    # connect was never called).
    broker.connect()

    # Execute orders — wrapped so RunLog is always written even on failure.
    exec_error: Exception | None = None
    report: executor.ExecutionReport | None = None
    try:
        report = executor.run_orders(session, broker, bot.id, orders, snapshot, today)
    except Exception as exc:
        exec_error = exc
        log.warning("bot=%d executor raised: %s", bot.id, exc)

    # End-of-run equity snapshot (one per day — updates if called again same day).
    try:
        Portfolio.record_equity_snapshot(session, bot.id, today, last_prices)
    except Exception as snap_exc:
        log.warning("bot=%d equity snapshot failed: %s", bot.id, snap_exc)

    # Run log — written unconditionally so the bot always appears in the dashboard.
    if report is not None:
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
        n_buys, n_sells, n_rej = len(buys), len(sells), len(report.rejected)
    else:
        # Executor failed before producing a report — record the error as summary.
        summary = f"Error: {exec_error}"
        n_buys, n_sells, n_rej = 0, 0, len(orders)

    session.add(RunLog(
        bot_id=bot.id,
        run_date=today,
        n_buys=n_buys,
        n_sells=n_sells,
        n_rejected=n_rej,
        summary=summary,
        triggered_by=trigger,
    ))

    # Re-raise so run_once catches it, rolls back, and adds an ErrorLog.
    if exec_error is not None:
        raise exec_error

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
    if backend == "t212":
        from core.broker import Trading212Broker
        # Demo mode for paper trading; live mode when trading_mode=="live"
        demo = (trading_mode != "live")
        return Trading212Broker(demo=demo)
    raise ValueError(f"Unknown BROKER_BACKEND={backend!r}")


def _resolve_pending_orders_all_bots() -> None:
    """Resolve pending IBKR orders for all bots before the daily run.

    Groups bots by IBKR port (so we only connect once per Gateway) and calls
    ``agents.reconciliation.resolve_pending_orders()``.  Failures are logged
    but never block the main run.
    """
    from agents.reconciliation import resolve_pending_orders, import_manual_positions, cancel_orphan_orders

    try:
        with get_session() as s:
            enabled_bots = s.query(Bot).filter(Bot.enabled == 1).all()
            bot_ids = [b.id for b in enabled_bots]
    except Exception as exc:
        log.warning("_resolve_pending_orders: DB error fetching bots: %s", exc)
        return

    # Collect unique ports across all enabled bots
    bot_cfgs = {b["id"]: b for b in CONFIG.strategies.get("bots", [])}
    port_to_bots: dict[int, list[int]] = {}
    for bot_id in bot_ids:
        cfg = bot_cfgs.get(bot_id, {})
        # Use paper port by default (all bots currently paper).
        port = cfg.get("ibkr_port_paper") or cfg.get("ibkr_port")
        if port is None:
            continue
        port = int(port)
        port_to_bots.setdefault(port, []).append(bot_id)

    for port, ids in port_to_bots.items():
        primary = ids[0] if ids else None

        # 0. Cancel orphan IBKR orders (placed by crashed runs, not in DB)
        try:
            cancelled = cancel_orphan_orders(ids, port)
            if cancelled:
                log.info(
                    "_resolve_pending_orders: cancelled %d orphan order(s) on port %d",
                    cancelled, port,
                )
        except Exception as exc:
            log.warning(
                "_resolve_pending_orders: cancel_orphan port=%d failed: %s", port, exc
            )

        # 1. Resolve pending DB orders against actual IBKR fills
        try:
            resolved = resolve_pending_orders(ids, port)
            if resolved:
                log.info(
                    "_resolve_pending_orders: resolved %d pending order(s) on port %d",
                    resolved, port,
                )
        except Exception as exc:
            log.warning(
                "_resolve_pending_orders: port=%d failed: %s", port, exc
            )

        # 2. Import manual positions from IBKR that are not in SQLite
        try:
            imported = import_manual_positions(ids, port, primary_bot_id=primary)
            if imported:
                log.info(
                    "_resolve_pending_orders: imported %d manual position(s) on port %d",
                    imported, port,
                )
        except Exception as exc:
            log.warning(
                "_resolve_pending_orders: import_manual port=%d failed: %s", port, exc
            )


def _sync_t212_initial_capital(today: date) -> None:
    """Fetch the live T212 account balance and distribute it equally among
    enabled bots that have not yet traded (initial_capital_eur is still the
    settings.yaml default, or the account value changed).

    This runs once per day at the start of run_once() when BROKER_BACKEND=t212,
    replacing the hardcoded initial_capital_eur in settings.yaml with the real
    account value split across active bots.  Bots that already have trades are
    left untouched — their virtual book tracks from their first fill.
    """
    from core.db import Trade, get_session

    try:
        from core.broker import Trading212Broker

        with get_session() as session:
            all_bots = session.query(Bot).order_by(Bot.id).all()

            # Group enabled bots by trading_mode so paper and live accounts
            # are sized independently.
            for mode in ("paper", "live"):
                demo = (mode == "paper")
                broker = Trading212Broker(demo=demo)
                try:
                    acct = broker._fetch_account()
                except Exception as exc:
                    log.warning(
                        "_sync_t212_initial_capital: could not fetch T212 %s balance: %s",
                        mode, exc,
                    )
                    continue

                total_eur = acct.get("total_eur", 0.0)
                if total_eur <= 0:
                    log.warning(
                        "_sync_t212_initial_capital: T212 %s balance is %.2f — skipping",
                        mode, total_eur,
                    )
                    continue

                enabled_bots = [
                    b for b in all_bots
                    if b.enabled and getattr(b, "trading_mode", "paper") == mode
                ]
                n = len(enabled_bots)
                if n == 0:
                    continue

                per_bot_eur = round(total_eur / n, 2)

                for b in enabled_bots:
                    trade_count = (
                        session.query(Trade).filter(Trade.bot_id == b.id).count()
                    )
                    if trade_count == 0 and abs(b.initial_capital_eur - per_bot_eur) > 1.0:
                        log.info(
                            "_sync_t212_initial_capital: bot=%d %s mode=%s "
                            "initial_capital_eur %.2f -> %.2f (T212 total=%.2f / %d bots)",
                            b.id, b.name, mode,
                            b.initial_capital_eur, per_bot_eur, total_eur, n,
                        )
                        b.initial_capital_eur = per_bot_eur

                session.commit()

    except Exception as exc:
        log.warning("_sync_t212_initial_capital failed: %s", exc)


def run_once(
    today: date | None = None,
    *,
    force_rebalance: bool = False,
    as_of: date | None = None,
    skip_bot_ids: frozenset[int] = frozenset(),
    trigger: str = "auto",
) -> list[executor.ExecutionReport]:
    """Run one full cycle for every enabled bot.

    Each bot gets its own broker connection (different IBKR Gateway port per
    account). Any per-bot exception is logged and does NOT abort other bots.

    ``skip_bot_ids`` — bot IDs to skip even if enabled (used by --auto to
    avoid re-running bots that already completed today).
    """
    today = today or datetime.now(tz=timezone.utc).date()
    validate_run_dates(today, as_of)

    # ── Pre-run: resolve any pending orders from previous sessions ─────────────
    # Only for IBKR backend; mock and T212 use REST-based reconciliation
    # (T212 orders fill synchronously, so there are no "pending" orders to
    # resolve on startup).
    if CONFIG.broker_backend == "ibkr":
        _resolve_pending_orders_all_bots()

    # ── Pre-run: sync T212 account balance → per-bot initial capital ───────────
    # For fresh bots (no trades yet) the virtual book cash equals
    # initial_capital_eur.  Rather than hardcoding this in settings.yaml,
    # we fetch the live T212 account balance and split it equally among
    # enabled bots of the same trading mode (paper or live).
    if CONFIG.broker_backend == "t212":
        _sync_t212_initial_capital(today)

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
                r = run_bot(
                    session,
                    broker,
                    bot,
                    today,
                    force_rebalance=force_rebalance,
                    as_of=as_of,
                    trigger=trigger,
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
