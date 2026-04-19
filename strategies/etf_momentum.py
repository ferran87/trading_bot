"""Bot 1 — Conservative: ETF Momentum Rotation.

Rules (from PROJECT_PLAN.md):

* Universe: UCITS ETFs (see watchlists.yaml `etfs_ucits`; expand after IBKR contract checks).
* Signal: rank by total return over `lookback_days` (63 = 3 trading months).
* Hold: top 3 equal-weighted (~33% each).
* Trend filter: if ALL top-3 have negative return, go 100% cash.
* Rebalance: Monday only. No intra-week action. For manual runs use
  ``main.py --force-rebalance`` (optional ``--as-of`` for last close date).

Implementation notes:
* Proposes SELLs for anything currently held that shouldn't be, then BUYs
  for the remaining slots. The executor processes SELLs first so freed
  cash is available.
* Uses `ref_price_eur = last close` for sizing. MockBroker fills near this.
* `asset_class=ETF` is set so risk.py applies the 35% cap.
"""
from __future__ import annotations

import logging

from analysis.price_signals import momentum_rank
from core.types import AssetClass, Order, PortfolioSnapshot, Side
from strategies.base import Strategy, StrategyContext

log = logging.getLogger(__name__)


class EtfMomentumStrategy(Strategy):
    name = "etf_momentum"

    def propose_orders(
        self,
        snapshot: PortfolioSnapshot,
        ctx: StrategyContext,
    ) -> list[Order]:
        params = ctx.params
        lookback = int(params["lookback_days"])
        top_n = int(params["top_n"])
        rebalance_weekday = int(params["rebalance_weekday"])
        trend_filter = bool(params.get("trend_filter", True))
        min_history = int(params.get("min_history_days", lookback + 5))

        if ctx.today.weekday() != rebalance_weekday and not ctx.force_rebalance:
            log.debug(
                "etf_momentum bot=%d skipping: today is weekday %d, rebalance on %d",
                ctx.bot_id, ctx.today.weekday(), rebalance_weekday,
            )
            return []
        if ctx.force_rebalance and ctx.today.weekday() != rebalance_weekday:
            log.info(
                "etf_momentum bot=%d: force_rebalance=True (weekday %d, would normally rebalance on %d)",
                ctx.bot_id, ctx.today.weekday(), rebalance_weekday,
            )

        closes = {}
        for ticker, bars in ctx.bars.items():
            if len(bars.df) >= min_history:
                closes[ticker] = bars.df["close"]
        if not closes:
            log.warning("etf_momentum bot=%d: no ticker has enough history", ctx.bot_id)
            return []

        ranked = momentum_rank(closes, lookback)
        if not ranked:
            return []

        top = ranked[:top_n]
        log.info(
            "etf_momentum bot=%d ranked top: %s",
            ctx.bot_id, [(t, f"{r*100:.2f}%") for t, r in top],
        )

        # Trend filter: all negative → fully cash.
        if trend_filter and all(r < 0 for _, r in top):
            log.info("etf_momentum bot=%d: trend filter triggered, going to cash", ctx.bot_id)
            targets: dict[str, float] = {}
        else:
            target_weight = 1.0 / len(top)
            targets = {t: target_weight for t, _ in top}

        orders: list[Order] = []
        equity = snapshot.total_eur

        # 1. SELL anything we hold that isn't a target.
        for ticker, pos in snapshot.positions.items():
            if ticker not in targets and pos.qty > 0:
                orders.append(
                    Order(
                        bot_id=ctx.bot_id,
                        ticker=ticker,
                        side=Side.SELL,
                        qty=pos.qty,
                        ref_price_eur=pos.last_price_eur,
                        signal_reason=f"momentum rotation: exit {ticker}",
                        asset_class=AssetClass.ETF,
                    )
                )

        # 2. For each target, BUY enough shares to reach the target weight.
        for ticker, weight in targets.items():
            bars = ctx.bars.get(ticker)
            if bars is None:
                continue
            price = bars.last_close()
            if price <= 0:
                continue
            target_value = equity * weight
            existing_value = (
                snapshot.positions[ticker].market_value_eur
                if ticker in snapshot.positions else 0.0
            )
            delta_value = target_value - existing_value
            if delta_value <= 0:
                # Slightly over-weight is fine; we don't trim unless we'd exit.
                continue
            qty = int(delta_value // price)  # whole shares
            if qty <= 0:
                continue
            orders.append(
                Order(
                    bot_id=ctx.bot_id,
                    ticker=ticker,
                    side=Side.BUY,
                    qty=float(qty),
                    ref_price_eur=price,
                    signal_reason=(
                        f"momentum rotation: top-{top_n} rebalance to "
                        f"{weight*100:.1f}% (delta €{delta_value:.2f})"
                    ),
                    asset_class=AssetClass.ETF,
                )
            )

        return orders
