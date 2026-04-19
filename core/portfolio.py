"""Per-bot virtual-book accounting.

Each bot has its own cash + positions tracked in SQLite. Orders executed
in the shared IBKR paper account are tagged per bot; we compute P&L from
the virtual book, NOT from the raw IBKR balance.

Conventions:
  - Cash is held in EUR.
  - Fills arrive in EUR (MockBroker already converts; IBKRBroker will apply
    the FX rate captured at fill time).
  - Buys debit cash by `qty*price_eur + fee_eur`; sells credit by
    `qty*price_eur - fee_eur`.
"""
from __future__ import annotations

from datetime import date
from typing import Iterable

from sqlalchemy.orm import Session

from core.db import Bot, EquitySnapshot, Position, Trade
from core.types import Fill, PortfolioSnapshot, PositionView, Side


class PortfolioError(Exception):
    pass


class Portfolio:
    """Stateless helper around a SQLAlchemy session. Always pass the session
    in — we never cache one here."""

    @staticmethod
    def cash_eur(session: Session, bot_id: int) -> float:
        """Cash = initial_capital + sum(sells - buys - fees)."""
        bot = session.query(Bot).filter(Bot.id == bot_id).one()
        cash = float(bot.initial_capital_eur)
        for t in session.query(Trade).filter(Trade.bot_id == bot_id).all():
            notional = t.qty * t.price_eur
            if t.side == Side.BUY.value:
                cash -= notional + t.fee_eur
            else:
                cash += notional - t.fee_eur
        return cash

    @staticmethod
    def open_positions(session: Session, bot_id: int) -> list[Position]:
        return session.query(Position).filter(Position.bot_id == bot_id).all()

    @staticmethod
    def snapshot(
        session: Session,
        bot_id: int,
        last_prices_eur: dict[str, float],
    ) -> PortfolioSnapshot:
        cash = Portfolio.cash_eur(session, bot_id)
        positions: dict[str, PositionView] = {}
        for p in Portfolio.open_positions(session, bot_id):
            last = last_prices_eur.get(p.ticker, p.avg_entry_eur)
            positions[p.ticker] = PositionView(
                ticker=p.ticker,
                qty=p.qty,
                avg_entry_eur=p.avg_entry_eur,
                last_price_eur=last,
            )
        return PortfolioSnapshot(bot_id=bot_id, cash_eur=cash, positions=positions)

    @staticmethod
    def apply_fill(
        session: Session,
        bot_id: int,
        fill: Fill,
        signal_reason: str,
    ) -> Trade:
        """Records the trade, updates/creates/removes the Position row.

        Returns the persisted Trade object. Caller commits.
        """
        trade = Trade(
            bot_id=bot_id,
            timestamp=fill.timestamp,
            ticker=fill.ticker,
            side=fill.side.value,
            qty=fill.qty,
            price=fill.price,
            price_eur=fill.price_eur,
            fx_rate=fill.fx_rate,
            fee_eur=fill.fee_eur,
            signal_reason=signal_reason,
            order_type="MARKET",
            broker_order_id=fill.broker_order_id,
        )
        session.add(trade)

        pos = (
            session.query(Position)
            .filter(Position.bot_id == bot_id, Position.ticker == fill.ticker)
            .one_or_none()
        )

        if fill.side is Side.BUY:
            if pos is None:
                session.add(
                    Position(
                        bot_id=bot_id,
                        ticker=fill.ticker,
                        qty=fill.qty,
                        avg_entry_eur=fill.price_eur,
                        entry_date=fill.timestamp.date(),
                    )
                )
            else:
                new_qty = pos.qty + fill.qty
                pos.avg_entry_eur = (
                    pos.avg_entry_eur * pos.qty + fill.price_eur * fill.qty
                ) / new_qty
                pos.qty = new_qty
        else:
            if pos is None:
                raise PortfolioError(
                    f"Bot {bot_id} tried to SELL {fill.ticker} with no open position"
                )
            if fill.qty > pos.qty + 1e-9:
                raise PortfolioError(
                    f"Bot {bot_id} SELL {fill.ticker} qty {fill.qty} > held {pos.qty}"
                )
            pos.qty -= fill.qty
            if pos.qty <= 1e-9:
                session.delete(pos)

        return trade

    @staticmethod
    def record_equity_snapshot(
        session: Session,
        bot_id: int,
        snap_date: date,
        last_prices_eur: dict[str, float],
    ) -> EquitySnapshot:
        snap = Portfolio.snapshot(session, bot_id, last_prices_eur)
        existing = (
            session.query(EquitySnapshot)
            .filter(
                EquitySnapshot.bot_id == bot_id,
                EquitySnapshot.snap_date == snap_date,
            )
            .one_or_none()
        )
        if existing is None:
            es = EquitySnapshot(
                bot_id=bot_id,
                snap_date=snap_date,
                cash_eur=snap.cash_eur,
                positions_value_eur=snap.positions_value_eur,
                total_eur=snap.total_eur,
            )
            session.add(es)
            return es

        existing.cash_eur = snap.cash_eur
        existing.positions_value_eur = snap.positions_value_eur
        existing.total_eur = snap.total_eur
        return existing

    @staticmethod
    def reset_virtual_book(session: Session, bot_id: int) -> None:
        """Delete all trades, positions, and equity snapshots for ``bot_id``.

        After ``commit``, implied cash is ``initial_capital_eur`` with no
        holdings. This only resets the **SQLite virtual ledger** for that
        bot; it does **not** sell or flatten anything at IBKR. If the same
        paper account already holds shares from earlier live fills, those
        broker positions remain until you close them in TWS / Gateway.
        """
        session.query(Trade).filter(Trade.bot_id == bot_id).delete(
            synchronize_session=False
        )
        session.query(Position).filter(Position.bot_id == bot_id).delete(
            synchronize_session=False
        )
        session.query(EquitySnapshot).filter(EquitySnapshot.bot_id == bot_id).delete(
            synchronize_session=False
        )

    @staticmethod
    def trades_today(session: Session, bot_id: int, today: date) -> int:
        """Count of trades placed today by `bot_id`."""
        from datetime import datetime, time, timezone

        start = datetime.combine(today, time.min, tzinfo=timezone.utc)
        end = datetime.combine(today, time.max, tzinfo=timezone.utc)
        return (
            session.query(Trade)
            .filter(
                Trade.bot_id == bot_id,
                Trade.timestamp >= start,
                Trade.timestamp <= end,
            )
            .count()
        )

    @staticmethod
    def all_tickers(session: Session, bots: Iterable[int]) -> set[str]:
        """Convenience: distinct tickers across given bots' open positions."""
        q = session.query(Position.ticker).filter(Position.bot_id.in_(list(bots)))
        return {row[0] for row in q.all()}
