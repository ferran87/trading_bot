"""Tools for the AI Thesis Portfolio Manager agent (bot 30).

Each function here is exposed to Claude via a JSON schema in
``agents/portfolio_manager.py``.  They return strings (the agent loop
expects string tool results) but internally work with native Python types.

Guardrails built into code (not just prompt):
  - ``submit_thesis``       validates bear_case length, invalidates_if count,
                            horizon_months floor, and conviction range.
  - ``submit_review``       enforces the conviction-throttle (max 1 step/week),
                            blocks 'exit' rationale that doesn't cite an
                            invalidates_if condition, and enforces the 14-day
                            hold floor before any thesis-driven exit.
  - ``get_active_theses``   surfaces the audit trail Claude needs to avoid
                            reinventing context it already built.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone, timedelta

from sqlalchemy.orm import Session

from core.db import Thesis, ThesisAction, ThesisReviewLog, get_session

log = logging.getLogger(__name__)

BOT_ID = 30                       # ai_thesis bot id
MIN_BEAR_CASE_CHARS = 100         # enforce substantive devil's advocate
MIN_INVALIDATION_CONDITIONS = 2   # must pre-commit exit criteria
MIN_HORIZON_MONTHS = 3            # theses are medium-term by design
MAX_CONVICTION_STEP_PER_WEEK = 1  # throttle rapid conviction swings
MIN_HOLD_DAYS_BEFORE_EXIT = 14    # no thesis-driven exit in first 14 days
MAX_REASONABLE_UPSIDE_PCT = 100   # any "+X%" claim above this is auto-flagged
IMMEDIATE_ENTRY_CONVICTION = 5    # only conviction 5 bypasses RSI/SMA gate (was 4)

CONVICTION_MULT = {5: 1.5, 4: 1.2, 3: 1.0, 2: 0.8, 1: 0.6}
BASE_PCT = 0.10    # 10% of bot capital
MAX_PCT  = 0.15    # hard cap regardless of conviction

# ── Content validation patterns ──────────────────────────────────────────────
# Forbidden authority appeals — banned to prevent meme-driven theses.
# (regex: matches whole word, case-insensitive)
_FORBIDDEN_PATTERNS = [
    (re.compile(r"\b(jim\s+)?cramer\b", re.IGNORECASE),
     "Cramer citations are banned (meme, not analysis)"),
    (re.compile(r"\bwall\s+street\s+(diu|says|mantenint?|holds?)\b", re.IGNORECASE),
     "Vague 'Wall Street says' phrasing banned — cite a specific tool result"),
    (re.compile(r"\banalistes?\s+(diuen?|mantenen?|esperen?)\b", re.IGNORECASE),
     "Vague 'analysts say' phrasing banned — use get_fundamentals for real targets"),
    (re.compile(r"\bels\s+experts?\s+(diuen|creuen|opinen)\b", re.IGNORECASE),
     "'Experts say' phrasing banned — cite specific tool data"),
]

# Pattern to extract numeric "+X%" claims from thesis prose for sanity check
_PERCENT_CLAIM_RE = re.compile(r"[+\-]?\s*(\d+(?:[\.,]\d+)?)\s*%")


def _extract_pct_claims(text: str) -> list[float]:
    """Return all numeric percentage values found in text (e.g. '+370%' → 370.0)."""
    out = []
    for m in _PERCENT_CLAIM_RE.finditer(text):
        try:
            out.append(float(m.group(1).replace(",", ".")))
        except ValueError:
            continue
    return out


def _validate_content(thesis_text: str, bull_case: str, bear_case: str) -> list[str]:
    """Return a list of validation errors (empty list if all checks pass).

    Checks:
    - Forbidden authority appeals (Cramer, vague 'Wall Street says', etc.)
    - Absurd percentage claims (>100% upside in any prose field)
    """
    errors = []
    full_text = f"{thesis_text}\n{bull_case}\n{bear_case}"

    # Forbidden patterns
    for pat, msg in _FORBIDDEN_PATTERNS:
        if pat.search(full_text):
            errors.append(f"Forbidden content: {msg}")

    # Sanity check on percentage claims
    pcts = _extract_pct_claims(full_text)
    absurd = [p for p in pcts if p > MAX_REASONABLE_UPSIDE_PCT]
    if absurd:
        errors.append(
            f"Absurd percentage claim(s) {absurd}: any single % > {MAX_REASONABLE_UPSIDE_PCT} "
            "is rejected as a likely arithmetic error. Recompute and resubmit."
        )

    return errors


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware(dt: datetime | None) -> datetime | None:
    """Normalize a datetime to UTC-aware.

    Postgres TIMESTAMP (without TZ) round-trips through SQLAlchemy as naive,
    but ``_utcnow()`` returns aware — subtracting them raises TypeError.
    Always pass DB datetimes through this before doing arithmetic with now.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _size_pct(conviction: int) -> float:
    raw = BASE_PCT * CONVICTION_MULT.get(conviction, 1.0)
    return round(min(raw, MAX_PCT), 4)


# ── Public tool functions ─────────────────────────────────────────────────────

def get_universe_tickers() -> str:
    """Return the curated watchlist with sector tags."""
    from pathlib import Path
    import yaml

    cfg_path = Path(__file__).parents[1] / "config" / "ai_thesis_universe.yaml"
    with open(cfg_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    # Deduplicate by ticker (AMZN appears twice in starter YAML)
    seen = set()
    tickers = []
    for t in data.get("tickers", []):
        if t["ticker"] not in seen:
            seen.add(t["ticker"])
            tickers.append(t)

    return json.dumps(tickers)


def get_ticker_analysis(ticker: str, news_days: int = 30, rsi_days: int = 90) -> str:
    """Return RSI history + recent news for a ticker in a single call.

    Consolidates what would otherwise be two separate tool calls so Claude
    can evaluate a candidate or review an active thesis efficiently.
    """
    from agents.tools import get_news_headlines, get_rsi_history
    rsi = json.loads(get_rsi_history(ticker, rsi_days))
    news = json.loads(get_news_headlines(ticker, news_days))
    return json.dumps({"ticker": ticker, "rsi_history": rsi, "news": news})


def get_market_context_today() -> str:
    """Return S&P 500 (SXR8.DE) RSI and price over the last 30 days."""
    from agents.tools import get_market_context
    from datetime import date
    return get_market_context(str(date.today()))


def get_fundamentals(ticker: str) -> str:
    """Return real fundamental metrics for a ticker from yfinance.

    This tool exists specifically to prevent Claude from hallucinating
    margins, P/E ratios, market cap, etc. from training-data memory.
    All numerical claims about the company in the thesis MUST come from
    a tool result like this one.

    Returns JSON with currency, market cap, valuation ratios, margins,
    growth rates, current price, 52w range, and dividend yield.
    """
    try:
        import yfinance as yf
    except ImportError:
        return json.dumps({"error": "yfinance not installed"})

    try:
        t = yf.Ticker(ticker)
        info = t.info or {}
    except Exception as e:
        return json.dumps({"error": f"yfinance fetch failed for {ticker}: {e}"})

    if not info.get("currentPrice") and not info.get("regularMarketPrice"):
        return json.dumps({
            "ticker": ticker,
            "warning": "yfinance returned no price data; fundamentals may be incomplete",
            "raw_keys_returned": list(info.keys())[:20],
        })

    def _pct(x):
        return round(x * 100, 2) if isinstance(x, (int, float)) else None

    # Earnings calendar (next reporting date + EPS / revenue estimates)
    next_earnings_date = None
    eps_estimate_avg = None
    revenue_estimate_avg = None
    is_estimate = None
    try:
        cal = t.calendar or {}
        dates = cal.get("Earnings Date") or []
        if dates:
            next_earnings_date = str(dates[0])
        eps_estimate_avg = cal.get("Earnings Average")
        revenue_estimate_avg = cal.get("Revenue Average")
        is_estimate = info.get("isEarningsDateEstimate")
    except Exception:
        pass

    return json.dumps({
        "ticker": ticker,
        "currency": info.get("currency"),
        "current_price": info.get("currentPrice") or info.get("regularMarketPrice"),
        "market_cap": info.get("marketCap"),
        "enterprise_value": info.get("enterpriseValue"),
        "trailing_pe": info.get("trailingPE"),
        "forward_pe": info.get("forwardPE"),
        "peg_ratio": info.get("pegRatio"),
        "price_to_book": info.get("priceToBook"),
        "price_to_sales_ttm": info.get("priceToSalesTrailing12Months"),
        "profit_margin_pct": _pct(info.get("profitMargins")),
        "operating_margin_pct": _pct(info.get("operatingMargins")),
        "gross_margin_pct": _pct(info.get("grossMargins")),
        "ebitda_margin_pct": _pct(info.get("ebitdaMargins")),
        "revenue_growth_yoy_pct": _pct(info.get("revenueGrowth")),
        "earnings_growth_yoy_pct": _pct(info.get("earningsGrowth")),
        "earnings_quarterly_growth_pct": _pct(info.get("earningsQuarterlyGrowth")),
        "return_on_equity_pct": _pct(info.get("returnOnEquity")),
        "debt_to_equity": info.get("debtToEquity"),
        "current_ratio": info.get("currentRatio"),
        "dividend_yield_pct": _pct(info.get("dividendYield")),
        "beta": info.get("beta"),
        "fifty_two_week_high": info.get("fiftyTwoWeekHigh"),
        "fifty_two_week_low": info.get("fiftyTwoWeekLow"),
        "fifty_day_avg": info.get("fiftyDayAverage"),
        "two_hundred_day_avg": info.get("twoHundredDayAverage"),
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        "country": info.get("country"),
        # Earnings calendar
        "next_earnings_date": next_earnings_date,
        "next_earnings_eps_estimate": eps_estimate_avg,
        "next_earnings_revenue_estimate": revenue_estimate_avg,
        "next_earnings_date_is_estimate": is_estimate,
    })


# ── SEC EDGAR 8-K filings ─────────────────────────────────────────────────────
# Companies file 8-Ks for material events (earnings, guidance updates, M&A,
# leadership changes). The earnings release is attached as Exhibit 99.1 and
# usually contains forward guidance numbers in prose form. This tool gives
# Claude direct access to that text instead of relying on Yahoo News headlines.
#
# US-only: SEC EDGAR covers US-listed companies. European tickers (with .AS,
# .DE, .PA, .SW, .L suffixes) are detected and skipped.

_EU_SUFFIXES = (".AS", ".DE", ".PA", ".SW", ".L", ".MI", ".MC", ".BR", ".AT", ".OL", ".HE", ".ST", ".CO")
_EDGAR_IDENTITY_SET = False


def _ensure_edgar_identity() -> None:
    """SEC requires a User-Agent identity for API access. Set it once."""
    global _EDGAR_IDENTITY_SET
    if _EDGAR_IDENTITY_SET:
        return
    try:
        from edgar import set_identity
        # SEC requires "Name email" — uses CLAUDE.md userEmail
        set_identity("Ferran Punso ferranpunso@gmail.com")
        _EDGAR_IDENTITY_SET = True
    except ImportError:
        log.warning("edgartools not installed; SEC EDGAR access disabled")


def get_recent_8k_filings(ticker: str, days: int = 90, limit: int = 5) -> str:
    """Return recent SEC 8-K filings for a US-listed ticker.

    For each filing returned:
      - filing_date
      - items (SEC item codes — e.g. '2.02' = Results of Operations,
        '7.01' = Reg FD Disclosure, '8.01' = Other Events, '9.01' = Exhibits)
      - url
      - has_earnings: True if the filing includes an earnings release
      - earnings_text: truncated markdown of the earnings press release
        (only present when has_earnings=True). Up to ~3500 chars — usually
        contains the Q's headline numbers, segment commentary, AND forward
        guidance (revenue/EPS/margin targets for next Q or full year).
      - key_metrics: structured revenue / net income / EPS dict where
        edgartools could extract them.

    Non-US tickers (with EU suffixes) return a clear message rather than data.

    The agent should call this for any thesis on a US large/mid-cap when the
    next_earnings_date is within the horizon, OR when news headlines mention
    'guidance', 'reaffirms', 'raises', 'lowers' — those words usually trace
    back to an 8-K worth reading.
    """
    if not ticker or any(ticker.upper().endswith(suf) for suf in _EU_SUFFIXES):
        return json.dumps({
            "ticker": ticker,
            "message": (
                f"{ticker} is not US-listed (SEC EDGAR is US-only). "
                "For European tickers, rely on news headlines via get_ticker_analysis."
            ),
        })

    try:
        from edgar import Company
    except ImportError:
        return json.dumps({"error": "edgartools not installed"})

    _ensure_edgar_identity()

    try:
        company = Company(ticker)
        if company is None or not company.cik:
            return json.dumps({"error": f"Could not resolve {ticker} on SEC EDGAR"})
    except Exception as e:
        return json.dumps({"error": f"SEC company lookup failed for {ticker}: {e}"})

    # Pull a generous window then filter by date in Python
    from datetime import date, timedelta
    cutoff = date.today() - timedelta(days=days)

    try:
        all_filings = company.get_filings(form="8-K").head(max(limit * 4, 20))
    except Exception as e:
        return json.dumps({"error": f"SEC filings fetch failed for {ticker}: {e}"})

    rows = []
    for f in all_filings:
        if len(rows) >= limit:
            break
        try:
            f_date = f.filing_date
            if hasattr(f_date, "date"):
                f_date = f_date.date()
            if f_date < cutoff:
                continue

            row = {
                "filing_date": str(f_date),
                "items": getattr(f, "items", None) or "",
                "url": getattr(f, "filing_url", None) or "",
                "has_earnings": False,
                "earnings_text": None,
                "key_metrics": None,
            }

            # Try parsing as CurrentReport (8-K type)
            try:
                obj = f.obj()
                if getattr(obj, "has_earnings", False):
                    row["has_earnings"] = True
                    er = obj.earnings
                    # Earnings press release as markdown (cleaner than raw text)
                    try:
                        att = er.attachment
                        md = att.markdown() if hasattr(att, "markdown") else None
                        if not md and hasattr(att, "text"):
                            md = att.text()
                        if md:
                            # Truncate aggressively — Claude doesn't need the
                            # 30+ pages of footnotes; the headline + guidance
                            # paragraphs are in the first ~3500 chars.
                            row["earnings_text"] = (md[:3500] + "\n... [truncated]"
                                                    if len(md) > 3500 else md)
                    except Exception as _:
                        pass

                    # Structured key metrics: only keep the fields that
                    # edgartools extracts reliably (period + EPS). Revenue
                    # / net_income come back with broken scales (e.g. 7.0
                    # for $2.709B) — better to omit than mislead. Claude
                    # should read the actual numbers from earnings_text.
                    try:
                        km = er.get_key_metrics() or {}
                        eps = km.get("eps_diluted") or km.get("eps_basic")
                        period = km.get("period")
                        if eps is not None or period:
                            row["key_metrics"] = {
                                "eps_diluted": eps,
                                "period": period,
                                "_note": "Revenue/net_income omitted — edgartools auto-extractor is unreliable for those fields. Read earnings_text for the real figures.",
                            }
                    except Exception:
                        pass
            except Exception:
                # Some 8-Ks aren't earnings-related — that's fine, just skip parsing
                pass

            rows.append(row)
        except Exception as e:
            log.debug("8-K row failed: %s", e)
            continue

    if not rows:
        return json.dumps({
            "ticker": ticker,
            "message": f"No 8-K filings in the last {days} days for {ticker}.",
        })

    return json.dumps({
        "ticker": ticker,
        "lookback_days": days,
        "filings": rows,
    })


def get_recent_earnings_history(ticker: str, quarters: int = 8) -> str:
    """Return historical EPS estimates vs actuals for the last N quarters.

    Returns the beat/miss pattern Claude needs to assess management's
    track record:
      - "Beat estimates 7 of last 8 quarters" → genuine outperformance signal
      - "Average surprise +9%" → consistent beats, likely sandbagging guidance

    Each row: earnings date, EPS estimate, reported EPS, surprise %.
    Most recent first. The next (upcoming) date will have estimate but
    no reported value yet.
    """
    try:
        import yfinance as yf
    except ImportError:
        return json.dumps({"error": "yfinance not installed"})

    try:
        t = yf.Ticker(ticker)
        df = t.earnings_dates
    except Exception as e:
        return json.dumps({"error": f"yfinance earnings_dates failed for {ticker}: {e}"})

    if df is None or df.empty:
        return json.dumps({"ticker": ticker, "message": "No earnings history available."})

    df = df.head(quarters).copy()

    rows = []
    for idx, row in df.iterrows():
        rows.append({
            "earnings_date": str(idx.date()) if hasattr(idx, "date") else str(idx),
            "eps_estimate": float(row["EPS Estimate"]) if row["EPS Estimate"] == row["EPS Estimate"] else None,
            "reported_eps": float(row["Reported EPS"]) if row["Reported EPS"] == row["Reported EPS"] else None,
            "surprise_pct": float(row["Surprise(%)"]) if row["Surprise(%)"] == row["Surprise(%)"] else None,
        })

    # Aggregate beat/miss stats over the reported quarters only
    reported = [r for r in rows if r["reported_eps"] is not None and r["eps_estimate"] is not None]
    beats = sum(1 for r in reported if r["reported_eps"] > r["eps_estimate"])
    misses = sum(1 for r in reported if r["reported_eps"] < r["eps_estimate"])
    inline = len(reported) - beats - misses
    avg_surprise = (
        round(sum(r["surprise_pct"] for r in reported if r["surprise_pct"] is not None)
              / max(1, len([r for r in reported if r["surprise_pct"] is not None])), 2)
        if reported else None
    )

    return json.dumps({
        "ticker": ticker,
        "quarters_returned": len(rows),
        "quarters_with_results": len(reported),
        "beats": beats,
        "misses": misses,
        "inline": inline,
        "average_surprise_pct": avg_surprise,
        "history": rows,
    })


def get_analyst_targets(ticker: str) -> str:
    """Return real analyst price targets and recommendations from yfinance.

    Use this instead of citing 'Wall Street targets' from memory.
    Returns mean/high/low targets, current recommendation key, and
    number of analysts contributing.
    """
    try:
        import yfinance as yf
    except ImportError:
        return json.dumps({"error": "yfinance not installed"})

    try:
        t = yf.Ticker(ticker)
        info = t.info or {}
    except Exception as e:
        return json.dumps({"error": f"yfinance fetch failed for {ticker}: {e}"})

    current = info.get("currentPrice") or info.get("regularMarketPrice")
    target_mean = info.get("targetMeanPrice")
    upside_pct = None
    if current and target_mean:
        upside_pct = round((target_mean / current - 1) * 100, 2)

    return json.dumps({
        "ticker": ticker,
        "currency": info.get("currency"),
        "current_price": current,
        "target_mean": target_mean,
        "target_high": info.get("targetHighPrice"),
        "target_low": info.get("targetLowPrice"),
        "target_median": info.get("targetMedianPrice"),
        "upside_to_mean_pct": upside_pct,
        "recommendation_key": info.get("recommendationKey"),
        "recommendation_mean": info.get("recommendationMean"),  # 1=Strong Buy, 5=Strong Sell
        "number_of_analyst_opinions": info.get("numberOfAnalystOpinions"),
    })


def get_active_themes_for_analyst() -> str:
    """Return user-approved investment themes for the analyst to use when submitting theses.

    The analyst calls this during Sunday scans to resolve theme_id before calling
    submit_thesis — theme_id is required when active themes exist.

    Implemented directly here (not imported from strategist_tools) to avoid a
    circular import: strategist_tools imports from pm_tools at module level.
    """
    from core.db import Theme
    with get_session() as s:
        themes = s.query(Theme).filter(Theme.status == "active").order_by(Theme.potential.desc()).all()
        if not themes:
            return json.dumps({"message": "No active themes. User must approve theme proposals first."})
        return json.dumps([
            {
                "id":                t.id,
                "name":              t.name,
                "potential":         t.potential,
                "importance":        t.importance,
                "candidate_tickers": t.candidate_tickers,
                "narrative_text":    t.narrative_text[:400],  # truncated for context efficiency
            }
            for t in themes
        ])


def _get_active_theme_ids() -> list[int]:
    """Return IDs of all active themes. Used inside submit_thesis validation."""
    from core.db import Theme
    with get_session() as s:
        return [t.id for t in s.query(Theme).filter(Theme.status == "active").all()]


def get_active_theses() -> str:
    """Return all theses that are currently active or waiting for a signal.

    Claude uses this at the start of each daily review to know which
    positions it is responsible for monitoring.
    """
    with get_session() as s:
        theses = (
            s.query(Thesis)
            .filter(Thesis.bot_id == BOT_ID, Thesis.status.in_(["candidate", "waiting", "active"]))
            .order_by(Thesis.opened_at)
            .all()
        )
        result = []
        for t in theses:
            result.append({
                "id":            t.id,
                "ticker":        t.ticker,
                "status":        t.status,
                "conviction":    t.conviction,
                "horizon_months": t.horizon_months,
                "opened_at":     str(t.opened_at.date()),
                "last_reviewed_at": str(t.last_reviewed_at.date()) if t.last_reviewed_at else None,
                "consecutive_weakening_count": t.consecutive_weakening_count,
                "thesis_text":           t.thesis_text,
                "invalidates_if":        t.invalidates_if,
                "catalysts":             t.catalysts,
                # Phase 4 scorecard fields (None for legacy theses)
                "theme_id":              t.theme_id,
                "positioning_vs_theme":  t.positioning_vs_theme,
                "execution_evidence":    t.execution_evidence,
                "valuation_assessment":  t.valuation_assessment,
            })

    if not result:
        return json.dumps({"message": "No active or waiting theses."})
    return json.dumps(result)


def submit_thesis(
    ticker: str,
    conviction: int,
    horizon_months: int,
    thesis_text: str,
    bull_case: str,
    bear_case: str,
    invalidates_if: list[str],
    catalysts: list[dict],
    target_price_eur: float | None = None,
    stop_price_eur: float | None = None,
    # Phase 4 scorecard — required when active themes exist, optional for legacy paths
    theme_id: int | None = None,
    positioning_vs_theme: str | None = None,
    execution_evidence: str | None = None,
    valuation_assessment: str | None = None,
) -> str:
    """Validate and persist a new thesis for a ticker.

    Guardrails enforced:
    - bear_case ≥ 100 chars (substantive devil's advocate required)
    - invalidates_if ≥ 2 items (pre-committed, measurable kill conditions)
    - horizon_months ≥ 3 (medium-term by design)
    - conviction in 1-5
    - No duplicate active thesis for the same ticker

    For conviction ≥ 4: creates a 'candidate' Thesis + pending 'open' ThesisAction.
    For conviction = 3: creates a 'waiting' Thesis (no action yet — strategy module
                        polls for RSI/SMA gate; creates action when triggered).
    For conviction ≤ 2: rejected — too uncertain to track.

    Returns JSON with {status, thesis_id, action_id, message}.
    """
    # ── Validation ──────────────────────────────────────────────────────────
    if conviction < 1 or conviction > 5:
        return json.dumps({"status": "error", "message": f"conviction must be 1-5, got {conviction}"})

    if conviction <= 2:
        return json.dumps({
            "status": "rejected",
            "message": (
                f"Conviction {conviction} is too low to create a thesis. "
                "Minimum conviction to track is 3 (waiting for technical confirmation), "
                "or 4+ to propose immediate entry."
            )
        })

    if len(bear_case.strip()) < MIN_BEAR_CASE_CHARS:
        return json.dumps({
            "status": "error",
            "message": (
                f"bear_case is too short ({len(bear_case.strip())} chars). "
                f"Minimum is {MIN_BEAR_CASE_CHARS} chars. Write a substantive devil's advocate case."
            )
        })

    if len(invalidates_if) < MIN_INVALIDATION_CONDITIONS:
        return json.dumps({
            "status": "error",
            "message": (
                f"invalidates_if must have ≥ {MIN_INVALIDATION_CONDITIONS} specific conditions, "
                f"got {len(invalidates_if)}. Pre-commit measurable exit criteria before entering."
            )
        })

    if horizon_months < MIN_HORIZON_MONTHS:
        return json.dumps({
            "status": "error",
            "message": (
                f"horizon_months must be ≥ {MIN_HORIZON_MONTHS}, got {horizon_months}. "
                "Theses are medium-term. For shorter plays, use the rules-based bots."
            )
        })

    if not thesis_text.strip():
        return json.dumps({"status": "error", "message": "thesis_text cannot be empty."})

    # ── Content validation (forbidden patterns + math sanity) ────────────────
    content_errors = _validate_content(thesis_text, bull_case, bear_case)
    if content_errors:
        return json.dumps({
            "status": "error",
            "message": (
                "Thesis content failed validation. Fix and resubmit:\n  - "
                + "\n  - ".join(content_errors)
            )
        })

    # ── Phase 4: enforce theme linkage + 3-criteria scorecard ───────────────
    # theme_id is required when active themes exist — the analyst must link each
    # new thesis to a user-approved Theme so analysis is framed within the right
    # investment narrative, not case-built from scratch.
    active_theme_ids = _get_active_theme_ids()
    if active_theme_ids:
        if theme_id is None:
            return json.dumps({
                "status": "error",
                "message": (
                    f"theme_id is required when active themes exist {active_theme_ids}. "
                    "Call get_active_themes() to see the available themes and their IDs, "
                    "then re-submit with the most relevant theme_id."
                ),
            })
        if theme_id not in active_theme_ids:
            return json.dumps({
                "status": "error",
                "message": (
                    f"theme_id={theme_id} is not an active theme. "
                    f"Valid active theme IDs: {active_theme_ids}. "
                    "Call get_active_themes() to get the current list."
                ),
            })

    if theme_id is not None:
        # When a theme is linked, the 3-criteria scorecard fields are mandatory.
        MIN_SCORECARD_CHARS = 80
        missing = []
        for field_value, field_label in [
            (positioning_vs_theme,
             "positioning_vs_theme (≥80 chars — specific competitive moat vs peers: "
             "technology, switching costs, margin trajectory, market share — NOT 'operates in the space')"),
            (execution_evidence,
             "execution_evidence (≥80 chars — cite actual 8-K/earnings data: "
             "beat/miss amount, guidance raised or cut, margin trend vs prior quarter)"),
            (valuation_assessment,
             "valuation_assessment (≥80 chars — cite real P/E, PEG, P/S from get_fundamentals; "
             "conclude 'discount / parity / premium vs sector' with the actual numbers)"),
        ]:
            if not field_value or len(field_value.strip()) < MIN_SCORECARD_CHARS:
                missing.append(field_label)
        if missing:
            return json.dumps({
                "status": "error",
                "message": (
                    "The 3-criteria scorecard is incomplete. Fix and resubmit:\n  - "
                    + "\n  - ".join(missing)
                ),
            })

    # ── Duplicate check ──────────────────────────────────────────────────────
    with get_session() as s:
        existing = (
            s.query(Thesis)
            .filter(
                Thesis.bot_id == BOT_ID,
                Thesis.ticker == ticker,
                Thesis.status.in_(["candidate", "waiting", "active"]),
            )
            .first()
        )
        if existing:
            return json.dumps({
                "status": "error",
                "message": (
                    f"An active/waiting thesis for {ticker} already exists (id={existing.id}, "
                    f"status={existing.status}). Update the existing thesis instead."
                )
            })

    # ── Persist thesis ───────────────────────────────────────────────────────
    # Only conviction >= IMMEDIATE_ENTRY_CONVICTION (5) bypasses the RSI/SMA gate.
    # Conviction 3-4 → 'waiting': must wait for technical confirmation.
    # This is conservative: it lets the technical layer catch bad calibrations
    # in narrative theses (a known failure mode — see ANET 2026-05-10 retrospective).
    thesis_status = "candidate" if conviction >= IMMEDIATE_ENTRY_CONVICTION else "waiting"
    size = _size_pct(conviction)

    with get_session() as s:
        thesis = Thesis(
            ticker=ticker,
            bot_id=BOT_ID,
            status=thesis_status,
            thesis_text=thesis_text.strip(),
            bull_case=bull_case.strip(),
            bear_case=bear_case.strip(),
            catalysts=catalysts or [],
            invalidates_if=invalidates_if,
            conviction=conviction,
            conviction_last_changed_at=None,
            consecutive_weakening_count=0,
            horizon_months=horizon_months,
            target_price_eur=target_price_eur,
            stop_price_eur=stop_price_eur,
            max_position_pct=size,
            # Phase 4 scorecard
            theme_id=theme_id,
            positioning_vs_theme=positioning_vs_theme.strip() if positioning_vs_theme else None,
            execution_evidence=execution_evidence.strip() if execution_evidence else None,
            valuation_assessment=valuation_assessment.strip() if valuation_assessment else None,
        )
        s.add(thesis)
        s.flush()
        thesis_id = thesis.id

        action_id = None
        if conviction >= IMMEDIATE_ENTRY_CONVICTION:
            # Conviction 5: propose immediate entry (user still approves).
            # Conviction 3-4: 'waiting' status — no action yet; strategy module
            # creates an 'open' action when RSI/SMA gate triggers.
            action = ThesisAction(
                thesis_id=thesis_id,
                action_type="open",
                size_pct=size,
                rationale=(
                    f"Nova tesi amb convicció {conviction}/5. "
                    f"Mida proposada: {size*100:.0f}% del capital del bot. "
                    f"Tesi: {thesis_text[:200]}"
                ),
                conviction_at_proposal=conviction,
                status="pending",
            )
            s.add(action)
            s.flush()
            action_id = action.id

        s.commit()

    msg = (
        f"Thesis created for {ticker} (id={thesis_id}, status={thesis_status}, "
        f"conviction={conviction})."
    )
    if action_id:
        msg += f" Open action proposed (id={action_id}, size={size*100:.0f}%) — awaiting user approval."
    else:
        msg += (
            f" Status='waiting': will propose entry when RSI/SMA gate triggers "
            f"(conviction {conviction} < {IMMEDIATE_ENTRY_CONVICTION})."
        )

    log.info("pm_tools.submit_thesis: %s", msg)
    return json.dumps({"status": "ok", "thesis_id": thesis_id, "action_id": action_id, "message": msg})


def submit_review(
    thesis_id: int,
    verdict: str,
    new_info_summary: str,
    conviction_after: int,
    notes: str = "",
    exit_rationale: str | None = None,
) -> str:
    """Record a daily thesis review and optionally propose an action.

    Guardrails enforced:
    - verdict must be 'intact' | 'strengthened' | 'weakening' | 'invalidated'
    - conviction can only drop by ≤ 1 step per week (throttle)
    - 'weakening' does NOT create an action card (informational only)
    - 'invalidated' → propose EXIT (but only after min_hold_days have elapsed)
    - exit_rationale must explicitly reference one of the thesis's invalidates_if conditions
    - 14-day minimum hold before any thesis-driven exit

    Returns JSON with {status, review_id, action_id, message}.
    """
    valid_verdicts = {"intact", "strengthened", "weakening", "invalidated"}
    if verdict not in valid_verdicts:
        return json.dumps({
            "status": "error",
            "message": f"verdict must be one of {valid_verdicts}, got '{verdict}'"
        })

    with get_session() as s:
        thesis = s.query(Thesis).filter(Thesis.id == thesis_id).first()
        if not thesis:
            return json.dumps({"status": "error", "message": f"Thesis {thesis_id} not found."})

        conviction_before = thesis.conviction
        now = _utcnow()

        # ── Conviction throttle ──────────────────────────────────────────────
        if conviction_after != conviction_before:
            if abs(conviction_after - conviction_before) > MAX_CONVICTION_STEP_PER_WEEK:
                return json.dumps({
                    "status": "error",
                    "message": (
                        f"Conviction change too large: {conviction_before} → {conviction_after}. "
                        f"Maximum change is {MAX_CONVICTION_STEP_PER_WEEK} step per week. "
                        "Split into multiple weekly reviews."
                    )
                })
            if thesis.conviction_last_changed_at:
                days_since_change = (now - _as_aware(thesis.conviction_last_changed_at)).days
                if days_since_change < 7:
                    return json.dumps({
                        "status": "error",
                        "message": (
                            f"Conviction was changed {days_since_change} days ago. "
                            "Must wait 7 days between conviction changes to prevent "
                            "short-term noise from swinging the thesis."
                        )
                    })

        # ── Weakening count tracking ─────────────────────────────────────────
        new_weakening_count = thesis.consecutive_weakening_count
        if verdict == "weakening":
            new_weakening_count += 1
        elif verdict in ("intact", "strengthened"):
            new_weakening_count = 0
        # 'invalidated' doesn't affect the counter

        # ── Hold floor ───────────────────────────────────────────────────────
        hold_days = (now - _as_aware(thesis.opened_at)).days
        action_id = None
        action_note = ""

        if verdict == "invalidated":
            if hold_days < MIN_HOLD_DAYS_BEFORE_EXIT:
                return json.dumps({
                    "status": "error",
                    "message": (
                        f"Cannot propose exit for {thesis.ticker}: only {hold_days} days since "
                        f"thesis opened (minimum is {MIN_HOLD_DAYS_BEFORE_EXIT} days). "
                        "If the situation is truly catastrophic, the trailing stop will handle it. "
                        "Downgrade conviction to 'weakening' and revisit next week."
                    )
                })

            # ── Exit rationale must cite an invalidates_if condition ─────────
            if not exit_rationale:
                return json.dumps({
                    "status": "error",
                    "message": (
                        "exit_rationale is required for 'invalidated' verdict. "
                        "You must explicitly cite which invalidates_if condition was met."
                    )
                })

            # Check that at least one invalidates_if string appears (partial match)
            conditions = thesis.invalidates_if or []
            cited = any(
                cond.lower()[:30] in exit_rationale.lower()
                for cond in conditions
            )
            if not cited and conditions:
                return json.dumps({
                    "status": "error",
                    "message": (
                        f"exit_rationale does not appear to cite any of the pre-written "
                        f"invalidation conditions: {conditions}. "
                        "The exit rationale must reference the specific condition that was met."
                    )
                })

        # ── Write review log ─────────────────────────────────────────────────
        review = ThesisReviewLog(
            thesis_id=thesis_id,
            new_info_summary=new_info_summary,
            conviction_before=conviction_before,
            conviction_after=conviction_after,
            verdict=verdict,
            notes=notes,
        )
        s.add(review)
        s.flush()
        review_id = review.id

        # ── Update thesis ────────────────────────────────────────────────────
        thesis.review_count += 1
        thesis.last_reviewed_at = now
        thesis.consecutive_weakening_count = new_weakening_count

        if conviction_after != conviction_before:
            thesis.conviction = conviction_after
            thesis.conviction_last_changed_at = now

        # ── Create action if warranted ───────────────────────────────────────
        if verdict == "invalidated":
            # Propose exit — user must still approve
            size = _size_pct(conviction_before)
            action = ThesisAction(
                thesis_id=thesis_id,
                action_type="exit",
                size_pct=size,
                rationale=exit_rationale or f"Thesis invalidated: {new_info_summary[:300]}",
                conviction_at_proposal=conviction_before,
                status="pending",
            )
            s.add(action)
            s.flush()
            action_id = action.id
            thesis.status = "invalidated"
            action_note = f" EXIT action proposed (id={action_id}) — awaiting user approval."

        elif verdict == "strengthened" and conviction_after > conviction_before:
            # Conviction rose: propose ADD
            size = _size_pct(conviction_after)
            action = ThesisAction(
                thesis_id=thesis_id,
                action_type="add",
                size_pct=size,
                rationale=f"Convicció augmentada {conviction_before}→{conviction_after}. {new_info_summary[:300]}",
                conviction_at_proposal=conviction_after,
                status="pending",
            )
            s.add(action)
            s.flush()
            action_id = action.id
            action_note = f" ADD action proposed (id={action_id}) — awaiting user approval."

        elif (verdict == "weakening"
              and new_weakening_count >= 5
              and conviction_after < conviction_before):
            # 5+ consecutive weakening reviews + conviction dropped: propose REDUCE
            size = _size_pct(conviction_after)
            action = ThesisAction(
                thesis_id=thesis_id,
                action_type="reduce",
                size_pct=size,
                rationale=(
                    f"5+ revisions consecutives de debilitament. "
                    f"Convicció reduïda {conviction_before}→{conviction_after}. "
                    f"{new_info_summary[:300]}"
                ),
                conviction_at_proposal=conviction_after,
                status="pending",
            )
            s.add(action)
            s.flush()
            action_id = action.id
            action_note = f" REDUCE action proposed (id={action_id}) — awaiting user approval."

        s.commit()

    msg = (
        f"Review logged for thesis {thesis_id} ({thesis.ticker}): "
        f"verdict={verdict}, conviction {conviction_before}→{conviction_after}, "
        f"weakening_count={new_weakening_count}."
    ) + action_note

    log.info("pm_tools.submit_review: %s", msg)
    return json.dumps({
        "status": "ok",
        "review_id": review_id,
        "action_id": action_id,
        "message": msg,
    })


# ── Tool definitions (JSON schemas for the agent loop) ────────────────────────

TOOL_DEFINITIONS: list[dict] = [
    {
        "name": "get_universe_tickers",
        "description": (
            "Returns the curated watchlist of 30-50 tickers that Claude evaluates "
            "for investment theses. Includes ticker, name, sector, and region. "
            "Call this first during Sunday candidate evaluation."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_active_theses",
        "description": (
            "Returns all theses that are currently active or waiting for a technical "
            "signal (status='active' or 'waiting'). Includes conviction, invalidation "
            "conditions, and last-reviewed date. Call this first during daily review."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_ticker_analysis",
        "description": (
            "Returns RSI(14) history and recent news headlines for a ticker in a single "
            "call. Use this to evaluate a candidate or review an active thesis. "
            "rsi_days controls how much RSI history to return (default 90); "
            "news_days controls the news lookback (default 30)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker":    {"type": "string", "description": "Stock ticker, e.g. MSFT, ASML.AS"},
                "rsi_days":  {"type": "integer", "description": "Days of RSI history. Default 90.", "default": 90},
                "news_days": {"type": "integer", "description": "Days of news to fetch. Default 30.", "default": 30},
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "get_market_context_today",
        "description": (
            "Returns S&P 500 (SXR8.DE) RSI and price over the last 30 days. "
            "Use this to understand the current market regime when evaluating candidates."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_fundamentals",
        "description": (
            "Returns REAL fundamental metrics for a ticker from yfinance: P/E, margins "
            "(operating, gross, profit), market cap, revenue growth YoY, debt ratios, "
            "52-week high/low, sector. CRITICAL: any numerical claim about the company "
            "in your thesis (margins, P/E, growth rates, market cap) MUST come from a "
            "tool call like this one. Do NOT cite numbers from memory — your training "
            "data is months/years stale and will be wrong. Always call this before "
            "submitting a thesis that mentions any fundamental figure."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Stock ticker"},
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "get_analyst_targets",
        "description": (
            "Returns REAL analyst price targets and consensus rating for a ticker "
            "(target mean/high/low/median, upside-to-mean %, recommendation key, "
            "number of analysts). Use this instead of citing 'Wall Street targets' "
            "from memory. The 'upside_to_mean_pct' field gives you the actual upside "
            "calculation — do not compute your own (recent failure: bot computed +370% "
            "when actual was +27%, off by an order of magnitude)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Stock ticker"},
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "get_recent_8k_filings",
        "description": (
            "Returns recent SEC 8-K filings for a US-listed ticker (US ONLY — "
            "European tickers like ASML.AS, SAP.DE return a 'not US-listed' "
            "message and you should rely on get_ticker_analysis news headlines "
            "for them). For each 8-K: filing date, SEC item codes (2.02 = "
            "Results of Operations, 7.01 = Reg FD Disclosure / guidance, "
            "8.01 = Other Events), and when the filing is an earnings release, "
            "the actual press release text including any forward guidance "
            "language ('We expect Q3 revenue of $2.8B...'). This is the most "
            "authoritative source you have for management guidance — way "
            "better than Yahoo News headlines. Use this whenever the next "
            "earnings date is in the recent past, OR when news headlines "
            "mention 'guidance', 'reaffirms', 'raises', 'lowers'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "US stock ticker"},
                "days":   {"type": "integer", "description": "Lookback window in days. Default 90.", "default": 90},
                "limit":  {"type": "integer", "description": "Max filings to return. Default 5.", "default": 5},
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "get_recent_earnings_history",
        "description": (
            "Returns the last N quarters of EPS estimates vs reported actuals for "
            "a ticker, plus aggregate beat/miss stats and average surprise %. Use "
            "this to assess management's track record: a company that has beaten "
            "estimates 7 of 8 quarters with +9% average surprise has real momentum; "
            "one that misses regularly does not. The next upcoming earnings date "
            "appears with an estimate but no reported value yet — useful for the "
            "catalysts field. Default 8 quarters (2 years)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Stock ticker"},
                "quarters": {
                    "type": "integer",
                    "description": "How many quarters of history to return. Default 8.",
                    "default": 8,
                },
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "get_active_themes",
        "description": (
            "Returns user-approved investment Themes (status=active). MUST call during "
            "Sunday candidate scans to resolve theme_id before submit_thesis — theme_id "
            "is required when active themes exist. Returns id, name, potential, "
            "importance, candidate_tickers, and a short excerpt of narrative_text."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "submit_thesis",
        "description": (
            "Validate and persist a new investment thesis for a ticker. "
            "For conviction = 5: immediately proposes an 'open' action for user approval. "
            "For conviction 3-4: creates a 'waiting' thesis — entry proposed only when "
            "RSI/SMA conditions align. For conviction ≤ 2: rejected (too uncertain). "
            "All fields validated: bear_case ≥ 100 chars, ≥ 2 invalidation conditions, "
            "horizon_months ≥ 3. CONTENT VALIDATION (rejected if violated): no Cramer/'Wall Street says'/"
            "'analysts say' phrasings without specific source; no percentage claims > 100% "
            "(likely arithmetic error). Duplicate active theses for the same ticker are rejected."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker":         {"type": "string", "description": "Stock ticker"},
                "conviction":     {"type": "integer", "description": "1-5. Must be ≥ 3 to create a thesis."},
                "horizon_months": {"type": "integer", "description": "Expected holding period in months. Must be ≥ 3."},
                "thesis_text":    {"type": "string", "description": "2-3 sentence summary of the investment case."},
                "bull_case":      {"type": "string", "description": "What makes this thesis work."},
                "bear_case":      {"type": "string", "description": "Devil's advocate: what could go wrong. Must be ≥ 100 chars."},
                "invalidates_if": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "≥ 2 specific, measurable kill conditions. E.g. 'revenue guidance < +20% YoY'.",
                },
                "catalysts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "event":            {"type": "string"},
                            "expected_date":    {"type": "string"},
                            "expected_outcome": {"type": "string"},
                        },
                    },
                    "description": "Upcoming events that could confirm or invalidate the thesis.",
                },
                "target_price_eur": {"type": "number", "description": "Optional price target in EUR."},
                "stop_price_eur":   {"type": "number", "description": "Optional stop price in EUR."},
                "theme_id": {
                    "type": "integer",
                    "description": (
                        "Criterion 1 — Theme Fit: ID of the active theme this stock plays into. "
                        "Required when active themes exist. Call get_active_themes() first to see IDs."
                    ),
                },
                "positioning_vs_theme": {
                    "type": "string",
                    "description": (
                        "Criterion 2 — Unique competitive moat vs peers within the theme: technology "
                        "lead, switching costs, margin trajectory, market share evidence from 8-K. "
                        "≥80 chars. NOT 'operates in the space' — cite what makes THIS company better "
                        "than its theme peers (e.g. NRR >120%, gross margin 10pp above peers, "
                        "proprietary patents, 4+ quarters of guidance beats)."
                    ),
                },
                "execution_evidence": {
                    "type": "string",
                    "description": (
                        "Criterion 3a — Execution quality from get_recent_8k_filings + "
                        "get_recent_earnings_history: beat/miss amount, guidance raised/cut, "
                        "margin trend vs prior quarter. ≥80 chars."
                    ),
                },
                "valuation_assessment": {
                    "type": "string",
                    "description": (
                        "Criterion 3b — Valuation from get_fundamentals: cite forward P/E, PEG "
                        "(< 1 = cheap for growth, > 2 = expensive), P/S TTM. Compare vs a peer or "
                        "sector average. Conclude with: 'cotitza a descompte / paritat / prima "
                        "respecte sector'. ≥80 chars."
                    ),
                },
            },
            "required": [
                "ticker", "conviction", "horizon_months",
                "thesis_text", "bull_case", "bear_case", "invalidates_if",
            ],
        },
    },
    {
        "name": "submit_review",
        "description": (
            "Record a daily review for an active thesis and optionally propose an action. "
            "verdict must be: 'intact' (no change), 'strengthened' (new positive evidence), "
            "'weakening' (concerning but not invalidated — NO action card created), or "
            "'invalidated' (kill condition explicitly met → EXIT proposed). "
            "Guardrails: conviction throttle (max 1 step/week), 14-day hold floor before exit, "
            "exit_rationale must cite one of the thesis's invalidates_if conditions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "thesis_id":       {"type": "integer", "description": "ID from get_active_theses."},
                "verdict":         {"type": "string",  "description": "'intact' | 'strengthened' | 'weakening' | 'invalidated'"},
                "new_info_summary":{"type": "string",  "description": "Summary of new price action + news since last review."},
                "conviction_after":{"type": "integer", "description": "Updated conviction (1-5). Can differ by max 1 from current."},
                "notes":           {"type": "string",  "description": "Optional additional notes."},
                "exit_rationale":  {"type": "string",  "description": "Required if verdict='invalidated'. Must cite the specific invalidates_if condition met."},
            },
            "required": ["thesis_id", "verdict", "new_info_summary", "conviction_after"],
        },
    },
]


# ── Dispatcher ────────────────────────────────────────────────────────────────

def dispatch(tool_name: str, tool_input: dict) -> str:
    """Route a tool call to the right tool, catching ALL exceptions.

    A single tool failure (e.g. yfinance returns no data, SEC EDGAR is down)
    must NEVER crash the agent loop. Wrap every dispatch in try/except and
    return the error as a JSON tool result so Claude can see it, react to
    it (e.g. skip that ticker, try a different tool), and continue.
    """
    try:
        return _dispatch_inner(tool_name, tool_input)
    except Exception as e:
        log.exception("pm_tools.dispatch: tool=%s failed", tool_name)
        return json.dumps({
            "error": f"Tool '{tool_name}' raised {type(e).__name__}: {e}",
            "hint": "Try a different ticker, a different tool, or skip this candidate.",
        })


def _dispatch_inner(tool_name: str, tool_input: dict) -> str:
    """The actual routing — kept separate so the wrapper can catch everything."""
    if tool_name == "get_universe_tickers":
        return get_universe_tickers()
    if tool_name == "get_active_theses":
        return get_active_theses()
    if tool_name == "get_ticker_analysis":
        return get_ticker_analysis(
            tool_input["ticker"],
            tool_input.get("rsi_days", 90),
            tool_input.get("news_days", 30),
        )
    if tool_name == "get_market_context_today":
        return get_market_context_today()
    if tool_name == "get_fundamentals":
        return get_fundamentals(tool_input["ticker"])
    if tool_name == "get_analyst_targets":
        return get_analyst_targets(tool_input["ticker"])
    if tool_name == "get_recent_earnings_history":
        return get_recent_earnings_history(
            tool_input["ticker"],
            tool_input.get("quarters", 8),
        )
    if tool_name == "get_recent_8k_filings":
        return get_recent_8k_filings(
            tool_input["ticker"],
            tool_input.get("days", 90),
            tool_input.get("limit", 5),
        )
    if tool_name == "get_active_themes":
        return get_active_themes_for_analyst()
    if tool_name == "submit_thesis":
        return submit_thesis(
            ticker=tool_input["ticker"],
            conviction=tool_input["conviction"],
            horizon_months=tool_input["horizon_months"],
            thesis_text=tool_input["thesis_text"],
            bull_case=tool_input["bull_case"],
            bear_case=tool_input["bear_case"],
            invalidates_if=tool_input["invalidates_if"],
            catalysts=tool_input.get("catalysts", []),
            target_price_eur=tool_input.get("target_price_eur"),
            stop_price_eur=tool_input.get("stop_price_eur"),
            theme_id=tool_input.get("theme_id"),
            positioning_vs_theme=tool_input.get("positioning_vs_theme"),
            execution_evidence=tool_input.get("execution_evidence"),
            valuation_assessment=tool_input.get("valuation_assessment"),
        )
    if tool_name == "submit_review":
        return submit_review(
            thesis_id=tool_input["thesis_id"],
            verdict=tool_input["verdict"],
            new_info_summary=tool_input["new_info_summary"],
            conviction_after=tool_input["conviction_after"],
            notes=tool_input.get("notes", ""),
            exit_rationale=tool_input.get("exit_rationale"),
        )
    return json.dumps({"error": f"Unknown tool: {tool_name}"})
