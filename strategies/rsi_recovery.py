"""Bot 4 — RSI Recovery.

Entry logic (derived from optimizer scan over Feb-Apr 2026):
  Enter when a stock has bounced off an oversold extreme — meaning RSI(14)
  was below 35 at least 5 trading days ago but has now recovered above 40.
  This fires AFTER the bottom is confirmed, not at the dip, which is why it
  outperformed simple dip-buying in the tariff-selloff period.

Exit logic:
  SHORT mode (first 7 days): 15% trailing stop from peak — quick safety net.
  GRADUATED mode (profitable after day 7): 30% trailing stop — lets multi-month
  recovery runs develop without being stopped out by normal consolidation.
  Hard stop at -8% from avg entry applies in both modes.

Sizing:
  Up to max_concurrent (10) positions at per_position_pct (10%) each.
  10 × 10% = 100% of equity. Hard cap via risk.py (position_cap_stock: 0.20).
  Pyramid adds (up to max_adds_per_ticker) allowed for the same entry signal
  if position is already profitable — same guard as sharp_dip.

Universe: stocks_us + stocks_eu + etfs_ucits (all 42 tickers).
No fixed hold period — trailing stop and hard stop do the work.
"""
from __future__ import annotations

import logging

import pandas as pd

from analysis.price_signals import above_sma, rsi, total_return
from core.config import CONFIG
from core.types import AssetClass, Order, PortfolioSnapshot, Side
from strategies.base import Strategy, StrategyContext

log = logging.getLogger(__name__)

_CLASS_MAP = {"etf": AssetClass.ETF, "crypto": AssetClass.CRYPTO}


def _asset_class_for(ticker: str) -> AssetClass:
    cls = CONFIG.watchlists.get("venue", {}).get(ticker, {}).get("class", "stock")
    return _CLASS_MAP.get(cls, AssetClass.STOCK)


def _rsi_min_recent(close: pd.Series, rsi_period: int, lookback: int) -> float:
    """Minimum RSI value over the `lookback` bars ending one bar before the last.

    Checks a rolling window rather than one fixed point so crash bottoms are
    caught regardless of exact timing within the lookback window.
    """
    if len(close) < rsi_period + lookback + 1:
        return float("nan")
    rsi_series = rsi(close.iloc[:-1], rsi_period)   # exclude today
    window = rsi_series.iloc[-lookback:]
    if window.empty:
        return float("nan")
    return float(window.min())


class RsiRecoveryStrategy(Strategy):
    name = "rsi_recovery"

    def propose_orders(
        self,
        snapshot: PortfolioSnapshot,
        ctx: StrategyContext,
    ) -> list[Order]:
        params = ctx.params
        rsi_period       = int(params.get("rsi_period", 14))
        rsi_was_below    = float(params.get("rsi_was_below", 35))   # oversold threshold
        rsi_now_above    = float(params.get("rsi_now_above", 40))   # recovery confirmation
        rsi_entry_max    = float(params.get("rsi_entry_max", 100))  # block late entries
        lookback_days    = int(params.get("rsi_lookback_days", 5))  # how many days to look back
        trail_pct        = float(params.get("trail_pct", 0.15))
        long_trail_pct   = float(params.get("long_trail_pct", 0.30))
        grad_min_days    = int(params.get("graduate_min_days", 7))
        stop_loss        = float(params.get("stop_loss_pct", 0.08))
        max_concurrent   = int(params["max_concurrent"])
        per_pos_pct      = float(params["per_position_pct"])
        min_history      = int(params.get("min_history_days", 60))
        max_adds         = int(params.get("max_adds_per_ticker", 1))

        mkt_ticker           = params.get("market_filter_ticker")
        mkt_rsi_below        = float(params.get("market_rsi_was_below", 35))
        mkt_rsi_lookback     = int(params.get("market_rsi_lookback_days", lookback_days))

        orders: list[Order] = []
        equity = snapshot.total_eur

        # Pre-compute market co-recovery flag once per cycle.
        market_was_oversold = True  # default open if no filter configured
        if mkt_ticker and mkt_ticker in ctx.bars:
            mkt_close = ctx.bars[mkt_ticker].df["close"]
            mkt_rsi_min = _rsi_min_recent(mkt_close, rsi_period, mkt_rsi_lookback)
            market_was_oversold = (
                not (mkt_rsi_min != mkt_rsi_min) and mkt_rsi_min < mkt_rsi_below
            )

        # --- 1. EXIT checks ---
        for ticker, pos in snapshot.positions.items():
            bars = ctx.bars.get(ticker)
            price = ctx.prices_eur.get(ticker) or (bars.last_close() if bars is not None else None)
            days_held = (ctx.today - pos.entry_date).days
            exit_reason: str | None = None

            if price is None:
                pass  # no data — keep holding (no forced exit by time)
            else:
                gain = price / pos.avg_entry_eur - 1.0

                # Hard stop (EUR gain vs EUR avg entry)
                if gain <= -stop_loss:
                    exit_reason = f"stop loss {gain*100:.1f}% <= -{stop_loss*100:.0f}%"
                else:
                    # Trailing stop: use native-currency (USD for US stocks) prices for
                    # the peak so we don't mix EUR current price with a USD peak.
                    price_native = bars.last_close() if bars is not None else price
                    if bars is not None and len(bars.df) > 0:
                        entry_ts = pd.Timestamp(pos.entry_date)
                        since_entry = bars.df["close"][bars.df.index >= entry_ts]
                        peak_native = float(since_entry.max()) if not since_entry.empty else price_native
                    else:
                        peak_native = price_native

                    # Graduation: once profitable after grad_min_days, switch to wider trail.
                    graduated = days_held >= grad_min_days and gain > 0
                    active_trail = long_trail_pct if graduated else trail_pct

                    drawdown = price_native / peak_native - 1.0
                    if drawdown <= -active_trail:
                        mode = "graduated" if graduated else "short"
                        # Convert native peak to EUR for a consistent signal reason.
                        fx_ratio = price / price_native if price_native else 1.0
                        peak_eur = peak_native * fx_ratio
                        exit_reason = (
                            f"{mode} trailing stop {drawdown*100:.1f}% from peak "
                            f"(peak=EUR{peak_eur:.2f}, now=EUR{price:.2f}, gain={gain*100:.1f}%)"
                        )

            if exit_reason:
                ref = price if price and price > 0 else pos.last_price_eur
                log.info(
                    "rsi_recovery bot=%d SELL %s: %s (entry=%.2f days=%d)",
                    ctx.bot_id, ticker, exit_reason, pos.avg_entry_eur, days_held,
                )
                orders.append(
                    Order(
                        bot_id=ctx.bot_id,
                        ticker=ticker,
                        side=Side.SELL,
                        qty=pos.qty,
                        ref_price_eur=ref,
                        signal_reason=f"rsi_recovery: {exit_reason}",
                        asset_class=_asset_class_for(ticker),
                    )
                )

        # --- 2. ENTRY checks ---
        tickers_being_sold = {o.ticker for o in orders if o.side is Side.SELL}
        positions_after_exits = len(snapshot.positions) - len(tickers_being_sold)
        slots_available = max_concurrent - positions_after_exits

        if slots_available <= 0:
            return orders

        for ticker, bars in ctx.bars.items():
            if slots_available <= 0:
                break
            if len(bars.df) < min_history:
                continue

            price = ctx.prices_eur.get(ticker) or bars.last_close()
            if price <= 0:
                continue

            close = bars.df["close"]
            # Market co-crash required — blocks individual-stock false bounces when
            # the broad market never became oversold.
            if not market_was_oversold:
                continue

            already_held = ticker in snapshot.positions and ticker not in tickers_being_sold

            if already_held:
                pos = snapshot.positions[ticker]
                # Only pyramid if currently profitable
                if price / pos.avg_entry_eur - 1.0 <= 0:
                    continue
                prior_buys = ctx.buys_per_ticker.get(ticker, 0)
                if prior_buys > max_adds:
                    continue

            # RSI recovery signal: min RSI over the last `lookback_days` was below the
            # oversold threshold AND today's RSI has recovered above the confirmation level.
            rsi_now = float(rsi(close, rsi_period).iloc[-1]) if len(close) >= rsi_period + 1 else float("nan")
            if rsi_now != rsi_now or rsi_now <= rsi_now_above or rsi_now >= rsi_entry_max:
                continue

            rsi_min = _rsi_min_recent(close, rsi_period, lookback_days)
            if rsi_min != rsi_min or rsi_min >= rsi_was_below:
                continue

            # Size each entry equally across remaining slots, capped at per_pos_pct (20%).
            dynamic_pct = min(1.0 / slots_available, per_pos_pct)
            target_value = equity * dynamic_pct
            qty = round(target_value / price, 4)
            if qty <= 0:
                continue

            label = "pyramid add" if already_held else "entry"
            log.info(
                "rsi_recovery bot=%d BUY %s (%s): RSI now=%.1f min_recent=%.1f "
                "(target=EUR%.0f qty=%.4f @ EUR%.2f)",
                ctx.bot_id, ticker, label, rsi_now, rsi_min,
                target_value, qty, price,
            )
            orders.append(
                Order(
                    bot_id=ctx.bot_id,
                    ticker=ticker,
                    side=Side.BUY,
                    qty=float(qty),
                    ref_price_eur=price,
                    signal_reason=(
                        f"rsi_recovery: {label} RSI min={rsi_min:.0f} now={rsi_now:.0f}"
                    ),
                    asset_class=_asset_class_for(ticker),
                )
            )
            slots_available -= 1

        return orders
