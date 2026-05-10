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
from strategies.ai_thesis import AiThesisStrategy
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
    "ai_thesis": AiThesisStrategy,          # Phase 2 — AI Thesis Bot (bot 30)
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
    """Sync initial_capital_eur for all enabled T212 bots from deposited amounts.

    Paper bots: uses the full account deposit history, split equally.

    Live bots: each bot may have a ``live_capital_since`` date. When set, only
    deposits made on or after that date are counted. This lets users who already
    have a manual portfolio activate the bot without their pre-existing capital
    being included in the bot's budget — only new monthly top-ups are treated
    as the bot's money.

    Why deposited amount and not current account value:
      - Current value includes unrealised P&L → return % would read 0% on a
        fresh run after a good day.
      - Deposits represent actual capital committed by the user.
      - New deposits auto-increase initial_capital_eur (and therefore cash_eur),
        giving the bot more buying power without any manual intervention.

    This runs once per day at the start of run_once() when BROKER_BACKEND=t212.
    All enabled bots are updated unconditionally so new deposits land immediately.
    """
    from core.db import get_session

    try:
        from core.broker import Trading212Broker

        with get_session() as session:
            all_bots = session.query(Bot).order_by(Bot.id).all()

            for mode in ("paper", "live"):
                demo = (mode == "paper")
                broker = Trading212Broker(demo=demo)

                enabled_bots = [
                    b for b in all_bots
                    if b.enabled and getattr(b, "trading_mode", "paper") == mode
                ]
                if not enabled_bots:
                    continue

                for b in enabled_bots:
                    # Live bots with a since-date get their own filtered deposit total;
                    # paper bots and live bots without a since-date share the full total.
                    since = getattr(b, "live_capital_since", None)

                    try:
                        deposited_eur = broker._fetch_total_deposited(since_date=since)
                    except Exception as exc:
                        log.warning(
                            "_sync_t212_initial_capital: could not fetch T212 %s "
                            "transaction history for bot=%d: %s",
                            mode, b.id, exc,
                        )
                        continue

                    if deposited_eur <= 0:
                        log.warning(
                            "_sync_t212_initial_capital: bot=%d T212 %s deposited=%.2f "
                            "(since=%s) — skipping",
                            b.id, mode, deposited_eur, since,
                        )
                        continue

                    # For bots without a since-date, split total equally across peers
                    # (paper bots share the paper account).  For bots with a since-date
                    # (live bots with their own deposit slice), use the full filtered amount.
                    if since is None:
                        n_peers = len(enabled_bots)
                        per_bot_eur = round(deposited_eur / n_peers, 2)
                    else:
                        per_bot_eur = round(deposited_eur, 2)

                    if abs(b.initial_capital_eur - per_bot_eur) > 0.01:
                        log.info(
                            "_sync_t212_initial_capital: bot=%d %s mode=%s "
                            "initial_capital_eur %.2f -> %.2f "
                            "(deposited=%.2f since=%s)",
                            b.id, b.name, mode,
                            b.initial_capital_eur, per_bot_eur,
                            deposited_eur, since,
                        )
                        b.initial_capital_eur = per_bot_eur

                session.commit()

    except Exception as exc:
        log.warning("_sync_t212_initial_capital failed: %s", exc)


def _resolve_t212_pending_orders() -> None:
    """Update pending T212 trades with actual filled qty/price from the T212 API.

    When a T212 order is placed outside market hours it stays NEW until the
    exchange opens.  The executor records it as ``status='pending'`` with the
    reference price and the *floored* integer qty.  On the next run, this
    function polls T212 for each pending broker_order_id and:

    - If FILLED: updates qty → filledQuantity, price → filledPrice, status → filled.
      Then recomputes the Position row so avg_entry_eur and qty are accurate.
    - If CANCELLED / REJECTED: marks the trade cancelled and removes the position
      if the position was only ever this trade.
    - Still NEW / other: leaves untouched (will retry next run).

    Failures per-order are logged but never block the main run.
    """
    try:
        from core.db import Position, Trade, get_session
        from core.portfolio import Portfolio

        with get_session() as session:
            pending = (
                session.query(Trade)
                .filter(
                    Trade.status == "pending",
                    Trade.broker_order_id.isnot(None),
                )
                .all()
            )
            if not pending:
                return

            log.info("_resolve_t212_pending: checking %d pending order(s)", len(pending))

            # Group by trading_mode so we use the right T212 account (paper vs live)
            from core.db import Bot
            bot_mode: dict[int, str] = {
                b.id: getattr(b, "trading_mode", "paper")
                for b in session.query(Bot).all()
            }

            from core.broker import Trading212Broker

            # Fetch complete order history once per demo/live account and index
            # by order ID.  The individual /orders/{id} endpoint returns 404 once
            # an order is old, so the history endpoint is the only reliable source.
            def _fetch_history(broker: Trading212Broker) -> dict[str, dict]:
                history: dict[str, dict] = {}
                url: str | None = "/equity/history/orders"
                while url:
                    try:
                        data = broker._get(url, params={"limit": 50})
                    except Exception:
                        break
                    for item in data.get("items", []):
                        order = item.get("order", {})
                        oid = str(order.get("id", ""))
                        if oid:
                            history[oid] = item
                    next_path = data.get("nextPagePath")
                    url = next_path if next_path else None
                return history

            brokers: dict[bool, Trading212Broker] = {}
            histories: dict[bool, dict[str, dict]] = {}

            for trade in pending:
                demo = (bot_mode.get(trade.bot_id, "paper") == "paper")
                if demo not in brokers:
                    brokers[demo]   = Trading212Broker(demo=demo)
                    histories[demo] = _fetch_history(brokers[demo])

                order_id  = str(trade.broker_order_id)
                item      = histories[demo].get(order_id)
                if item is None:
                    log.debug(
                        "_resolve_t212_pending: order %s not in history — leaving pending",
                        order_id,
                    )
                    continue

                order_data = item.get("order", {})
                fill_data  = item.get("fill",  {})
                status = order_data.get("status", "")

                if status == "FILLED":
                    wallet     = fill_data.get("walletImpact", {})
                    taxes      = wallet.get("taxes", [])
                    # fxRate: how many account-currency units per 1 EUR.
                    # e.g. 1.175 means "1 EUR = $1.175 USD".
                    # For EUR-denominated instruments fxRate is absent or 1.
                    fx_rate    = float(wallet.get("fxRate", 1) or 1)

                    filled_qty  = float(
                        fill_data.get("quantity")
                        or order_data.get("filledQuantity")
                        or trade.qty
                    )
                    # T212 fill price is in the instrument's NATIVE currency
                    # (e.g. USD for MSFT, GS, JPM; EUR for ASML.AS, BNP.PA).
                    # We must divide by fxRate to get the EUR equivalent.
                    filled_price_native = float(
                        fill_data.get("price")
                        or order_data.get("filledPrice")
                        or (trade.price_eur * fx_rate)  # fallback: reverse stored EUR
                    )
                    filled_price_eur = (
                        filled_price_native / fx_rate if fx_rate > 0
                        else filled_price_native
                    )

                    # Actual fee from T212 taxes (FX conversion fee for USD stocks).
                    # T212 returns tax quantities as negative (wallet deductions),
                    # so we take abs() to store fees as positive costs.
                    fee_eur = abs(sum(
                        float(t.get("quantity") or t.get("value") or 0)
                        for t in taxes
                    ))
                    if fee_eur == 0:
                        net_abs = abs(float(wallet.get("netValue") or 0))
                        if net_abs > 0:
                            implied = filled_qty * filled_price_eur
                            if abs(net_abs - implied) > 0.005:
                                fee_eur = abs(net_abs - implied)

                    old_qty   = trade.qty
                    old_price = trade.price_eur
                    trade.qty       = filled_qty
                    trade.price_eur = filled_price_eur
                    if fee_eur > 0:
                        trade.fee_eur = fee_eur
                    trade.status    = "filled"

                    log.info(
                        "_resolve_t212_pending: FILLED bot=%d %s %s qty %.4f->%.4f "
                        "native_price=%.4f fx=%.6f price_eur %.4f->%.4f fee=%.4f",
                        trade.bot_id, trade.side, trade.ticker,
                        old_qty, filled_qty,
                        filled_price_native, fx_rate, old_price, filled_price_eur,
                        fee_eur,
                    )

                    # Recompute the position from all trades for this (bot, ticker)
                    _recompute_position(session, trade.bot_id, trade.ticker)

                elif status in ("CANCELLED", "REJECTED"):
                    trade.status = "cancelled"
                    log.info(
                        "_resolve_t212_pending: %s bot=%d %s %s — marking cancelled",
                        status, trade.bot_id, trade.side, trade.ticker,
                    )
                    # Remove position if there are no other filled buys
                    _recompute_position(session, trade.bot_id, trade.ticker)

                else:
                    log.debug(
                        "_resolve_t212_pending: order %s still %s — leaving pending",
                        trade.broker_order_id, status,
                    )

            session.commit()
            log.info("_resolve_t212_pending: done")

    except Exception as exc:
        log.warning("_resolve_t212_pending_orders failed (non-fatal): %s", exc)


def _recompute_position(session, bot_id: int, ticker: str) -> None:
    """Recompute and upsert the Position row for (bot_id, ticker) from filled trades.

    Recalculates qty and avg_entry_eur from the trades ledger so the position
    is always consistent with what was actually executed.
    """
    from core.db import Position, Trade

    filled_buys  = session.query(Trade).filter(
        Trade.bot_id == bot_id,
        Trade.ticker == ticker,
        Trade.side   == "BUY",
        Trade.status == "filled",
    ).all()
    filled_sells = session.query(Trade).filter(
        Trade.bot_id == bot_id,
        Trade.ticker == ticker,
        Trade.side   == "SELL",
        Trade.status == "filled",
    ).all()

    total_bought = sum(t.qty for t in filled_buys)
    total_sold   = sum(t.qty for t in filled_sells)
    net_qty      = total_bought - total_sold

    pos = session.query(Position).filter(
        Position.bot_id == bot_id,
        Position.ticker == ticker,
    ).one_or_none()

    if net_qty <= 1e-6:
        if pos is not None:
            session.delete(pos)
        return

    # Weighted average entry price across all filled buys
    avg_entry = (
        sum(t.qty * t.price_eur for t in filled_buys) / total_bought
        if total_bought > 0 else 0.0
    )

    if pos is None:
        from core.db import Position as Pos
        from datetime import date
        first_buy = min(filled_buys, key=lambda t: t.timestamp)
        pos = Pos(
            bot_id=bot_id,
            ticker=ticker,
            qty=net_qty,
            avg_entry_eur=avg_entry,
            entry_date=first_buy.timestamp.date() if first_buy.timestamp else date.today(),
        )
        session.add(pos)
    else:
        pos.qty           = net_qty
        pos.avg_entry_eur = avg_entry


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
    if CONFIG.broker_backend == "ibkr":
        _resolve_pending_orders_all_bots()
    elif CONFIG.broker_backend == "t212":
        _resolve_t212_pending_orders()

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
