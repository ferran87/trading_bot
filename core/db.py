"""SQLAlchemy models + session factory + `python -m core.db init` CLI.

Schema (from PROJECT_PLAN.md):
  bots, trades, positions, equity_snapshots, errors.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import (
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Session, relationship, sessionmaker

from core.config import CONFIG


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


class Bot(Base):
    __tablename__ = "bots"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False, unique=True)
    strategy = Column(String, nullable=False)
    initial_capital_eur = Column(Float, nullable=False)
    enabled = Column(Integer, nullable=False, default=1)     # 0/1 bool for SQLite
    created_at = Column(DateTime, nullable=False, default=utcnow)

    trades = relationship("Trade", back_populates="bot", cascade="all, delete-orphan")
    positions = relationship("Position", back_populates="bot", cascade="all, delete-orphan")


class Trade(Base):
    """Immutable record of every fill."""

    __tablename__ = "trades"

    id = Column(Integer, primary_key=True)
    bot_id = Column(Integer, ForeignKey("bots.id"), nullable=False, index=True)
    timestamp = Column(DateTime, nullable=False, default=utcnow, index=True)
    ticker = Column(String, nullable=False, index=True)
    side = Column(String, nullable=False)              # "BUY" | "SELL"
    qty = Column(Float, nullable=False)
    price = Column(Float, nullable=False)              # fill price in local ccy
    price_eur = Column(Float, nullable=False)          # fill price converted to EUR
    fx_rate = Column(Float, nullable=False, default=1.0)
    fee_eur = Column(Float, nullable=False, default=0.0)
    signal_reason = Column(Text, nullable=False, default="")
    order_type = Column(String, nullable=False, default="MARKET")
    broker_order_id = Column(String, nullable=True)

    bot = relationship("Bot", back_populates="trades")


class Position(Base):
    """Current open positions. One row per (bot, ticker)."""

    __tablename__ = "positions"
    __table_args__ = (UniqueConstraint("bot_id", "ticker", name="uq_position_bot_ticker"),)

    id = Column(Integer, primary_key=True)
    bot_id = Column(Integer, ForeignKey("bots.id"), nullable=False, index=True)
    ticker = Column(String, nullable=False, index=True)
    qty = Column(Float, nullable=False)
    avg_entry_eur = Column(Float, nullable=False)     # weighted avg entry in EUR
    entry_date = Column(Date, nullable=False)

    bot = relationship("Bot", back_populates="positions")


class EquitySnapshot(Base):
    """Daily equity curve, one row per (bot, date)."""

    __tablename__ = "equity_snapshots"
    __table_args__ = (UniqueConstraint("bot_id", "snap_date", name="uq_equity_bot_date"),)

    id = Column(Integer, primary_key=True)
    bot_id = Column(Integer, ForeignKey("bots.id"), nullable=False, index=True)
    snap_date = Column(Date, nullable=False, index=True)
    cash_eur = Column(Float, nullable=False)
    positions_value_eur = Column(Float, nullable=False)
    total_eur = Column(Float, nullable=False)


class ErrorLog(Base):
    __tablename__ = "errors"

    id = Column(Integer, primary_key=True)
    timestamp = Column(DateTime, nullable=False, default=utcnow, index=True)
    bot_id = Column(Integer, ForeignKey("bots.id"), nullable=True, index=True)
    component = Column(String, nullable=False)
    message = Column(Text, nullable=False)
    traceback = Column(Text, nullable=True)


# --- Engine / session ---

_engine = None
_SessionLocal: sessionmaker[Session] | None = None


def engine():
    global _engine
    if _engine is None:
        _engine = create_engine(CONFIG.db_url, future=True)
    return _engine


def session_factory() -> sessionmaker[Session]:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=engine(), expire_on_commit=False, future=True)
    return _SessionLocal


def get_session() -> Session:
    return session_factory()()


# --- Init CLI ---


def init_db() -> None:
    """Create tables and seed the 3 bots from strategies.yaml."""
    Path(CONFIG.db_path).parent.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(engine())

    initial_capital = float(CONFIG.settings["guardrails"]["initial_capital_eur"])
    with get_session() as s:
        for b in CONFIG.strategies["bots"]:
            existing = s.query(Bot).filter(Bot.id == b["id"]).one_or_none()
            if existing is None:
                s.add(
                    Bot(
                        id=b["id"],
                        name=b["name"],
                        strategy=b["strategy"],
                        initial_capital_eur=initial_capital,
                        enabled=1 if b.get("enabled", True) else 0,
                    )
                )
            else:
                existing.name = b["name"]
                existing.strategy = b["strategy"]
                existing.enabled = 1 if b.get("enabled", True) else 0
        s.commit()
    print(f"DB ready at {CONFIG.db_path}")


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "init":
        init_db()
    else:
        print("Usage: python -m core.db init")
        sys.exit(1)
