"""
Tool implementations for the Trade Explanation Agent.

Each function here is a *tool* — a piece of code the agent can call
when it needs specific information. The agent decides when and how to
call these; we just implement what they do.

Think of tools as the agent's senses:
- get_rsi_history   → lets it "see" price momentum over time
- get_news_headlines → lets it "read" recent news
- get_market_context → lets it understand the broader market backdrop
- get_position_history → lets it "recall" the full trade history
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date, timedelta

log = logging.getLogger(__name__)


def get_rsi_history(ticker: str, days: int = 60) -> str:
    """
    Returns RSI(14) values for the last `days` trading days.

    The agent uses this to understand the RSI trajectory that triggered
    a trade: how low did RSI go, when did it start recovering?
    """
    from analysis import market_data, price_signals

    # Fetch more history than we need — RSI needs ~14 bars to warm up
    bars = market_data.fetch_bars(ticker, period=f"{max(days * 2, 120)}d")
    if bars is None or bars.df.empty:
        return json.dumps({"error": f"No price data available for {ticker}"})

    rsi_series = price_signals.rsi(bars.df["close"])
    recent = rsi_series.dropna().tail(days)

    result = [
        {"date": str(idx.date()), "rsi": round(float(val), 1)}
        for idx, val in recent.items()
    ]
    return json.dumps(result)


def get_news_headlines(ticker: str, days: int = 30) -> str:
    """
    Fetches recent news headlines from Yahoo Finance RSS.

    The agent uses this to understand what news events caused or
    accompanied the RSI movement — earnings, macro events, scandals, etc.
    """
    import feedparser

    # Yahoo Finance uses the base ticker symbol without exchange suffixes
    yf_ticker = (
        ticker
        .replace(".AS", "")
        .replace(".DE", "")
        .replace(".PA", "")
        .replace(".SW", "")
        .replace(".L",  "")
    )
    url = f"https://finance.yahoo.com/rss/headline?s={yf_ticker}"

    try:
        feed = feedparser.parse(url)
        cutoff = date.today() - timedelta(days=days)

        headlines = []
        for entry in feed.entries[:25]:
            if not hasattr(entry, "published_parsed") or entry.published_parsed is None:
                continue
            pub_date = date.fromtimestamp(time.mktime(entry.published_parsed))
            if pub_date >= cutoff:
                headlines.append({
                    "date": str(pub_date),
                    "title": entry.get("title", ""),
                    "summary": entry.get("summary", "")[:300],
                })

        if not headlines:
            return json.dumps({"message": f"No recent news found for {ticker} ({yf_ticker})"})
        return json.dumps(headlines)

    except Exception as e:
        log.warning("get_news_headlines(%s): %s", ticker, e)
        return json.dumps({"error": f"Could not fetch news for {ticker}: {e}"})


def get_market_context(trade_date: str) -> str:
    """
    Returns S&P 500 (SXR8.DE) price and RSI in the 30 days around a date.

    The agent uses this to determine whether a stock's selloff was caused
    by broad market panic (easier to recover from) or stock-specific trouble.
    """
    from analysis import market_data, price_signals

    bars = market_data.fetch_bars("SXR8.DE", period="6mo")
    if bars is None or bars.df.empty:
        return json.dumps({"error": "No market data available for SXR8.DE"})

    rsi_series = price_signals.rsi(bars.df["close"])

    try:
        target = date.fromisoformat(trade_date)
    except ValueError:
        return json.dumps({"error": f"Invalid date format: {trade_date}. Use YYYY-MM-DD."})

    start = target - timedelta(days=20)
    end   = target + timedelta(days=10)

    mask      = (bars.df.index.date >= start) & (bars.df.index.date <= end)
    window    = bars.df[mask]
    rsi_win   = rsi_series[mask]

    result = [
        {
            "date":  str(idx.date()),
            "close": round(float(window.loc[idx, "close"]), 2),
            "rsi":   round(float(rsi_win.loc[idx]), 1) if idx in rsi_win.index else None,
        }
        for idx in window.index
    ]
    return json.dumps(result)


def get_position_history(bot_id: int, ticker: str) -> str:
    """
    Returns all trades (buys, adds, sells) for a given bot and ticker.

    The agent uses this to see the full picture: when did we first buy,
    did we add at a loss, what was our average cost, why did we exit?
    """
    from core.db import Trade, get_session

    with get_session() as s:
        trades = (
            s.query(Trade)
            .filter(Trade.bot_id == bot_id, Trade.ticker == ticker)
            .order_by(Trade.timestamp)
            .all()
        )
        result = [
            {
                "date":          str(t.timestamp.date()),
                "side":          t.side,
                "qty":           round(t.qty, 4),
                "price_eur":     round(t.price_eur, 2),
                "fee_eur":       round(t.fee_eur, 2),
                "signal_reason": t.signal_reason,
            }
            for t in trades
        ]

    if not result:
        return json.dumps({"message": f"No trade history for bot {bot_id}, ticker {ticker}"})
    return json.dumps(result)
