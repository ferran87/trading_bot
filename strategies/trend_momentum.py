"""Trend Momentum strategy — bull-market and moderate-correction bot.

Thesis
------
The RSI Compounder waits for extreme capitulation (RSI < 25) before entering.
That fires once or twice a year at most. This strategy targets the much more
frequent pattern of *pullbacks within an established uptrend*:

  1. The broad market is healthy (reference index above its 200-day SMA).
  2. An individual stock is in an uptrend (price above its 50-day SMA).
  3. The stock has had a short-term pullback (RSI dipped to 40-58).
  4. Momentum is resuming (RSI today > RSI N days ago).

Entry fires when all four conditions align. No crash required.

Exit (priority order)
---------------------
  1. Catastrophic stop: -``catastrophic_stop``% from avg entry (always active).
  2. Trend break: close drops below SMA50 for ``trend_break_days`` consecutive
     sessions — the uptrend is broken, exit regardless of P&L.
  3. Trailing stop: ``trail_pct``% from peak (fires only when profitable).
  4. Time limit: ``max_days_held`` days if the position never turned profitable.

Parameters (strategies.yaml)
-----------------------------
  market_filter_ticker   : reference index (default SXR8.DE)
  market_sma_period      : SMA period for market filter (default 200)
  sma_period             : per-stock SMA period for trend filter (default 50)
  rsi_period             : RSI period (default 14)
  rsi_entry_min          : minimum RSI at entry — pullback floor (default 40)
  rsi_entry_max          : maximum RSI at entry — not too extended (default 58)
  rsi_momentum_days      : RSI must be higher than N days ago (default 3)
  trail_pct              : trailing stop % from peak, profitable only (default 0.15)
  catastrophic_stop      : hard stop from avg entry, always active (default -0.15)
  trend_break_days       : consecutive closes below SMA50 to trigger exit (default 2)
  max_days_held          : time-based exit if never profitable (default 60)
  max_concurrent         : max open positions (default 10)
  per_position_pct       : position size as fraction of equity (default 0.10)
  min_history_days       : minimum bars required (default 220)
"""
from __future__ import annotations

import logging

import pandas as pd

from analysis.price_signals import rsi
from core.config import CONFIG
from core.types import AssetClass, Order, PortfolioSnapshot, Side
from strategies.base import Strategy, StrategyContext

log = logging.getLogger(__name__)

_CLASS_MAP = {"etf": AssetClass.ETF, "crypto": AssetClass.CRYPTO}


def _asset_class_for(ticker: str) -> AssetClass:
    cls = CONFIG.watchlists.get("venue", {}).get(ticker, {}).get("class", "stock")
    return _CLASS_MAP.get(cls, AssetClass.STOCK)


def _sma(close: pd.Series, period: int) -> float | None:
    if len(close) < period:
        return None
    return float(close.iloc[-period:].mean())


def _consecutive_below_sma50(close: pd.Series, sma50: float) -> int:
    """Count trailing consecutive sessions where close < sma50."""
    count = 0
    for price in reversed(close.tolist()):
        if price < sma50:
            count += 1
        else:
            break
    return count


class TrendMomentumStrategy(Strategy):
    """Bull-market pullback strategy. See module docstring for details."""

    name = "trend_momentum"

    def propose_orders(
        self,
        snapshot: PortfolioSnapshot,
        ctx: StrategyContext,
    ) -> list[Order]:
        params = ctx.params

        rsi_period       = int(params.get("rsi_period", 14))
        rsi_entry_min    = float(params.get("rsi_entry_min", 40))
        rsi_entry_max    = float(params.get("rsi_entry_max", 58))
        rsi_mom_days     = int(params.get("rsi_momentum_days", 3))
        sma_period       = int(params.get("sma_period", 50))
        market_ticker    = params.get("market_filter_ticker", "SXR8.DE")
        market_sma       = int(params.get("market_sma_period", 200))
        trail_pct        = float(params.get("trail_pct", 0.15))
        cat_stop         = float(params.get("catastrophic_stop", -0.15))
        trend_break_days = int(params.get("trend_break_days", 2))
        max_days         = int(params.get("max_days_held", 60))
        max_concurrent   = int(params.get("max_concurrent", 10))
        per_pos_pct      = float(params.get("per_position_pct", 0.10))
        min_history      = int(params.get("min_history_days", 220))

        orders: list[Order] = []
        equity = snapshot.total_eur

        # ── Market filter: reference index must be above its SMA ──────────────
        market_in_uptrend = False
        mkt_bars = ctx.bars.get(market_ticker)
        if mkt_bars is not None and len(mkt_bars.df) >= market_sma:
            mkt_close = mkt_bars.df["close"]
            mkt_sma = _sma(mkt_close, market_sma)
            if mkt_sma is not None:
                market_in_uptrend = float(mkt_close.iloc[-1]) > mkt_sma

        # ── 1. EXIT + TREND-BREAK checks on held positions ────────────────────
        tickers_being_sold: set[str] = set()

        for ticker, pos in snapshot.positions.items():
            bars = ctx.bars.get(ticker)
            price = ctx.prices_eur.get(ticker) or (bars.last_close() if bars is not None else None)
            if price is None:
                continue

            days_held = (ctx.today - pos.entry_date).days
            gain = price / pos.avg_entry_eur - 1.0
            close = bars.df["close"] if bars is not None else None
            exit_reason: str | None = None

            # Priority 1 — catastrophic stop (always active)
            if gain <= cat_stop:
                exit_reason = (
                    f"catastrophic stop {gain*100:.1f}% <= {cat_stop*100:.0f}%"
                )

            # Priority 2 — trend break: N consecutive closes below SMA50
            if exit_reason is None and close is not None:
                sma50 = _sma(close, sma_period)
                if sma50 is not None:
                    below_count = _consecutive_below_sma50(close, sma50)
                    if below_count >= trend_break_days:
                        exit_reason = (
                            f"trend break: {below_count} consecutive closes below "
                            f"SMA{sma_period} (SMA={sma50:.2f}, price={price:.2f}, "
                            f"gain={gain*100:.1f}%)"
                        )

            # Priority 3 — trailing stop (profitable only)
            if exit_reason is None and gain > 0 and close is not None:
                entry_ts = pd.Timestamp(pos.entry_date)
                since_entry = close[close.index >= entry_ts]
                peak_native = float(since_entry.max()) if not since_entry.empty else float(close.iloc[-1])
                price_native = float(close.iloc[-1])
                drawdown = price_native / peak_native - 1.0
                if drawdown <= -trail_pct:
                    fx_ratio = price / price_native if price_native else 1.0
                    peak_eur = peak_native * fx_ratio
                    exit_reason = (
                        f"trailing stop {drawdown*100:.1f}% from peak "
                        f"(trail={trail_pct*100:.0f}%, "
                        f"peak=EUR{peak_eur:.2f}, gain={gain*100:.1f}%)"
                    )

            # Priority 4 — time limit (never profitable)
            if exit_reason is None and gain <= 0 and days_held >= max_days:
                exit_reason = (
                    f"time limit {days_held}d, never profitable (gain={gain*100:.1f}%)"
                )

            if exit_reason:
                log.info(
                    "trend_momentum bot=%d SELL %s: %s (entry=%.2f days=%d)",
                    ctx.bot_id, ticker, exit_reason, pos.avg_entry_eur, days_held,
                )
                orders.append(Order(
                    bot_id=ctx.bot_id, ticker=ticker, side=Side.SELL,
                    qty=pos.qty, ref_price_eur=price,
                    signal_reason=f"trend_momentum: {exit_reason}",
                    asset_class=_asset_class_for(ticker),
                ))
                tickers_being_sold.add(ticker)

        # ── 2. ENTRY checks — only when market is in uptrend ─────────────────
        if not market_in_uptrend:
            log.info(
                "trend_momentum bot=%d: market filter FAIL (%s not above SMA%d) — no entries",
                ctx.bot_id, market_ticker, market_sma,
            )
            return orders

        positions_after_exits = len(snapshot.positions) - len(tickers_being_sold)
        slots_available = max_concurrent - positions_after_exits

        if slots_available <= 0:
            return orders

        candidates: list[tuple[float, str, float, float]] = []  # (rsi_val, ticker, price, qty)

        for ticker, bars in ctx.bars.items():
            if ticker == market_ticker:
                continue
            if ticker in snapshot.positions and ticker not in tickers_being_sold:
                continue
            if len(bars.df) < min_history:
                continue

            price = ctx.prices_eur.get(ticker) or bars.last_close()
            if price <= 0:
                continue

            close = bars.df["close"]

            # Condition A: stock above its SMA50
            sma50 = _sma(close, sma_period)
            if sma50 is None or float(close.iloc[-1]) <= sma50:
                continue

            # Condition B: RSI in pullback zone
            if len(close) < rsi_period + rsi_mom_days + 2:
                continue
            rsi_series = rsi(close, rsi_period)
            rsi_now = float(rsi_series.iloc[-1])
            if rsi_now != rsi_now:  # NaN guard
                continue
            if not (rsi_entry_min <= rsi_now <= rsi_entry_max):
                continue

            # Condition C: RSI momentum — rising vs N days ago
            rsi_n_ago = float(rsi_series.iloc[-rsi_mom_days - 1])
            if rsi_n_ago != rsi_n_ago or rsi_now <= rsi_n_ago:
                continue

            qty = round(equity * per_pos_pct / price, 4)
            if qty <= 0:
                continue

            candidates.append((rsi_now, ticker, price, qty))

        # Sort by RSI ascending — prefer stocks with the deepest (but recovering) pullback
        candidates.sort(key=lambda x: x[0])

        for rsi_val, ticker, price, qty in candidates:
            if slots_available <= 0:
                break
            rsi_series = rsi(ctx.bars[ticker].df["close"], rsi_period)
            rsi_n_ago = float(rsi_series.iloc[-rsi_mom_days - 1])
            log.info(
                "trend_momentum bot=%d BUY %s: RSI=%.1f (was %.1f, %dd ago) "
                "above SMA%d, mkt above SMA%d (EUR%.0f qty=%.4f @ EUR%.2f)",
                ctx.bot_id, ticker, rsi_val, rsi_n_ago, rsi_mom_days,
                sma_period, market_sma, equity * per_pos_pct, qty, price,
            )
            orders.append(Order(
                bot_id=ctx.bot_id, ticker=ticker, side=Side.BUY,
                qty=float(qty), ref_price_eur=price,
                signal_reason=(
                    f"trend_momentum: RSI={rsi_val:.0f} (was {rsi_n_ago:.0f} "
                    f"{rsi_mom_days}d ago), above SMA{sma_period}, mkt above SMA{market_sma}"
                ),
                asset_class=_asset_class_for(ticker),
            ))
            slots_available -= 1

        return orders
