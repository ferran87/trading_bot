"""Backtesting engine.

Simulates a bot day-by-day over a historical date range using real price data
from yfinance (truncated to each simulated day via `as_of`). All trades and
positions are recorded in an isolated in-memory SQLite DB — the live DB is
never touched.

Usage:
    from datetime import date, timedelta
    from backtesting.engine import run_backtest

    result = run_backtest(bot_id=1, start_date=date.today() - timedelta(30),
                          end_date=date.today())
    print(result.equity_df)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

import pandas as pd
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy.orm import sessionmaker

from analysis import market_data
from core import fx
from core.broker import MockBroker
from core.config import CONFIG
from core.db import Base, Bot, EquitySnapshot, Trade

log = logging.getLogger(__name__)


@dataclass
class BacktestResult:
    bot_id: int
    bot_name: str
    initial_capital_eur: float
    equity_df: pd.DataFrame    # cols: date, total_eur, cash_eur, positions_value_eur
    trades_df: pd.DataFrame    # cols: date, ticker, side, qty, price_eur, fee_eur, signal_reason
    last_prices: dict[str, float] = field(default_factory=dict)  # ticker -> end-of-backtest close (EUR)
    errors: list[str] = field(default_factory=list)

    @property
    def total_return_pct(self) -> float:
        if self.equity_df.empty:
            return 0.0
        final = float(self.equity_df["total_eur"].iloc[-1])
        return final / self.initial_capital_eur - 1.0

    @property
    def sharpe(self) -> float:
        if self.equity_df.empty:
            return float("nan")
        ser = self.equity_df["total_eur"]
        if len(ser) < 2:
            return float("nan")
        daily = ser.pct_change().dropna()
        std = daily.std()
        if std == 0 or pd.isna(std):
            return float("nan")
        return float((daily.mean() / std) * (252 ** 0.5))

    @property
    def max_drawdown(self) -> float:
        if self.equity_df.empty:
            return 0.0
        ser = self.equity_df["total_eur"]
        if ser.empty:
            return 0.0
        running_max = ser.cummax()
        dd = (ser - running_max) / running_max
        return float(dd.min())


def _trading_days(start: date, end: date) -> list[date]:
    """Return actual NYSE trading days between start and end (inclusive).

    Uses a single yfinance download of AAPL to get the real market calendar
    instead of a naive weekday filter, which incorrectly includes US market
    holidays (New Year's Day, MLK Day, Good Friday, etc.).  AAPL is the most
    liquid NYSE/NASDAQ ticker and always has a bar on every real trading day.

    Falls back to weekday-only filtering if the download fails.
    """
    import yfinance as yf
    try:
        df = yf.download(
            "AAPL",
            start=start.isoformat(),
            end=(end + timedelta(days=1)).isoformat(),
            auto_adjust=True,
            progress=False,
            timeout=15,
        )
        if not df.empty:
            return sorted(d.date() for d in df.index)
    except Exception:
        pass
    # Fallback: weekday filter (same as before, but warns)
    log.warning("_trading_days: yfinance fallback — holidays not excluded")
    days: list[date] = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def run_backtest(
    bot_id: int,
    start_date: date,
    end_date: date,
) -> BacktestResult:
    """Simulate `bot_id` day-by-day from `start_date` to `end_date`.

    Downloads market data once per ticker (cached) then truncates bars to each
    simulated trading day, so the strategy only sees information available at
    close on that day.
    """
    # Look up bot from the live DB — avoids stale in-process config cache.
    from core.db import get_session as _get_session
    from core.db import Bot as _BotModel
    with _get_session() as _s:
        _db_bot = _s.query(_BotModel).filter(_BotModel.id == bot_id).one_or_none()
    if _db_bot is None:
        raise ValueError(f"No bot with id={bot_id} in the database")
    bot_cfg = {"id": _db_bot.id, "name": _db_bot.name, "strategy": _db_bot.strategy}

    initial_capital = float(CONFIG.settings["guardrails"]["initial_capital_eur"])

    # Isolated in-memory database — live DB is untouched.
    eng = sa_create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(eng)
    SessionLocal = sessionmaker(bind=eng, expire_on_commit=False, future=True)

    with SessionLocal() as s:
        s.add(
            Bot(
                id=bot_id,
                name=bot_cfg["name"],
                strategy=bot_cfg["strategy"],
                initial_capital_eur=initial_capital,
                enabled=1,
            )
        )
        s.commit()

    broker = MockBroker()
    broker.connect()
    market_data.clear_cache()
    fx.clear_cache()

    errors: list[str] = []
    for day in _trading_days(start_date, end_date):
        broker.sim_date = day  # fill timestamps reflect the simulated date
        with SessionLocal() as s:
            bot_row = s.query(Bot).filter(Bot.id == bot_id).one()
            try:
                from core.runner import run_bot  # lazy import to avoid top-level strategy imports on cloud
                run_bot(s, broker, bot_row, day, force_rebalance=False, as_of=day)
                s.commit()
            except Exception as exc:
                s.rollback()
                msg = f"{day}: {exc}"
                log.warning("backtest bot=%d %s", bot_id, msg)
                errors.append(msg)

    # Resolve universe and fetch end-of-backtest prices for open position P&L.
    params = CONFIG.strategies["strategies"][bot_cfg["strategy"]]
    uni_spec = params.get("universe", [])
    universe_tickers: list[str] = []
    if isinstance(uni_spec, str):
        universe_tickers = list(CONFIG.watchlists[uni_spec])
    elif isinstance(uni_spec, list):
        for grp in uni_spec:
            universe_tickers.extend(CONFIG.watchlists[grp])
    aux = params.get("market_filter_ticker")
    if aux and aux not in universe_tickers:
        universe_tickers.append(aux)
    min_hist = int(params.get("min_history_days") or params.get("lookback_days", 70))
    mkt_sma = int(params.get("market_filter_sma", 0))
    if mkt_sma:
        min_hist = max(min_hist, mkt_sma)
    final_bars = market_data.prefetch_since(universe_tickers, min_hist, as_of=end_date)
    last_prices = market_data.last_prices_eur(final_bars) if final_bars else {}

    with SessionLocal() as s:
        eq_rows = (
            s.query(EquitySnapshot)
            .filter(EquitySnapshot.bot_id == bot_id)
            .order_by(EquitySnapshot.snap_date)
            .all()
        )
        trade_rows = (
            s.query(Trade)
            .filter(Trade.bot_id == bot_id)
            .order_by(Trade.timestamp)
            .all()
        )

    equity_df = pd.DataFrame(
        [
            {
                "date": r.snap_date,
                "total_eur": r.total_eur,
                "cash_eur": r.cash_eur,
                "positions_value_eur": r.positions_value_eur,
            }
            for r in eq_rows
        ]
    )
    if not equity_df.empty:
        equity_df["date"] = pd.to_datetime(equity_df["date"])

    trades_df = pd.DataFrame(
        [
            {
                "date": t.timestamp,
                "ticker": t.ticker,
                "side": t.side,
                "qty": t.qty,
                "price_eur": round(t.price_eur, 2),
                "fee_eur": round(t.fee_eur, 2),
                "signal_reason": t.signal_reason,
            }
            for t in trade_rows
        ]
    )

    return BacktestResult(
        bot_id=bot_id,
        bot_name=bot_cfg["name"],
        initial_capital_eur=initial_capital,
        equity_df=equity_df,
        trades_df=trades_df,
        last_prices=last_prices,
        errors=errors,
    )
