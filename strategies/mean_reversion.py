"""Bot 2 — Moderate: Mean Reversion on Stocks.

Rules (from PROJECT_PLAN.md):

* Universe: stocks_us + stocks_eu (see watchlists.yaml).
* Entry : RSI(14) < rsi_entry_below (30) at close → BUY at next open.
* Exits (first to fire):
    - RSI(14) > rsi_exit_above (55)
    - Price >= avg_entry * (1 + profit_target_pct)   [default +5%]
    - Price <= avg_entry * (1 - stop_loss_pct)        [default -7%]
    - Days held >= max_days_held                       [default 10]
* Sizing : up to max_concurrent (5) positions at per_position_pct (20%) each.
* Cadence: evaluated every weekday (no Monday gate).

Implementation notes:
* Exit orders are proposed before entry orders so the executor's SELL-first
  sequencing frees cash before new BUYs are sized.
* expected_profit_eur is set on BUY orders so risk.py's fee-aware skip fires
  correctly (skip if round-trip fee > 25% of expected profit).
* entry_date in PositionView provides the days-held counter without DB access.
"""
from __future__ import annotations

import logging

from analysis.price_signals import above_sma, rsi
from core.types import AssetClass, Order, PortfolioSnapshot, Side
from strategies.base import Strategy, StrategyContext

log = logging.getLogger(__name__)


class MeanReversionStrategy(Strategy):
    name = "mean_reversion"

    def propose_orders(
        self,
        snapshot: PortfolioSnapshot,
        ctx: StrategyContext,
    ) -> list[Order]:
        params = ctx.params
        rsi_period          = int(params.get("rsi_period", 14))
        entry_below         = float(params["rsi_entry_below"])
        exit_above          = float(params["rsi_exit_above"])
        profit_target       = float(params["profit_target_pct"])
        stop_loss           = float(params["stop_loss_pct"])
        max_days            = int(params["max_days_held"])
        max_concurrent      = int(params["max_concurrent"])
        per_pos_pct         = float(params["per_position_pct"])
        min_history         = int(params.get("min_history_days", rsi_period * 3))
        trend_sma           = int(params.get("trend_sma_days", 0))  # 0 = disabled
        mkt_ticker          = params.get("market_filter_ticker")
        mkt_sma             = int(params.get("market_filter_sma", 200))

        orders: list[Order] = []
        equity = snapshot.total_eur

        # --- 1. EXIT checks on all currently held positions ---
        for ticker, pos in snapshot.positions.items():
            bars = ctx.bars.get(ticker)
            price = ctx.prices_eur.get(ticker) or (bars.last_close() if bars is not None else None)

            days_held = (ctx.today - pos.entry_date).days

            exit_reason: str | None = None

            if price is None or bars is None or len(bars.df) < rsi_period + 1:
                # No data → fall back to time-based exit only.
                if days_held >= max_days:
                    exit_reason = f"max days held ({days_held}d, no price data)"
            else:
                rsi_series = rsi(bars.df["close"], rsi_period)
                current_rsi = float(rsi_series.iloc[-1])

                if current_rsi > exit_above:
                    exit_reason = (
                        f"RSI {current_rsi:.1f} > {exit_above} (recovery)"
                    )
                elif price >= pos.avg_entry_eur * (1.0 + profit_target):
                    gain_pct = price / pos.avg_entry_eur - 1.0
                    exit_reason = (
                        f"profit target {gain_pct*100:.1f}% >= {profit_target*100:.0f}%"
                    )
                elif price <= pos.avg_entry_eur * (1.0 - stop_loss):
                    loss_pct = 1.0 - price / pos.avg_entry_eur
                    exit_reason = (
                        f"stop loss {loss_pct*100:.1f}% >= {stop_loss*100:.0f}%"
                    )
                elif days_held >= max_days:
                    exit_reason = f"max days held ({days_held}d)"

            if exit_reason:
                ref = price if price and price > 0 else pos.last_price_eur
                log.info(
                    "mean_reversion bot=%d SELL %s: %s "
                    "(entry=%.2f ref=%.2f days=%d)",
                    ctx.bot_id, ticker, exit_reason,
                    pos.avg_entry_eur, ref, days_held,
                )
                orders.append(
                    Order(
                        bot_id=ctx.bot_id,
                        ticker=ticker,
                        side=Side.SELL,
                        qty=pos.qty,
                        ref_price_eur=ref,
                        signal_reason=f"mean_reversion: {exit_reason}",
                        asset_class=AssetClass.STOCK,
                    )
                )

        # --- 2. ENTRY checks — only if capacity allows ---
        tickers_being_sold = {o.ticker for o in orders if o.side is Side.SELL}
        positions_after_exits = len(snapshot.positions) - len(tickers_being_sold)
        slots_available = max_concurrent - positions_after_exits

        if slots_available <= 0:
            return orders

        # Market-level filter: skip all new entries if the index is in a bear market.
        if mkt_ticker and mkt_ticker in ctx.bars:
            mkt_bars = ctx.bars[mkt_ticker]
            if not above_sma(mkt_bars.df["close"], mkt_sma):
                log.info(
                    "mean_reversion bot=%d: %s below %d-day MA — skipping all entries",
                    ctx.bot_id, mkt_ticker, mkt_sma,
                )
                return orders

        for ticker, bars in ctx.bars.items():
            if slots_available <= 0:
                break

            # Skip if already held (and not being sold this cycle).
            if ticker in snapshot.positions and ticker not in tickers_being_sold:
                continue

            if len(bars.df) < min_history:
                continue

            price = ctx.prices_eur.get(ticker) or bars.last_close()
            if price <= 0:
                continue

            rsi_series = rsi(bars.df["close"], rsi_period)
            last_rsi = rsi_series.iloc[-1]
            if last_rsi != last_rsi:  # NaN guard
                continue
            current_rsi = float(last_rsi)

            if current_rsi >= entry_below:
                continue

            target_value = equity * per_pos_pct
            qty = round(target_value / price, 4)  # fractional shares supported by IBKR
            if qty <= 0:
                continue

            expected_profit = target_value * profit_target
            log.info(
                "mean_reversion bot=%d BUY %s: RSI=%.1f < %.0f "
                "(target €%.0f, qty=%d @ €%.2f)",
                ctx.bot_id, ticker, current_rsi, entry_below,
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
                        f"mean_reversion: RSI {current_rsi:.1f} < {entry_below} entry"
                    ),
                    expected_profit_eur=expected_profit,
                    asset_class=AssetClass.STOCK,
                )
            )
            slots_available -= 1

        return orders
