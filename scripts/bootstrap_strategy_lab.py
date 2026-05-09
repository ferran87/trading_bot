"""Bootstrap the Strategy Lab's simulated trade history.

For each strategy (rsi_compounder via bot 7, trend_momentum via bot 10) this
runs the existing backtest engine over a multi-year window, reconstructs every
round-trip closed position from the simulated trades ledger, and inserts the
result into ``simulated_closed_positions`` so the Strategy Critic agent has a
historical corpus to reason over from day one.

Idempotency: each run uses a unique ``backtest_run_id`` so re-running adds new
rows alongside the old ones (you can compare runs). To replace a strategy's
rows entirely use ``--replace``.

Usage:
    python scripts/bootstrap_strategy_lab.py
    python scripts/bootstrap_strategy_lab.py --replace
    python scripts/bootstrap_strategy_lab.py --start 2023-01-01 --end 2025-12-31
    python scripts/bootstrap_strategy_lab.py --strategy rsi_compounder
"""
from __future__ import annotations

import argparse
import logging
import sys
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

# Project root on sys.path so `python scripts/...` works
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from analysis import market_data
from analysis.market_regime import compute_regimes
from backtesting.engine import run_backtest
from core.config import CONFIG  # noqa: F401 — triggers .env load
from core.db import SimulatedClosedPosition, get_session

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


# Strategy → representative bot id used to drive the backtest.
# Both bots use Ferran's paper config; the backtest runs in an in-memory DB
# so the live bot state is never touched.
STRATEGY_BOTS: dict[str, int] = {
    "rsi_compounder": 7,
    "trend_momentum": 10,
}


@dataclass
class _Lot:
    """In-flight position state during round-trip reconstruction."""

    qty: float = 0.0
    cost_eur: float = 0.0          # qty-weighted total entry cost (excl fees)
    fees_eur: float = 0.0          # accumulated fees (entries + partial exits)
    entry_date: date | None = None
    last_exit_reason: str = ""

    @property
    def avg_entry_eur(self) -> float:
        return self.cost_eur / self.qty if self.qty > 0 else 0.0


def _reconstruct_round_trips(
    trades: pd.DataFrame,
) -> list[dict]:
    """Walk the trades DataFrame and emit one record per closed position.

    Handles pyramid adds (multiple BUYs before a SELL) and partial exits
    (multiple SELLs to close). A position closes when running qty drops to
    ~0 — at that moment we emit the round-trip and reset state for that
    ticker.

    Returns a list of dicts ready for ``SimulatedClosedPosition`` insertion
    (without the per-hold metrics or regime tags — those get filled later).
    """
    if trades.empty:
        return []

    closed: list[dict] = []
    state: dict[str, _Lot] = {}
    exit_proceeds: dict[str, float] = {}    # ticker → sum(qty*price) for sells of current open lot
    exit_qty: dict[str, float] = {}
    last_exit_date: dict[str, date] = {}

    # Ensure we walk in chronological order
    trades_sorted = trades.sort_values("date").reset_index(drop=True)

    for row in trades_sorted.itertuples(index=False):
        ticker = row.ticker
        side = row.side
        qty = float(row.qty)
        price = float(row.price_eur)
        fee = float(row.fee_eur)
        # row.date is a pandas Timestamp; convert to plain date
        ts = pd.to_datetime(row.date)
        d = ts.date() if hasattr(ts, "date") else ts

        lot = state.setdefault(ticker, _Lot())

        if side == "BUY":
            if lot.qty < 1e-9:
                # Opening a fresh position
                lot.entry_date = d
                exit_proceeds[ticker] = 0.0
                exit_qty[ticker] = 0.0
            lot.qty += qty
            lot.cost_eur += qty * price
            lot.fees_eur += fee

        elif side == "SELL":
            if lot.qty < 1e-9:
                log.warning("orphan SELL %s on %s — no open position; skipping", ticker, d)
                continue
            lot.qty -= qty
            lot.fees_eur += fee
            exit_proceeds[ticker] = exit_proceeds.get(ticker, 0.0) + qty * price
            exit_qty[ticker] = exit_qty.get(ticker, 0.0) + qty
            last_exit_date[ticker] = d
            lot.last_exit_reason = str(row.signal_reason) if row.signal_reason else ""

            if lot.qty <= 1e-6:  # closed
                total_qty   = exit_qty[ticker]
                avg_entry   = lot.avg_entry_eur if lot.qty > 0 else (lot.cost_eur / total_qty if total_qty else 0.0)
                # When fully closed, all bought qty is now sold — entry avg = total cost / total bought
                bought_qty  = total_qty + lot.qty  # lot.qty is ~0 here
                avg_entry   = lot.cost_eur / bought_qty if bought_qty > 0 else 0.0
                avg_exit    = exit_proceeds[ticker] / total_qty if total_qty > 0 else 0.0
                gross_pl    = total_qty * (avg_exit - avg_entry)
                net_pl      = gross_pl - lot.fees_eur
                ret_pct     = (avg_exit / avg_entry - 1.0) if avg_entry > 0 else 0.0
                hold_days   = (d - (lot.entry_date or d)).days

                closed.append({
                    "ticker":          ticker,
                    "entry_date":      lot.entry_date,
                    "exit_date":       d,
                    "hold_days":       max(hold_days, 1),
                    "entry_price_eur": round(avg_entry, 4),
                    "exit_price_eur":  round(avg_exit, 4),
                    "qty":             round(total_qty, 6),
                    "return_pct":      round(ret_pct, 6),
                    "return_eur":      round(net_pl, 2),
                    "exit_reason":     _classify_exit(lot.last_exit_reason),
                })

                # Reset state for this ticker
                state[ticker] = _Lot()
                exit_proceeds[ticker] = 0.0
                exit_qty[ticker] = 0.0

    return closed


def _classify_exit(reason: str) -> str:
    """Map the verbose ``signal_reason`` text to a short canonical category.

    The strategies emit reasons like
        'TRAIL stop @ 0.86 (peak 1.20)'
        'RSI(14) > 70 — take profit'
        'SMA50 break — 3 closes below'
        'max_days_held=90 — time exit'
        'catastrophic_stop -40%'
    Categorising helps the Strategy Critic aggregate by exit type when it
    proposes parameter changes.
    """
    r = reason.lower()
    if "trail" in r:
        return "trailing_stop"
    if "catastrophic" in r:
        return "catastrophic_stop"
    if "rsi" in r and ("take profit" in r or "> 70" in r or "> 80" in r):
        return "rsi_take_profit"
    if "sma" in r and "break" in r:
        return "sma_break"
    if "max_days" in r or "time exit" in r:
        return "max_days"
    if "rotation" in r:
        return "rotation"
    return "other"


def _enrich_with_metrics_and_regime(
    closed: list[dict],
    universe: list[str],
    market_ticker: str,
) -> None:
    """Add max_unrealized_gain_pct, max_drawdown_pct, regime_at_entry, regime_at_exit.

    Mutates ``closed`` in place. Uses cached yfinance bars for the per-hold
    price series and a single ``compute_regimes()`` call for regime tags.
    """
    if not closed:
        return

    # Bulk fetch bars once (cached) — covers entire window
    min_d = min(c["entry_date"] for c in closed)
    max_d = max(c["exit_date"] for c in closed)

    # prefetch_since(min_days, as_of=...) fetches at least min_days of bars
    # ending at as_of. We need to span from min_d → max_d, so request enough
    # days to cover that window plus a small buffer.
    span_days = (max_d - min_d).days + 30
    bars = market_data.prefetch_since(universe, span_days, as_of=max_d)

    # Regime series (one call, sliced per-position)
    regime_df: pd.DataFrame | None = None
    try:
        regime_df = compute_regimes(market_ticker, min_d, max_d)
        if not regime_df.empty:
            regime_df = regime_df.set_index(pd.to_datetime(regime_df["date"]))
    except Exception as exc:
        log.warning("regime compute failed (%s); regimes will be NULL", exc)
        regime_df = None

    for c in closed:
        # ── Per-hold high-water and worst-drawdown ──────────────────
        # NB: market_data.Bars uses lowercase column names ('close', not 'Close')
        b = bars.get(c["ticker"])
        c["max_unrealized_gain_pct"] = 0.0
        c["max_drawdown_pct"]        = 0.0
        if b is not None and not b.df.empty:
            df = b.df
            try:
                idx_dates = df.index.date if hasattr(df.index, "date") else pd.to_datetime(df.index).date
                mask = (idx_dates >= c["entry_date"]) & (idx_dates <= c["exit_date"])
                window = df.loc[mask, "close"]
                if not window.empty and c["entry_price_eur"] > 0:
                    # window is in native currency; we compare ratios so currency cancels.
                    first = float(window.iloc[0])
                    if first > 0:
                        c["max_unrealized_gain_pct"] = round(float(window.max()) / first - 1.0, 6)
                        c["max_drawdown_pct"]        = round(float(window.min()) / first - 1.0, 6)
            except Exception as exc:
                log.debug("metric calc failed for %s: %s", c["ticker"], exc)

        # ── Regime tags ─────────────────────────────────────────────
        c["regime_at_entry"] = None
        c["regime_at_exit"]  = None
        if regime_df is not None and not regime_df.empty:
            try:
                ent_idx = regime_df.index.searchsorted(pd.Timestamp(c["entry_date"]))
                ext_idx = regime_df.index.searchsorted(pd.Timestamp(c["exit_date"]))
                if 0 <= ent_idx < len(regime_df):
                    c["regime_at_entry"] = str(regime_df.iloc[ent_idx]["regime"])
                if 0 <= ext_idx < len(regime_df):
                    c["regime_at_exit"]  = str(regime_df.iloc[ext_idx]["regime"])
            except Exception as exc:
                log.debug("regime lookup failed for %s: %s", c["ticker"], exc)


def _strategy_universe(strategy_name: str) -> tuple[list[str], str]:
    """Resolve the strategy's universe to a flat ticker list + market filter ticker."""
    params = CONFIG.strategies["strategies"][strategy_name]
    uni_spec = params.get("universe", [])
    tickers: list[str] = []
    if isinstance(uni_spec, str):
        tickers = list(CONFIG.watchlists[uni_spec])
    elif isinstance(uni_spec, list):
        for grp in uni_spec:
            tickers.extend(CONFIG.watchlists[grp])
    market_ticker = params.get("market_filter_ticker", "SXR8.DE")
    if market_ticker not in tickers:
        tickers.append(market_ticker)
    return tickers, market_ticker


def bootstrap_strategy(
    strategy: str,
    start: date,
    end: date,
    replace: bool = False,
) -> int:
    """Backtest one strategy and persist its round-trips. Returns row count inserted."""
    bot_id = STRATEGY_BOTS.get(strategy)
    if bot_id is None:
        log.error("Unknown strategy: %s (known: %s)", strategy, list(STRATEGY_BOTS))
        return 0

    log.info("=== bootstrapping %s (bot %d, %s → %s) ===", strategy, bot_id, start, end)

    # Run the backtest
    result = run_backtest(bot_id, start, end)
    log.info(
        "  backtest: %d trading days, %d trades, %d errors",
        len(result.equity_df), len(result.trades_df), len(result.errors),
    )
    if result.errors:
        for e in result.errors[:3]:
            log.warning("    %s", e)

    # Reconstruct round-trips
    closed = _reconstruct_round_trips(result.trades_df)
    log.info("  round-trips: %d closed positions", len(closed))

    if not closed:
        log.warning("  no closed positions — nothing to insert")
        return 0

    # Enrich with per-hold metrics + regime tags
    universe, market_ticker = _strategy_universe(strategy)
    _enrich_with_metrics_and_regime(closed, universe, market_ticker)

    # Persist
    run_id = f"{strategy}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    inserted = 0
    with get_session() as s:
        if replace:
            n_deleted = (
                s.query(SimulatedClosedPosition)
                .filter(SimulatedClosedPosition.strategy == strategy)
                .delete(synchronize_session=False)
            )
            log.info("  --replace: deleted %d existing row(s) for %s", n_deleted, strategy)

        for c in closed:
            s.add(SimulatedClosedPosition(
                strategy=strategy,
                ticker=c["ticker"],
                entry_date=c["entry_date"],
                exit_date=c["exit_date"],
                hold_days=c["hold_days"],
                entry_price_eur=c["entry_price_eur"],
                exit_price_eur=c["exit_price_eur"],
                qty=c["qty"],
                return_pct=c["return_pct"],
                return_eur=c["return_eur"],
                max_unrealized_gain_pct=c["max_unrealized_gain_pct"],
                max_drawdown_pct=c["max_drawdown_pct"],
                exit_reason=c["exit_reason"],
                regime_at_entry=c.get("regime_at_entry"),
                regime_at_exit=c.get("regime_at_exit"),
                backtest_run_id=run_id,
            ))
            inserted += 1
        s.commit()

    log.info("  inserted %d row(s) (run_id=%s)", inserted, run_id)

    # Quick stats so you can sanity-check the bootstrap
    if inserted:
        wins = sum(1 for c in closed if c["return_pct"] > 0)
        avg_ret = sum(c["return_pct"] for c in closed) / len(closed) * 100
        avg_hold = sum(c["hold_days"] for c in closed) / len(closed)
        log.info(
            "  stats: win_rate=%.1f%% avg_return=%+.2f%% avg_hold=%.1fd",
            wins / len(closed) * 100, avg_ret, avg_hold,
        )

    return inserted


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--strategy", choices=list(STRATEGY_BOTS), help="Bootstrap only this strategy (default: all)")
    parser.add_argument("--start", type=date.fromisoformat, default=date(2024, 1, 1), help="Start date YYYY-MM-DD")
    parser.add_argument("--end", type=date.fromisoformat, default=date.today(), help="End date YYYY-MM-DD")
    parser.add_argument("--replace", action="store_true", help="Delete existing rows for the strategy before inserting")
    args = parser.parse_args()

    strategies = [args.strategy] if args.strategy else list(STRATEGY_BOTS)
    total = 0
    for strat in strategies:
        total += bootstrap_strategy(strat, args.start, args.end, replace=args.replace)
    log.info("=== done: %d total row(s) inserted across %d strategy(ies) ===", total, len(strategies))


if __name__ == "__main__":
    main()
