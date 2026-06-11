"""Market data fetcher.

Uses yfinance for everything (daily bars).

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
        """Return the last non-NaN close. yfinance appends today's partial bar
        (NaN close) before market open — dropna() skips it and returns the
        last completed session's close instead."""
        valid = self.df["close"].dropna()
        if valid.empty:
            return float("nan")
        return float(valid.iloc[-1])

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
            timeout=60,
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


def to_eur_series(close, currency: str):
    """Convert a native-currency close series to EUR using daily FX rates.

    For each date in the index, applies the EUR/<ccy> rate from that date
    (or the nearest prior cached value).  Used by strategies whose trailing
    stop logic needs to measure drawdown in EUR (the account currency),
    not in the instrument's native currency — otherwise FX moves can mask
    real EUR P&L when stocks rally in USD but EUR strengthens against USD
    (NVDA 2026-06-05 incident: -12.9% USD drawdown but +0.5% EUR gain).

    Returns the same pandas index with EUR-denominated values.  EUR tickers
    are returned unchanged.  If FX fetch fails entirely, returns the input
    untouched (the trailing stop then degrades to native-currency behaviour
    — same as the old code, no regression).
    """
    import pandas as pd
    if close is None or len(close) == 0 or currency.upper() == "EUR":
        return close
    from core import fx
    # Seed the cache by requesting the most recent date — this triggers a
    # 2-year bulk download that populates every date in the window at once.
    last_date = close.index[-1]
    seed_date = last_date.date() if hasattr(last_date, "date") else last_date
    try:
        fx.eur_per_unit(currency, as_of=seed_date)
    except Exception:
        log.warning("to_eur_series: FX seed fetch failed for %s — returning native series", currency)
        return close
    # Convert each row; missing dates fall back to most recent prior rate
    out_values = []
    last_rate = None
    for ts, native_price in close.items():
        d = ts.date() if hasattr(ts, "date") else ts
        try:
            rate = fx.eur_per_unit(currency, as_of=d)
            last_rate = rate
        except Exception:
            rate = last_rate if last_rate is not None else 1.0
        out_values.append(float(native_price) * rate)
    return pd.Series(out_values, index=close.index, name="close_eur")


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
        import math as _math
        if _math.isfinite(close):
            out[ticker] = close
        else:
            log.warning("market_data: last_close for %s is not finite (%r) — omitting", ticker, close)
    return out


__all__ = [
    "Bars",
    "fetch_bars",
    "fetch_many",
    "prefetch_since",
    "clear_cache",
    "last_prices_eur",
    "to_eur_series",
]
