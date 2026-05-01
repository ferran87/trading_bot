"""Market data fetcher.

Phase 1 uses yfinance for everything (daily bars). Phase 1b will add an
IBKR primary fetch with yfinance fallback.

Everything here is CACHED per process — tests can clear the cache via
`clear_cache()`. The cache also lets main.py's multiple bots share one
yfinance download per ticker per run.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import pandas as pd

log = logging.getLogger(__name__)


@dataclass
class Bars:
    """Thin wrapper around a pandas DataFrame of OHLCV bars for ONE ticker.

    Columns (lower-case): open, high, low, close, volume.
    Index: pandas.DatetimeIndex (tz-naive, daily).
    """

    ticker: str
    df: pd.DataFrame

    def last_close(self) -> float:
        return float(self.df["close"].iloc[-1])

    def last_date(self) -> date:
        return self.df.index[-1].date()


_CACHE: dict[tuple[str, str], Bars] = {}


def clear_cache() -> None:
    _CACHE.clear()


def fetch_bars(
    ticker: str,
    *,
    period: str = "6mo",
    end: datetime | None = None,
) -> Bars:
    """Fetch daily bars. Cached per (ticker, period).

    Parameters
    ----------
    ticker : str
        yfinance ticker (e.g. "SXR8.DE").
    period : str
        yfinance period string. "6mo" is plenty for a 63-day momentum lookback.
    end : datetime | None
        If set, bars are truncated to <= end (useful for backtest / tests).
    """
    key = (ticker, period)
    if key not in _CACHE:
        import yfinance as yf

        df = yf.download(
            ticker,
            period=period,
            auto_adjust=True,
            progress=False,
            threads=False,
        )
        if df is None or df.empty:
            raise RuntimeError(f"yfinance returned no data for {ticker!r}")
        df = df.rename(columns=str.lower)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0] for c in df.columns]
        keep = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
        df = df[keep].copy()
        df.index = pd.to_datetime(df.index).tz_localize(None)
        _CACHE[key] = Bars(ticker=ticker, df=df)

    bars = _CACHE[key]
    if end is not None:
        cut = pd.Timestamp(end).tz_localize(None) if getattr(end, "tzinfo", None) else pd.Timestamp(end)
        trimmed = bars.df[bars.df.index <= cut]
        if trimmed.empty:
            raise RuntimeError(f"No bars for {ticker!r} before {end!r}")
        return Bars(ticker=ticker, df=trimmed)
    return bars


def fetch_many(tickers: list[str], *, period: str = "6mo") -> dict[str, Bars]:
    """Fetch each ticker individually. Slower than yfinance's batch mode but
    gives per-ticker error isolation — one bad symbol won't kill the whole run."""
    out: dict[str, Bars] = {}
    for t in tickers:
        try:
            out[t] = fetch_bars(t, period=period)
        except Exception as e:
            log.warning("market_data: could not fetch %s: %s", t, e)
    return out


def prefetch_since(
    tickers: list[str],
    min_days: int,
    *,
    as_of: date | None = None,
) -> dict[str, Bars]:
    """Convenience used by strategies that need at least `min_days` of history.

    If ``as_of`` is set, each series is truncated to daily bars with
    index ``<= as_of`` (use the last *session* close before or on that
    calendar date — e.g. Friday close when ``as_of`` is Friday and you
    run on Saturday).
    """
    # yfinance's `period` is relative to today. When as_of is in the past we
    # must also cover the gap (today - as_of) so truncation leaves enough bars.
    # Use a fixed generous period so the per-ticker cache hits across all
    # simulated days in a backtest.
    if as_of is None:
        months = max(2, (min_days // 21) + 2)
        period = f"{months}mo"
    else:
        period = "2y"
    out: dict[str, Bars] = {}
    for t in tickers:
        try:
            if as_of is None:
                out[t] = fetch_bars(t, period=period)
            else:
                end = datetime.combine(as_of, datetime.max.time()).replace(microsecond=0)
                out[t] = fetch_bars(t, period=period, end=end)
        except Exception as e:
            log.warning("market_data: could not fetch %s: %s", t, e)
    return out


def _venue_currency(ticker: str) -> str:
    """Derive the ticker's currency from the venue tag in watchlists.yaml.

    Venue names encode the currency as the last token after '_'
    (e.g. xetra_eur → EUR, nasdaq_usd → USD). Defaults to EUR for unknown.
    """
    try:
        from core.config import CONFIG
        venue_map: dict = CONFIG.watchlists.get("venue", {})
        entry = venue_map.get(ticker, {})
        venue: str = entry.get("venue", "xetra_eur")
        return venue.rsplit("_", 1)[-1].upper()
    except Exception:
        return "EUR"


# Rough fallback rates used only when yfinance FX fetch fails entirely.
# Better to show a slightly wrong EUR price than raw USD/CHF.
_FX_FALLBACK: dict[str, float] = {"USD": 0.88, "CHF": 0.95}


def last_prices_eur(bars_by_ticker: dict[str, Bars]) -> dict[str, float]:
    """Return the latest close price for each ticker, converted to EUR.

    Reads the venue map from watchlists.yaml to determine each ticker's
    native currency, then applies an EOD yfinance FX rate for non-EUR tickers.

    When the FX fetch fails (transient yfinance error) a hardcoded fallback
    rate is applied rather than returning the raw foreign-currency price,
    which would be silently cached and misread as EUR by the dashboard.
    """
    from core import fx

    out: dict[str, float] = {}
    for ticker, bars in bars_by_ticker.items():
        close = bars.last_close()
        ccy = _venue_currency(ticker)
        if ccy != "EUR":
            try:
                close = fx.to_eur(close, ccy)
            except Exception as e:
                fallback = _FX_FALLBACK.get(ccy, 1.0)
                log.warning(
                    "market_data: FX conversion failed for %s (%s→EUR): %s; "
                    "applying fallback rate %.4f", ticker, ccy, e, fallback,
                )
                close = close * fallback
        out[ticker] = close
    return out


__all__ = [
    "Bars",
    "fetch_bars",
    "fetch_many",
    "prefetch_since",
    "clear_cache",
    "last_prices_eur",
]
