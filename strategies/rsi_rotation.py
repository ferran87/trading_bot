"""Bot 5 — RSI Rotation.

Same entry/exit logic as Bot 4 (RSI Recovery) with one addition: when all
slots are full and a new RSI recovery signal fires, the bot rotates — it sells
the weakest holding (lowest current EUR gain) and buys the new candidate,
provided the weakest position's gain is below `rotation_loss_threshold`.

Rotation rules:
  - Only eject a position that is losing (gain < rotation_loss_threshold, default -3%).
    Profitable positions are never disturbed by rotation.
  - At most one rotation per daily cycle to avoid thrashing.
  - The incoming candidate must pass the full RSI recovery signal check.
  - If multiple candidates qualify, the one with the lowest rsi_min (deepest
    oversold = strongest signal) gets the slot first.

Everything else (exit stops, pyramid adds, sizing) is identical to Bot 4.
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


def _rsi_min_recent(close: pd.Series, rsi_period: int, lookback: int) -> float:
    if len(close) < rsi_period + lookback + 1:
        return float("nan")
    rsi_series = rsi(close.iloc[:-1], rsi_period)
    window = rsi_series.iloc[-lookback:]
    if window.empty:
        return float("nan")
    return float(window.min())


class RsiRotationStrategy(Strategy):
    name = "rsi_rotation"

    def propose_orders(
        self,
        snapshot: PortfolioSnapshot,
        ctx: StrategyContext,
    ) -> list[Order]:
        params = ctx.params
        rsi_period         = int(params.get("rsi_period", 14))
        rsi_was_below      = float(params.get("rsi_was_below", 25))
        rsi_now_above      = float(params.get("rsi_now_above", 40))
        rsi_entry_max      = float(params.get("rsi_entry_max", 100))  # block late entries
        lookback_days      = int(params.get("rsi_lookback_days", 15))
        trail_pct          = float(params.get("trail_pct", 0.15))
        long_trail_pct     = float(params.get("long_trail_pct", 0.30))
        grad_min_days      = int(params.get("graduate_min_days", 7))
        stop_loss          = float(params.get("stop_loss_pct", 0.08))
        max_concurrent     = int(params["max_concurrent"])
        per_pos_pct        = float(params["per_position_pct"])
        min_history        = int(params.get("min_history_days", 60))
        max_adds           = int(params.get("max_adds_per_ticker", 1))
        rotation_threshold = float(params.get("rotation_loss_threshold", -0.03))

        mkt_ticker       = params.get("market_filter_ticker")
        mkt_rsi_below    = float(params.get("market_rsi_was_below", 30))
        mkt_rsi_lookback = int(params.get("market_rsi_lookback_days", lookback_days))

        orders: list[Order] = []
        equity = snapshot.total_eur

        # Pre-compute market co-recovery flag.
        market_was_oversold = True
        if mkt_ticker and mkt_ticker in ctx.bars:
            mkt_close = ctx.bars[mkt_ticker].df["close"]
            mkt_rsi_min = _rsi_min_recent(mkt_close, rsi_period, mkt_rsi_lookback)
            market_was_oversold = (
                not (mkt_rsi_min != mkt_rsi_min) and mkt_rsi_min < mkt_rsi_below
            )

        # --- 1. EXIT checks (hard stop + graduated trailing stop) ---
        for ticker, pos in snapshot.positions.items():
            bars = ctx.bars.get(ticker)
            price = ctx.prices_eur.get(ticker) or (bars.last_close() if bars is not None else None)
            days_held = (ctx.today - pos.entry_date).days
            exit_reason: str | None = None

            if price is not None:
                gain = price / pos.avg_entry_eur - 1.0

                if gain <= -stop_loss:
                    exit_reason = f"stop loss {gain*100:.1f}% <= -{stop_loss*100:.0f}%"
                else:
                    price_native = bars.last_close() if bars is not None else price
                    if bars is not None and len(bars.df) > 0:
                        entry_ts = pd.Timestamp(pos.entry_date)
                        since_entry = bars.df["close"][bars.df.index >= entry_ts]
                        peak_native = float(since_entry.max()) if not since_entry.empty else price_native
                    else:
                        peak_native = price_native

                    graduated = days_held >= grad_min_days and gain > 0
                    active_trail = long_trail_pct if graduated else trail_pct
                    drawdown = price_native / peak_native - 1.0

                    if drawdown <= -active_trail:
                        mode = "graduated" if graduated else "short"
                        fx_ratio = price / price_native if price_native else 1.0
                        peak_eur = peak_native * fx_ratio
                        exit_reason = (
                            f"{mode} trailing stop {drawdown*100:.1f}% from peak "
                            f"(peak=EUR{peak_eur:.2f}, now=EUR{price:.2f}, "
                            f"gain={gain*100:.1f}%)"
                        )

            if exit_reason:
                ref = price if price and price > 0 else pos.last_price_eur
                log.info(
                    "rsi_rotation bot=%d SELL %s: %s (entry=%.2f days=%d)",
                    ctx.bot_id, ticker, exit_reason, pos.avg_entry_eur, days_held,
                )
                orders.append(
                    Order(
                        bot_id=ctx.bot_id,
                        ticker=ticker,
                        side=Side.SELL,
                        qty=pos.qty,
                        ref_price_eur=ref,
                        signal_reason=f"rsi_rotation: {exit_reason}",
                        asset_class=_asset_class_for(ticker),
                    )
                )

        # --- 2. ENTRY + ROTATION ---
        tickers_being_sold = {o.ticker for o in orders if o.side is Side.SELL}
        positions_after_exits = len(snapshot.positions) - len(tickers_being_sold)
        slots_available = max_concurrent - positions_after_exits

        # Scan all tickers for RSI recovery signal; rank by rsi_min ascending
        # (deepest oversold = strongest signal gets priority when rotating).
        candidates: list[tuple[float, float, str, float]] = []  # (rsi_min, rsi_now, ticker, price)
        for ticker, bars in ctx.bars.items():
            if len(bars.df) < min_history:
                continue
            price = ctx.prices_eur.get(ticker) or bars.last_close()
            if price <= 0:
                continue
            if not market_was_oversold:
                continue
            close = bars.df["close"]
            rsi_now = float(rsi(close, rsi_period).iloc[-1]) if len(close) >= rsi_period + 1 else float("nan")
            if rsi_now != rsi_now or rsi_now <= rsi_now_above or rsi_now >= rsi_entry_max:
                continue
            rsi_min = _rsi_min_recent(close, rsi_period, lookback_days)
            if rsi_min != rsi_min or rsi_min >= rsi_was_below:
                continue
            candidates.append((rsi_min, rsi_now, ticker, price))

        candidates.sort(key=lambda x: x[0])  # lowest rsi_min first = strongest signal

        rotated_this_cycle = False

        for rsi_min, rsi_now, ticker, price in candidates:
            already_held = ticker in snapshot.positions and ticker not in tickers_being_sold

            if already_held:
                pos = snapshot.positions[ticker]
                if price / pos.avg_entry_eur - 1.0 <= 0:
                    continue
                if ctx.buys_per_ticker.get(ticker, 0) > max_adds:
                    continue

            if slots_available > 0:
                # Normal entry.
                dynamic_pct = min(1.0 / slots_available, per_pos_pct)
                qty = round(equity * dynamic_pct / price, 4)
                if qty <= 0:
                    continue
                label = "pyramid add" if already_held else "entry"
                log.info(
                    "rsi_rotation bot=%d BUY %s (%s): RSI min=%.1f now=%.1f "
                    "(EUR%.0f qty=%.4f @ EUR%.2f)",
                    ctx.bot_id, ticker, label, rsi_min, rsi_now,
                    equity * dynamic_pct, qty, price,
                )
                orders.append(Order(
                    bot_id=ctx.bot_id, ticker=ticker, side=Side.BUY,
                    qty=float(qty), ref_price_eur=price,
                    signal_reason=f"rsi_rotation: {label} RSI min={rsi_min:.0f} now={rsi_now:.0f}",
                    asset_class=_asset_class_for(ticker),
                ))
                slots_available -= 1

            elif not already_held and not rotated_this_cycle:
                # Portfolio full — try to rotate out the weakest loser.
                held = {
                    t: p for t, p in snapshot.positions.items()
                    if t not in tickers_being_sold
                }
                if not held:
                    continue

                def _current_gain(t: str, p) -> float:
                    ep = ctx.prices_eur.get(t) or (
                        ctx.bars[t].last_close() if t in ctx.bars else p.avg_entry_eur
                    )
                    return ep / p.avg_entry_eur - 1.0

                weakest_ticker = min(held, key=lambda t: _current_gain(t, held[t]))
                weakest_gain = _current_gain(weakest_ticker, held[weakest_ticker])

                if weakest_gain >= rotation_threshold:
                    log.debug(
                        "rsi_rotation bot=%d: %s qualifies but weakest %s gain=%.1f%% "
                        "above threshold %.1f%% — no rotation",
                        ctx.bot_id, ticker, weakest_ticker,
                        weakest_gain * 100, rotation_threshold * 100,
                    )
                    continue

                weakest_pos = held[weakest_ticker]
                weakest_price = ctx.prices_eur.get(weakest_ticker) or (
                    ctx.bars[weakest_ticker].last_close()
                    if weakest_ticker in ctx.bars else weakest_pos.avg_entry_eur
                )

                log.info(
                    "rsi_rotation bot=%d ROTATE: sell %s (gain=%.1f%%) → buy %s "
                    "(RSI min=%.1f now=%.1f)",
                    ctx.bot_id, weakest_ticker, weakest_gain * 100, ticker, rsi_min, rsi_now,
                )

                orders.append(Order(
                    bot_id=ctx.bot_id, ticker=weakest_ticker, side=Side.SELL,
                    qty=weakest_pos.qty, ref_price_eur=weakest_price,
                    signal_reason=(
                        f"rsi_rotation: rotated out (gain={weakest_gain*100:.1f}%) for {ticker}"
                    ),
                    asset_class=_asset_class_for(weakest_ticker),
                ))
                tickers_being_sold.add(weakest_ticker)

                qty = round(equity * per_pos_pct / price, 4)
                if qty > 0:
                    orders.append(Order(
                        bot_id=ctx.bot_id, ticker=ticker, side=Side.BUY,
                        qty=float(qty), ref_price_eur=price,
                        signal_reason=(
                            f"rsi_rotation: rotation entry RSI min={rsi_min:.0f} now={rsi_now:.0f}"
                        ),
                        asset_class=_asset_class_for(ticker),
                    ))

                rotated_this_cycle = True

        return orders
