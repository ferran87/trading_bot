"""Bot 1 — Aggressive: Momentum Rotation on High-Beta Stocks + Crypto ETPs.

Rules:
* Universe: stocks_aggressive + crypto_etps (see watchlists.yaml).
* Signal: rank by total return over `lookback_days` (21 = ~1 trading month).
* Hold: top 4 equal-weighted (25% each).
* Trend filter: if ALL top-4 have negative return, go 100% cash.
* Rebalance: Monday only. Use ``main.py --force-rebalance`` to override.

Implementation notes:
* Asset class (STOCK / CRYPTO / ETF) is derived per ticker from the venue
  map in watchlists.yaml so risk.py applies the correct caps.
* Proposes SELLs for exits first so freed cash is available for BUYs.
"""
from __future__ import annotations

import logging

from analysis.price_signals import momentum_rank
from core.config import CONFIG
from core.types import AssetClass, Order, PortfolioSnapshot, Side
from strategies.base import Strategy, StrategyContext

log = logging.getLogger(__name__)

_CLASS_MAP = {"etf": AssetClass.ETF, "crypto": AssetClass.CRYPTO}


def _asset_class_for(ticker: str) -> AssetClass:
    cls = CONFIG.watchlists.get("venue", {}).get(ticker, {}).get("class", "stock")
    return _CLASS_MAP.get(cls, AssetClass.STOCK)


class AggressiveMomentumStrategy(Strategy):
    name = "aggressive_momentum"

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
                "aggressive_momentum bot=%d skipping: today is weekday %d, rebalance on %d",
                ctx.bot_id, ctx.today.weekday(), rebalance_weekday,
            )
            return []
        if ctx.force_rebalance and ctx.today.weekday() != rebalance_weekday:
            log.info(
                "aggressive_momentum bot=%d: force_rebalance=True (weekday %d, would rebalance on %d)",
                ctx.bot_id, ctx.today.weekday(), rebalance_weekday,
            )

        closes = {}
        for ticker, bars in ctx.bars.items():
            if len(bars.df) >= min_history:
                closes[ticker] = bars.df["close"]
        if not closes:
            log.warning("aggressive_momentum bot=%d: no ticker has enough history", ctx.bot_id)
            return []

        ranked = momentum_rank(closes, lookback)
        if not ranked:
            return []

        top = ranked[:top_n]
        log.info(
            "aggressive_momentum bot=%d ranked top: %s",
            ctx.bot_id, [(t, f"{r*100:.2f}%") for t, r in top],
        )

        if trend_filter and all(r < 0 for _, r in top):
            log.info("aggressive_momentum bot=%d: trend filter triggered, going to cash", ctx.bot_id)
            targets: dict[str, float] = {}
        else:
            target_weight = 1.0 / len(top)
            targets = {t: target_weight for t, _ in top}

        orders: list[Order] = []
        equity = snapshot.total_eur

        for ticker, pos in snapshot.positions.items():
            if ticker not in targets and pos.qty > 0:
                orders.append(
                    Order(
                        bot_id=ctx.bot_id,
                        ticker=ticker,
                        side=Side.SELL,
                        qty=pos.qty,
                        ref_price_eur=pos.last_price_eur,
                        signal_reason=f"aggressive momentum rotation: exit {ticker}",
                        asset_class=_asset_class_for(ticker),
                    )
                )

        for ticker, weight in targets.items():
            bars = ctx.bars.get(ticker)
            if bars is None:
                continue
            price = ctx.prices_eur.get(ticker) or bars.last_close()
            if price <= 0:
                continue
            target_value = equity * weight
            existing_value = (
                snapshot.positions[ticker].market_value_eur
                if ticker in snapshot.positions else 0.0
            )
            delta_value = target_value - existing_value
            if delta_value <= 0:
                continue
            qty = round(delta_value / price, 4)  # fractional shares supported by IBKR
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
                        f"aggressive momentum: top-{top_n} rebalance to "
                        f"{weight*100:.1f}% (delta €{delta_value:.2f})"
                    ),
                    asset_class=_asset_class_for(ticker),
                )
            )

        return orders
