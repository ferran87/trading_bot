"""EUR-centric FX helpers.

Every fill comes back from IBKR in its local currency. We convert to EUR
at the fill timestamp so the virtual book stays in a single unit.

Sources, in preference order:
  1. If the broker has already been given an FX rate (e.g. fetched from
     IBKR's Forex product at fill time), the caller passes it directly.
  2. yfinance "EURXXX=X" quote (daily close). Plenty for EOD cadence.

yfinance results are cached per-process; tests can clear via
``fx.clear_cache()``.
"""
from __future__ import annotations

import logging
from datetime import date

log = logging.getLogger(__name__)

_EUR = "EUR"
_PAIR_TEMPLATE = "EUR{ccy}=X"   # yfinance convention: EURUSD=X => EUR->USD

_CACHE: dict[tuple[str, date], float] = {}


def clear_cache() -> None:
    _CACHE.clear()


def to_eur(amount: float, currency: str, *, as_of: date | None = None) -> float:
    """Convert ``amount`` in ``currency`` to EUR.

    ``as_of`` lets backtests pin a historical rate; live runs pass None
    and we use the latest daily close.
    """
    if amount == 0 or currency == _EUR:
        return amount
    rate = eur_per_unit(currency, as_of=as_of)
    return amount * rate


def eur_per_unit(currency: str, *, as_of: date | None = None) -> float:
    """Return how many EUR one unit of ``currency`` is worth.

    We download ``EUR{ccy}=X``. For any XXX, yfinance's close is XXX per
    1 EUR (e.g. EURUSD=X close = USD per EUR). EUR per 1 XXX is ``1/close``.
    """
    if currency == _EUR:
        return 1.0
    key = (currency, as_of or date.today())
    if key not in _CACHE:
        rate = _fetch_rate(currency, as_of)
        _CACHE[key] = rate
    return _CACHE[key]


def _fetch_rate(currency: str, as_of: date | None) -> float:
    """yfinance quote for the EUR/<ccy> pair, inverted.

    When as_of is given (backtest mode) we download a 2-year window once and
    populate the cache for every date in that window, so a year-long backtest
    costs one HTTP call, not one per day.
    """
    import pandas as pd
    import yfinance as yf

    pair = _PAIR_TEMPLATE.format(ccy=currency)
    if as_of is not None:
        # Bulk download covering up to 2 years back so any backtest window is served.
        start = (pd.Timestamp(as_of) - pd.Timedelta(days=800)).date()
        end = (pd.Timestamp(as_of) + pd.Timedelta(days=2)).date()
        df = yf.download(pair, start=start, end=end, progress=False,
                         auto_adjust=True, threads=False)
    else:
        df = yf.download(pair, period="1mo", progress=False, auto_adjust=True,
                         threads=False)
    if df is None or df.empty:
        raise RuntimeError(f"No FX data for {pair}")

    close_col = df["Close"] if "Close" in df.columns else df.iloc[:, 0]
    if hasattr(close_col, "iloc") and len(close_col.shape) == 2:
        close_col = close_col.iloc[:, 0]

    # Populate cache for all dates in the downloaded window (backtest efficiency).
    if as_of is not None:
        for idx, val in close_col.items():
            d = idx.date() if hasattr(idx, "date") else idx
            if float(val) > 0:
                _CACHE[(currency, d)] = 1.0 / float(val)

        cutoff = pd.Timestamp(as_of).tz_localize(None)
        subset = close_col[close_col.index <= cutoff]
        if subset.empty:
            raise RuntimeError(f"No FX data for {pair} on or before {as_of}")
        foreign_per_eur = float(subset.iloc[-1])
    else:
        foreign_per_eur = float(close_col.iloc[-1])

    if foreign_per_eur <= 0:
        raise RuntimeError(f"Invalid FX close for {pair}: {foreign_per_eur}")
    return 1.0 / foreign_per_eur


__all__ = ["to_eur", "eur_per_unit", "clear_cache"]
