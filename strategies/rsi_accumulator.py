"""Bot 6 — RSI Accumulator.

Thesis: if a stock becomes deeply oversold and you believe it will recover,
a further drop is a better buying opportunity, not a reason to sell.

Entry: same RSI recovery signal as Bots 4/5 (individual RSI was below
rsi_was_below in last rsi_lookback_days, now above rsi_now_above, market
co-crash confirmed).

Adds (averaging down — never averaging up):
  Add 1: price drops add_at_loss_1 (default -8%) from avg entry.
  Add 2: price drops add_at_loss_2 (default -15%) from avg entry.
  Each add is the same size as the initial lot. Max 2 adds = 3 lots total,
  capped at 20% of portfolio by risk.py.

Exit — four conditions, in priority order:
  1. Catastrophic stop:  -40% from avg entry (absolute floor, fires always).
  2. Take profit (RSI):  RSI(14) > rsi_take_profit (default 70) — stock fully
                         recovered, lock in gains.
  3. Trailing stop:      35% from peak close — ONLY fires when position is
                         currently profitable (gain > 0). Lets winners run.
  4. Time limit:         max_days_held (default 90) if position never became
                         profitable — prevents being stuck forever in a loser.

Sizing: per_position_pct (default 6.7%) per lot × 3 lots ≈ 20% max exposure.
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


class RsiAccumulatorStrategy(Strategy):
    name = "rsi_accumulator"

    def propose_orders(
        self,
        snapshot: PortfolioSnapshot,
        ctx: StrategyContext,
    ) -> list[Order]:
        params = ctx.params
        rsi_period        = int(params.get("rsi_period", 14))
        rsi_was_below     = float(params.get("rsi_was_below", 25))
        rsi_now_above     = float(params.get("rsi_now_above", 40))
        rsi_entry_max     = float(params.get("rsi_entry_max", 65))  # don't enter overbought stocks
        lookback_days     = int(params.get("rsi_lookback_days", 15))
        per_pos_pct       = float(params.get("per_position_pct", 0.067))
        max_concurrent    = int(params.get("max_concurrent", 10))
        min_history       = int(params.get("min_history_days", 60))
        add_at_loss_1     = float(params.get("add_at_loss_1", -0.08))
        add_at_loss_2     = float(params.get("add_at_loss_2", -0.15))
        max_adds          = int(params.get("max_adds_per_ticker", 2))
        trail_pct         = float(params.get("trail_pct", 0.35))
        rsi_tp            = float(params.get("rsi_take_profit", 70))
        max_days          = int(params.get("max_days_held", 90))
        catastrophic_stop = float(params.get("catastrophic_stop", -0.40))

        mkt_ticker        = params.get("market_filter_ticker")
        mkt_rsi_below     = float(params.get("market_rsi_was_below", 30))
        mkt_rsi_lookback  = int(params.get("market_rsi_lookback_days", lookback_days))

        orders: list[Order] = []
        equity = snapshot.total_eur

        # Market co-crash flag.
        market_was_oversold = True
        if mkt_ticker and mkt_ticker in ctx.bars:
            mkt_close = ctx.bars[mkt_ticker].df["close"]
            mkt_rsi_min = _rsi_min_recent(mkt_close, rsi_period, mkt_rsi_lookback)
            market_was_oversold = (
                not (mkt_rsi_min != mkt_rsi_min) and mkt_rsi_min < mkt_rsi_below
            )

        # --- 1. EXIT + ADD checks on held positions ---
        adds_this_cycle: set[str] = set()

        for ticker, pos in snapshot.positions.items():
            bars = ctx.bars.get(ticker)
            price = ctx.prices_eur.get(ticker) or (bars.last_close() if bars is not None else None)
            if price is None:
                continue

            days_held = (ctx.today - pos.entry_date).days
            gain = price / pos.avg_entry_eur - 1.0
            close = bars.df["close"] if bars is not None else None

            exit_reason: str | None = None
            add_reason: str | None = None

            # Priority 1 — catastrophic stop (always active).
            if gain <= catastrophic_stop:
                exit_reason = (
                    f"catastrophic stop {gain*100:.1f}% <= {catastrophic_stop*100:.0f}%"
                )

            # Priority 2 — RSI take-profit (stock fully recovered).
            elif close is not None and len(close) >= rsi_period + 1:
                rsi_now = float(rsi(close, rsi_period).iloc[-1])
                if rsi_now == rsi_now and rsi_now >= rsi_tp:
                    exit_reason = f"RSI take-profit {rsi_now:.1f} >= {rsi_tp:.0f}"

            # Priority 3 — trailing stop (only when profitable).
            if exit_reason is None and gain > 0 and bars is not None and len(bars.df) > 0:
                price_native = bars.last_close()
                entry_ts = pd.Timestamp(pos.entry_date)
                since_entry = bars.df["close"][bars.df.index >= entry_ts]
                peak_native = float(since_entry.max()) if not since_entry.empty else price_native
                drawdown = price_native / peak_native - 1.0
                if drawdown <= -trail_pct:
                    fx_ratio = price / price_native if price_native else 1.0
                    peak_eur = peak_native * fx_ratio
                    exit_reason = (
                        f"trailing stop {drawdown*100:.1f}% from peak "
                        f"(peak=EUR{peak_eur:.2f}, now=EUR{price:.2f})"
                    )

            # Priority 4 — time limit (only if never in profit).
            if exit_reason is None and gain <= 0 and days_held >= max_days:
                exit_reason = f"time limit {days_held}d, never profitable (gain={gain*100:.1f}%)"

            if exit_reason:
                log.info(
                    "rsi_accumulator bot=%d SELL %s: %s (entry=%.2f days=%d)",
                    ctx.bot_id, ticker, exit_reason, pos.avg_entry_eur, days_held,
                )
                orders.append(Order(
                    bot_id=ctx.bot_id, ticker=ticker, side=Side.SELL,
                    qty=pos.qty, ref_price_eur=price,
                    signal_reason=f"rsi_accumulator: {exit_reason}",
                    asset_class=_asset_class_for(ticker),
                ))
                continue  # no add if we're exiting

            # Add checks — only if not exiting and adds remaining.
            prior_buys = ctx.buys_per_ticker.get(ticker, 0)
            adds_done = prior_buys - 1  # subtract the initial entry
            adds_pending = len([t for t in adds_this_cycle if t == ticker])
            total_adds = adds_done + adds_pending

            if total_adds < max_adds:
                if gain <= add_at_loss_2 and total_adds < max_adds:
                    add_reason = f"add {total_adds+1} at loss {gain*100:.1f}% <= {add_at_loss_2*100:.0f}%"
                elif gain <= add_at_loss_1 and total_adds < 1:
                    add_reason = f"add 1 at loss {gain*100:.1f}% <= {add_at_loss_1*100:.0f}%"

            if add_reason:
                qty = round(equity * per_pos_pct / price, 4)
                if qty > 0:
                    log.info(
                        "rsi_accumulator bot=%d ADD %s: %s (avg_entry=%.2f qty=%.4f)",
                        ctx.bot_id, ticker, add_reason, pos.avg_entry_eur, qty,
                    )
                    orders.append(Order(
                        bot_id=ctx.bot_id, ticker=ticker, side=Side.BUY,
                        qty=float(qty), ref_price_eur=price,
                        signal_reason=f"rsi_accumulator: {add_reason}",
                        asset_class=_asset_class_for(ticker),
                    ))
                    adds_this_cycle.add(ticker)

        # --- 2. ENTRY checks for new positions ---
        tickers_being_sold = {o.ticker for o in orders if o.side is Side.SELL}
        positions_after_exits = len(snapshot.positions) - len(tickers_being_sold)
        slots_available = max_concurrent - positions_after_exits

        if slots_available <= 0:
            return orders

        for ticker, bars in ctx.bars.items():
            if slots_available <= 0:
                break
            if ticker in snapshot.positions and ticker not in tickers_being_sold:
                continue  # already held — adds handled above
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

            dynamic_pct = min(1.0 / slots_available, per_pos_pct)
            qty = round(equity * dynamic_pct / price, 4)
            if qty <= 0:
                continue

            log.info(
                "rsi_accumulator bot=%d BUY %s entry: RSI min=%.1f now=%.1f "
                "(EUR%.0f qty=%.4f @ EUR%.2f)",
                ctx.bot_id, ticker, rsi_min, rsi_now,
                equity * dynamic_pct, qty, price,
            )
            orders.append(Order(
                bot_id=ctx.bot_id, ticker=ticker, side=Side.BUY,
                qty=float(qty), ref_price_eur=price,
                signal_reason=f"rsi_accumulator: entry RSI min={rsi_min:.0f} now={rsi_now:.0f}",
                asset_class=_asset_class_for(ticker),
            ))
            slots_available -= 1

        return orders
