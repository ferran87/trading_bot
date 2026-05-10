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

CONVICTION_MULT = {5: 1.5, 4: 1.2, 3: 1.0, 2: 0.8, 1: 0.6}
BASE_PCT = 0.10    # 10% of bot capital
MAX_PCT  = 0.15    # hard cap regardless of conviction


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _size_pct(conviction: int) -> float:
    raw = BASE_PCT * CONVICTION_MULT.get(conviction, 1.0)
    return round(min(raw, MAX_PCT), 4)


# ── Public tool functions ─────────────────────────────────────────────────────

def get_universe_tickers() -> str:
    """Return the curated watchlist with sector tags."""
    from pathlib import Path
    import yaml

    cfg_path = Path(__file__).parents[1] / "config" / "ai_thesis_universe.yaml"
    with open(cfg_path) as f:
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
                "thesis_text":   t.thesis_text,
                "invalidates_if": t.invalidates_if,
                "catalysts":     t.catalysts,
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
    thesis_status = "candidate" if conviction >= 4 else "waiting"
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
        )
        s.add(thesis)
        s.flush()
        thesis_id = thesis.id

        action_id = None
        if conviction >= 4:
            # High conviction: propose immediate entry (user still approves)
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
        msg += " Status='waiting': will propose entry when RSI/SMA gate triggers (conviction 3)."

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
                days_since_change = (now - thesis.conviction_last_changed_at).days
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
        hold_days = (now - thesis.opened_at).days
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
        "name": "submit_thesis",
        "description": (
            "Validate and persist a new investment thesis for a ticker. "
            "For conviction ≥ 4: immediately proposes an 'open' action for user approval. "
            "For conviction = 3: creates a 'waiting' thesis — entry is proposed only when "
            "RSI/SMA conditions align. For conviction ≤ 2: rejected (too uncertain). "
            "All fields are validated: bear_case ≥ 100 chars, ≥ 2 invalidation conditions, "
            "horizon_months ≥ 3. Duplicate active theses for the same ticker are rejected."
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
    """Route a tool call from the agent loop to the correct implementation."""
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
