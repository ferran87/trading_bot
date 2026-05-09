"""SQLAlchemy models + session factory + `python -m core.db init` CLI.

Schema (from PROJECT_PLAN.md):
  bots, trades, positions, equity_snapshots, errors.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import (
    JSON,
    Boolean,
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
    enabled = Column(Integer, nullable=False, default=1)       # 0/1 bool for SQLite
    owner = Column(String, nullable=False, default="")         # display name for dashboard selector
    trading_mode = Column(String, nullable=False, default="paper")  # "paper" | "live"
    created_at = Column(DateTime, nullable=False, default=utcnow)
    # Only count T212 deposits made on/after this date as the bot's capital.
    # NULL = count all deposits (default for paper bots).
    # Set to the date the live bot is first activated so pre-existing manual
    # portfolio deposits are never included in the bot's budget.
    live_capital_since = Column(Date, nullable=True, default=None)

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
    # "filled" — confirmed fill  |  "pending" — order sent, awaiting fill
    # "cancelled" — order cancelled before fill
    status = Column(String, nullable=False, default="filled")

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


class RunLog(Base):
    """One row per bot per automatic run — records every decision, including no-action."""

    __tablename__ = "run_logs"

    id = Column(Integer, primary_key=True)
    timestamp = Column(DateTime, nullable=False, default=utcnow, index=True)
    bot_id = Column(Integer, ForeignKey("bots.id"), nullable=False, index=True)
    run_date = Column(Date, nullable=False, index=True)
    n_buys = Column(Integer, nullable=False, default=0)
    n_sells = Column(Integer, nullable=False, default=0)
    n_rejected = Column(Integer, nullable=False, default=0)
    summary = Column(Text, nullable=False, default="")
    explanation = Column(Text, nullable=True, default=None)  # AI-generated plain-language summary
    triggered_by = Column(Text, nullable=True, default="auto")   # "auto" | "manual"


class CapitalAdjustment(Base):
    """Manual capital top-ups or withdrawals for a bot's virtual book."""

    __tablename__ = "capital_adjustments"

    id = Column(Integer, primary_key=True)
    bot_id = Column(Integer, ForeignKey("bots.id"), nullable=False, index=True)
    ts = Column(DateTime, nullable=False, default=utcnow)
    amount_eur = Column(Float, nullable=False)   # positive = deposit, negative = withdrawal
    note = Column(Text, nullable=False, default="")


# ── Strategy Lab tables (Phase 1 of the AI Trading System plan) ────────────
# These power the slow loop where Claude proposes numeric param tweaks for the
# rules-based bots and the user approves them via dashboard. See
# docs/DECISIONS.md and the plan in .claude/plans/ for full rationale.

class SimulatedClosedPosition(Base):
    """Round-trip trade reconstructed from a backtest run.

    Bootstrapped by ``scripts/bootstrap_strategy_lab.py``. Each row is a single
    closed position from a simulated bot run — Claude reads these as the
    historical corpus to reason over. New rows are added on every bootstrap
    refresh; ``backtest_run_id`` lets you tell different runs apart.
    """

    __tablename__ = "simulated_closed_positions"

    id = Column(Integer, primary_key=True)
    strategy = Column(String, nullable=False, index=True)        # 'rsi_compounder' | 'trend_momentum' | ...
    ticker = Column(String, nullable=False, index=True)
    entry_date = Column(Date, nullable=False, index=True)
    exit_date = Column(Date, nullable=False, index=True)
    hold_days = Column(Integer, nullable=False)
    entry_price_eur = Column(Float, nullable=False)
    exit_price_eur = Column(Float, nullable=False)
    qty = Column(Float, nullable=False)
    return_pct = Column(Float, nullable=False)
    return_eur = Column(Float, nullable=False)
    max_unrealized_gain_pct = Column(Float, nullable=False, default=0.0)   # peak inside the hold
    max_drawdown_pct = Column(Float, nullable=False, default=0.0)          # worst dip inside the hold
    exit_reason = Column(String, nullable=False, default="")               # 'trailing_stop' | 'rsi_exit' | ...
    regime_at_entry = Column(String, nullable=True)                        # from analysis.market_regime
    regime_at_exit = Column(String, nullable=True)
    backtest_run_id = Column(String, nullable=False, index=True)           # which bootstrap pass produced this row
    created_at = Column(DateTime, nullable=False, default=utcnow)


class RuleProposal(Base):
    """A proposed numeric parameter change, generated by ``agents/strategy_critic.py``.

    Stays ``status='pending'`` until the user approves or rejects it via the
    dashboard. Approval triggers a YAML edit and a ``RuleChangeLog`` row.
    """

    __tablename__ = "rule_proposals"

    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime, nullable=False, default=utcnow, index=True)
    strategy = Column(String, nullable=False, index=True)
    param_name = Column(String, nullable=False)              # e.g. 'trail_pct' or 'trail_pct_tight'
    current_value = Column(Float, nullable=False)
    proposed_value = Column(Float, nullable=False)
    rationale = Column(Text, nullable=False)                 # Claude's Catalan reasoning (causal, not just numeric)
    backtest_summary = Column(JSON, nullable=False, default=dict)        # {return_pct, sharpe, max_dd, win_rate, n_trades}
    walk_forward_summary = Column(JSON, nullable=False, default=dict)    # same metrics on held-out period
    passes_ratchet = Column(Boolean, nullable=False, default=False)      # return improves AND max_dd does not worsen
    status = Column(String, nullable=False, default="pending", index=True)   # 'pending' | 'approved' | 'rejected'
    decided_at = Column(DateTime, nullable=True)
    decided_by = Column(String, nullable=True)


class RuleChangeLog(Base):
    """Audit trail of every approved parameter change.

    Filled in at approval time; ``pnl_30d_after`` and ``pnl_90d_after`` are
    backfilled later by a periodic job once enough time has elapsed, so we
    can score Claude's batting average over time.
    """

    __tablename__ = "rule_change_log"

    id = Column(Integer, primary_key=True)
    applied_at = Column(DateTime, nullable=False, default=utcnow, index=True)
    proposal_id = Column(Integer, ForeignKey("rule_proposals.id"), nullable=False, index=True)
    strategy = Column(String, nullable=False, index=True)
    param_name = Column(String, nullable=False)
    old_value = Column(Float, nullable=False)
    new_value = Column(Float, nullable=False)
    git_commit_sha = Column(String, nullable=True)
    pnl_30d_after = Column(Float, nullable=True)             # filled in by track-record job
    pnl_90d_after = Column(Float, nullable=True)


# --- Engine / session ---

_engine = None
_SessionLocal: sessionmaker[Session] | None = None


def engine():
    global _engine
    if _engine is None:
        _engine = create_engine(CONFIG.db_url, future=True)
        Base.metadata.create_all(_engine)  # idempotent — creates missing tables on first use
        _migrate(_engine)
    return _engine


def _migrate(eng) -> None:
    """Apply lightweight additive migrations (new nullable columns only).

    Uses IF NOT EXISTS so each statement is idempotent on PostgreSQL.
    SQLite doesn't support IF NOT EXISTS on ADD COLUMN, so we fall back to
    catching the "duplicate column" error there.
    """
    import logging as _log
    from sqlalchemy import text

    _miglog = _log.getLogger(__name__)

    # Prefer IF NOT EXISTS (PostgreSQL); SQLite will raise on duplicate column
    # which is caught and ignored below.
    migrations = [
        "ALTER TABLE run_logs ADD COLUMN IF NOT EXISTS explanation TEXT",
        "ALTER TABLE trades ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'filled'",
        "ALTER TABLE run_logs ADD COLUMN IF NOT EXISTS triggered_by TEXT DEFAULT 'auto'",
        "ALTER TABLE bots ADD COLUMN IF NOT EXISTS live_capital_since DATE",
    ]
    with eng.connect() as conn:
        for sql in migrations:
            try:
                conn.execute(text(sql))
                conn.commit()
            except Exception as exc:
                err = str(exc).lower()
                # SQLite raises "duplicate column name"; PostgreSQL IF NOT EXISTS
                # prevents this. Anything else (e.g. permissions, timeout) is
                # worth logging so it's not silently missed.
                if "duplicate column" not in err and "already exists" not in err:
                    _miglog.warning("_migrate: skipped migration (%s): %s", sql[:60], exc)
                # Always continue — a single migration failure should not
                # block the rest of the application from starting.


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
    if "sqlite" in CONFIG.db_url:
        Path(CONFIG.db_path).parent.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(engine())

    initial_capital = float(CONFIG.settings["guardrails"]["initial_capital_eur"])
    with get_session() as s:
        for b in CONFIG.strategies["bots"]:
            # Parse optional live_capital_since (YYYY-MM-DD string or None)
            lcs_raw = b.get("live_capital_since")
            lcs: date | None = (
                date.fromisoformat(str(lcs_raw)) if lcs_raw else None
            )

            existing = s.query(Bot).filter(Bot.id == b["id"]).one_or_none()
            if existing is None:
                s.add(
                    Bot(
                        id=b["id"],
                        name=b["name"],
                        strategy=b["strategy"],
                        initial_capital_eur=initial_capital,
                        enabled=1 if b.get("enabled", True) else 0,
                        owner=b.get("owner", ""),
                        trading_mode=b.get("trading_mode", "paper"),
                        live_capital_since=lcs,
                    )
                )
            else:
                existing.name = b["name"]
                existing.strategy = b["strategy"]
                existing.enabled = 1 if b.get("enabled", True) else 0
                existing.owner = b.get("owner", existing.owner)
                existing.trading_mode = b.get("trading_mode", existing.trading_mode)
                # Only update live_capital_since if explicitly set in YAML
                # (don't overwrite a date set via dashboard with None)
                if lcs is not None:
                    existing.live_capital_since = lcs
        s.commit()
    print(f"DB ready at {CONFIG.db_path}")


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "init":
        init_db()
    else:
        print("Usage: python -m core.db init")
        sys.exit(1)
