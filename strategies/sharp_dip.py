"""Bot 3 — Sharp Dip Recovery (hybrid dip-buyer + trend-rider).

Entry signal (all required):
  1. consec_down_days >= consec_down_days param (default 3)
  2. 5-day cumulative drop >= drop_5d_pct param (default 5%)
  3. RSI(14) < rsi_entry_below (default 35)

Exit — two modes decided per position each cycle:

  SHORT mode (default — first 14 days while still under water):
    - Hard stop:    -stop_loss_pct from blended avg entry
    - Trailing:     price drops >trail_pct from peak close since entry
    - Safety net:   max_days_held cap

  GRADUATED mode (position profitable at day 7+):
    - Hard stop:    same -stop_loss_pct (unchanged)
    - Trailing:     wider long_trail_pct from peak (lets winners run)
    - Safety net:   disabled — trailing stop does the work

Pyramiding: a second (or third) BUY of a name already held is allowed when:
  - Current position is profitable (blended avg < price) — never average down
  - The full entry signal fires again (fresh dip within an uptrend)
  - Fewer than max_adds_per_ticker prior adds exist for this position

Sizing: up to max_concurrent (20) positions at per_position_pct (5%) each.
A fully-pyramided name (3 lots) caps at 15% of equity.
"""
from __future__ import annotations

import logging

import pandas as pd

from analysis.price_signals import consecutive_down_days, rsi, total_return
from core.config import CONFIG
from core.types import AssetClass, Order, PortfolioSnapshot, Side
from strategies.base import Strategy, StrategyContext

log = logging.getLogger(__name__)

_CLASS_MAP = {"etf": AssetClass.ETF, "crypto": AssetClass.CRYPTO}


def _asset_class_for(ticker: str) -> AssetClass:
    cls = CONFIG.watchlists.get("venue", {}).get(ticker, {}).get("class", "stock")
    return _CLASS_MAP.get(cls, AssetClass.STOCK)


class SharpDipStrategy(Strategy):
    name = "sharp_dip"

    def propose_orders(
        self,
        snapshot: PortfolioSnapshot,
        ctx: StrategyContext,
    ) -> list[Order]:
        params = ctx.params
        consec_min      = int(params.get("consec_down_days", 3))
        drop_threshold  = float(params.get("drop_5d_pct", 0.05))
        stop_loss       = float(params["stop_loss_pct"])
        trail_pct       = float(params.get("trail_pct", 0.07))
        long_trail_pct  = float(params.get("long_trail_pct", 0.12))
        max_days        = int(params.get("max_days_held", 14))
        max_concurrent  = int(params["max_concurrent"])
        per_pos_pct     = float(params["per_position_pct"])
        min_history     = int(params.get("min_history_days", 40))
        rsi_entry_below = params.get("rsi_entry_below")
        rsi_entry_below = float(rsi_entry_below) if rsi_entry_below is not None else None
        grad_min_days   = int(params.get("graduate_min_days", 7))
        max_adds        = int(params.get("max_adds_per_ticker", 2))

        orders: list[Order] = []
        equity = snapshot.total_eur

        # --- 1. EXIT checks on all held positions ---
        for ticker, pos in snapshot.positions.items():
            bars = ctx.bars.get(ticker)
            price = ctx.prices_eur.get(ticker) or (bars.last_close() if bars is not None else None)
            days_held = (ctx.today - pos.entry_date).days

            exit_reason: str | None = None

            if price is None:
                if days_held >= max_days:
                    exit_reason = f"max days held ({days_held}d, no price data)"
            else:
                gain = price / pos.avg_entry_eur - 1.0

                # Hard stop applies in both modes.
                if gain <= -stop_loss:
                    exit_reason = f"stop loss {gain*100:.1f}% <= -{stop_loss*100:.0f}%"
                else:
                    # Peak close since entry: use native currency (USD for US stocks) so the
                    # drawdown ratio is dimensionally consistent.
                    price_native = bars.last_close() if bars is not None else price
                    if bars is not None and len(bars.df) > 0:
                        entry_ts = pd.Timestamp(pos.entry_date)
                        since_entry = bars.df["close"][bars.df.index >= entry_ts]
                        peak_native = float(since_entry.max()) if not since_entry.empty else price_native
                    else:
                        peak_native = price_native

                    # Graduation: sticky — once native peak has ever exceeded avg entry (converted
                    # back to native by multiplying by current FX proxy) the position keeps the
                    # wider trail. Use a simpler proxy: peak exceeded entry price in native terms.
                    # Since avg_entry_eur is in EUR and peak_native is in USD, compare gain > 0.
                    graduated = days_held >= grad_min_days and gain > 0

                    active_trail = long_trail_pct if graduated else trail_pct
                    drawdown_from_peak = price_native / peak_native - 1.0

                    if drawdown_from_peak <= -active_trail:
                        mode = "graduated" if graduated else "short"
                        fx_ratio = price / price_native if price_native else 1.0
                        peak_eur = peak_native * fx_ratio
                        exit_reason = (
                            f"{mode} trailing stop {drawdown_from_peak*100:.1f}% from peak "
                            f"(peak=EUR{peak_eur:.2f}, now=EUR{price:.2f}, gain={gain*100:.1f}%)"
                        )
                    elif not graduated and days_held >= max_days:
                        exit_reason = f"safety net: max days held ({days_held}d)"

            if exit_reason:
                ref = price if price and price > 0 else pos.last_price_eur
                log.info(
                    "sharp_dip bot=%d SELL %s: %s (entry=%.2f ref=%.2f days=%d)",
                    ctx.bot_id, ticker, exit_reason, pos.avg_entry_eur, ref, days_held,
                )
                orders.append(
                    Order(
                        bot_id=ctx.bot_id,
                        ticker=ticker,
                        side=Side.SELL,
                        qty=pos.qty,
                        ref_price_eur=ref,
                        signal_reason=f"sharp_dip: {exit_reason}",
                        asset_class=_asset_class_for(ticker),
                    )
                )

        # --- 2. ENTRY checks (fresh entries + pyramid adds) ---
        tickers_being_sold = {o.ticker for o in orders if o.side is Side.SELL}
        # Each open position (after exits) occupies one slot; pyramid adds each take a slot.
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

            already_held = ticker in snapshot.positions and ticker not in tickers_being_sold

            if already_held:
                pos = snapshot.positions[ticker]
                current_gain = price / pos.avg_entry_eur - 1.0

                # Pyramid gate: only add to profitable positions.
                if current_gain <= 0:
                    continue

                # Cap adds per ticker.
                prior_buys = ctx.buys_per_ticker.get(ticker, 0)
                if prior_buys > max_adds:
                    continue
            # (else: fresh entry, no extra gate)

            # Entry signal must fire in both cases.
            consec = consecutive_down_days(bars.df["close"])
            if consec < consec_min:
                continue

            drop_5d = total_return(bars.df["close"], 5)
            if drop_5d != drop_5d or drop_5d > -drop_threshold:
                continue

            if rsi_entry_below is not None:
                rsi_ser = rsi(bars.df["close"], 14)
                rsi_val = float(rsi_ser.iloc[-1]) if len(rsi_ser) else float("nan")
                if rsi_val != rsi_val or rsi_val >= rsi_entry_below:
                    continue

            target_value = equity * per_pos_pct
            qty = round(target_value / price, 4)
            if qty <= 0:
                continue

            label = "pyramid add" if already_held else "entry"
            log.info(
                "sharp_dip bot=%d BUY %s (%s): consec=%d drop_5d=%.1f%% "
                "(target €%.0f, qty=%.4f @ €%.2f)",
                ctx.bot_id, ticker, label, consec, drop_5d * 100,
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
                        f"sharp_dip: {label} {consec}d down, {drop_5d*100:.1f}% 5d-drop"
                    ),
                    asset_class=_asset_class_for(ticker),
                )
            )
            slots_available -= 1

        return orders
