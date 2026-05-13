"""Tools for the Strategist agent (Phase 4 of the AI Trading System).

The Strategist proposes durable investment themes (2-3 year horizon) across
industries and tech. Themes are user-approved and become priors that the
Analyst agent (portfolio_manager.py) uses to evaluate stocks.

Two modes:
  - propose_new_themes: Strategist proposes 4-5 brand-new themes
  - review_existing_themes: Strategist surfaces informational observations
    on active themes (NEVER modifies them — only the user can edit ratings)

Tools exposed to Claude:
  - get_universe_with_sectors()        — the 67-ticker watchlist + theme tags
  - get_active_themes()                — what themes already exist (don't repeat)
  - get_market_context_today()         — broad-market RSI/price (reused from pm_tools)
  - get_fundamentals(ticker)           — reused from pm_tools
  - get_recent_8k_filings(ticker)      — reused from pm_tools
  - submit_theme_proposal(...)         — persist a proposed Theme
  - submit_theme_review(...)           — log a ThemeReviewNote (informational)

Validation lives in submit_*: bear-style guardrails (no Cramer/etc.), invalidator
specificity, candidate tickers must exist in the universe, importance/potential
are integers 1-5.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from core.db import Theme, ThemeReviewNote, get_session

# Reuse Phase 2 tools — same signatures, same JSON output
from agents.pm_tools import (
    get_universe_tickers as _get_universe_raw,
    get_market_context_today,
    get_fundamentals,
    get_recent_8k_filings,
    _validate_content,        # forbidden patterns + math sanity
)

log = logging.getLogger(__name__)

MIN_THEMES_PER_RUN = 1
MAX_THEMES_PER_RUN = 5
MIN_CANDIDATES_PER_THEME = 3
MAX_CANDIDATES_PER_THEME = 12
MIN_INVALIDATORS = 2


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── Tool implementations ──────────────────────────────────────────────────────

def get_universe_with_sectors() -> str:
    """Return the curated watchlist with sector + theme tags.

    Same data as pm_tools.get_universe_tickers but surfaced specifically for
    the Strategist so it knows what tickers it can recommend as candidates.
    Tickers proposed in a theme MUST exist in this list — submit_theme_proposal
    will reject unknown tickers.
    """
    return _get_universe_raw()


def get_active_themes() -> str:
    """Return all themes currently in 'active' or 'proposed' status.

    The Strategist uses this to avoid duplicate proposals. If a similar theme
    already exists, propose a different one instead.
    """
    with get_session() as s:
        themes = (
            s.query(Theme)
            .filter(Theme.status.in_(["proposed", "active"]))
            .order_by(Theme.proposed_at.desc())
            .all()
        )
        rows = [
            {
                "id": t.id,
                "name": t.name,
                "status": t.status,
                "horizon_years": t.horizon_years,
                "importance": t.importance,
                "potential": t.potential,
                "candidate_tickers": t.candidate_tickers,
                "narrative_text": t.narrative_text,
                "invalidators": t.invalidators,
            }
            for t in themes
        ]

    if not rows:
        return json.dumps({"message": "No active or proposed themes."})
    return json.dumps(rows)


def submit_theme_proposal(
    name: str,
    narrative_text: str,
    horizon_years: int,
    importance: int,
    potential: int,
    candidate_tickers: list[str],
    invalidators: list[str],
) -> str:
    """Validate and persist a new Theme (status='proposed', user must approve).

    Validation:
      - name non-empty
      - narrative_text passes content validation (no Cramer/etc., no absurd %)
      - horizon_years in 2-5
      - importance and potential each in 1-5
      - len(candidate_tickers) in [MIN_CANDIDATES, MAX_CANDIDATES]
      - all candidate_tickers exist in the universe
      - len(invalidators) >= MIN_INVALIDATORS
      - no duplicate theme name in active/proposed status
    """
    if not name or not name.strip():
        return json.dumps({"status": "error", "message": "name cannot be empty"})

    if not 2 <= horizon_years <= 5:
        return json.dumps({
            "status": "error",
            "message": f"horizon_years must be 2-5, got {horizon_years}",
        })

    if not 1 <= importance <= 5:
        return json.dumps({
            "status": "error",
            "message": f"importance must be 1-5, got {importance}",
        })

    if not 1 <= potential <= 5:
        return json.dumps({
            "status": "error",
            "message": f"potential must be 1-5, got {potential}",
        })

    if not narrative_text or len(narrative_text.strip()) < 200:
        return json.dumps({
            "status": "error",
            "message": (
                f"narrative_text too short ({len(narrative_text.strip())} chars). "
                "Min 200 — write 2-3 substantive paragraphs explaining the macro/tech argument."
            ),
        })

    # Reuse the Phase 2 content validator (forbidden Cramer/'Wall Street says'/etc.,
    # absurd % claims). Pass narrative_text in all three slots so it's checked.
    content_errors = _validate_content(narrative_text, narrative_text, narrative_text)
    if content_errors:
        return json.dumps({
            "status": "error",
            "message": "Narrative failed content validation:\n  - " + "\n  - ".join(content_errors),
        })

    if not (MIN_CANDIDATES_PER_THEME <= len(candidate_tickers) <= MAX_CANDIDATES_PER_THEME):
        return json.dumps({
            "status": "error",
            "message": (
                f"candidate_tickers must have {MIN_CANDIDATES_PER_THEME}-{MAX_CANDIDATES_PER_THEME} "
                f"entries, got {len(candidate_tickers)}"
            ),
        })

    if len(invalidators) < MIN_INVALIDATORS:
        return json.dumps({
            "status": "error",
            "message": (
                f"invalidators must have ≥ {MIN_INVALIDATORS} specific conditions, "
                f"got {len(invalidators)}"
            ),
        })

    # Verify all candidate tickers exist in the universe
    universe_data = json.loads(_get_universe_raw())
    universe_set = {t["ticker"] for t in universe_data}
    unknown = [t for t in candidate_tickers if t not in universe_set]
    if unknown:
        return json.dumps({
            "status": "error",
            "message": (
                f"These tickers are not in the universe: {unknown}. "
                "Use only tickers from get_universe_with_sectors. To add a new "
                "ticker to the universe, the user must edit config/ai_thesis_universe.yaml."
            ),
        })

    # Reject duplicates by name (case-insensitive)
    with get_session() as s:
        dup = (
            s.query(Theme)
            .filter(Theme.status.in_(["proposed", "active"]))
            .all()
        )
        for d in dup:
            if d.name.strip().lower() == name.strip().lower():
                return json.dumps({
                    "status": "error",
                    "message": (
                        f"Theme '{d.name}' already exists (id={d.id}, status={d.status}). "
                        "Propose a different theme."
                    ),
                })

        theme = Theme(
            name=name.strip(),
            narrative_text=narrative_text.strip(),
            horizon_years=horizon_years,
            importance=importance,
            potential=potential,
            candidate_tickers=candidate_tickers,
            invalidators=invalidators,
            status="proposed",
            created_by="strategist",
        )
        s.add(theme)
        s.flush()
        theme_id = theme.id
        s.commit()

    msg = (
        f"Theme '{name}' proposed (id={theme_id}). "
        f"Importance={importance}/5, potential={potential}/5, "
        f"horizon={horizon_years}y, {len(candidate_tickers)} candidates. "
        "Awaiting user approval."
    )
    log.info("strategist_tools.submit_theme_proposal: %s", msg)
    return json.dumps({"status": "ok", "theme_id": theme_id, "message": msg})


def submit_theme_review(
    theme_id: int,
    observation: str,
    recommendation: str,
    severity: str = "info",
) -> str:
    """Log an informational note about an existing active theme.

    Does NOT modify the theme. Surfaces in the dashboard as an unread note
    that the user can act on (edit theme, archive it) or dismiss.

    severity: 'info' | 'warning' | 'critical'
    """
    if severity not in {"info", "warning", "critical"}:
        return json.dumps({
            "status": "error",
            "message": f"severity must be 'info', 'warning', or 'critical', got '{severity}'",
        })

    if not observation or not observation.strip():
        return json.dumps({"status": "error", "message": "observation cannot be empty"})

    if not recommendation or not recommendation.strip():
        return json.dumps({"status": "error", "message": "recommendation cannot be empty"})

    # Validate observation/recommendation prose against the same forbidden patterns
    content_errors = _validate_content(observation, recommendation, observation + recommendation)
    if content_errors:
        return json.dumps({
            "status": "error",
            "message": "Note failed content validation:\n  - " + "\n  - ".join(content_errors),
        })

    with get_session() as s:
        theme = s.query(Theme).filter(Theme.id == theme_id).first()
        if not theme:
            return json.dumps({"status": "error", "message": f"Theme {theme_id} not found"})
        if theme.status != "active":
            return json.dumps({
                "status": "error",
                "message": f"Theme {theme_id} is '{theme.status}', not active. Reviews only apply to active themes.",
            })

        note = ThemeReviewNote(
            theme_id=theme_id,
            observation=observation.strip(),
            recommendation=recommendation.strip(),
            severity=severity,
            status="unread",
        )
        s.add(note)
        s.flush()
        note_id = note.id
        s.commit()

    msg = (
        f"Review note logged for theme {theme_id} ({theme.name}): "
        f"severity={severity}. {observation[:120]}"
    )
    log.info("strategist_tools.submit_theme_review: %s", msg)
    return json.dumps({"status": "ok", "note_id": note_id, "message": msg})


# ── Tool definitions (JSON schemas for Claude) ────────────────────────────────

TOOL_DEFINITIONS: list[dict] = [
    {
        "name": "get_universe_with_sectors",
        "description": (
            "Returns the curated watchlist (60+ tickers) with sector + theme tags. "
            "Tickers in your theme proposals MUST come from this list — "
            "submit_theme_proposal rejects unknown tickers."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_active_themes",
        "description": (
            "Returns themes currently active or proposed (awaiting user approval). "
            "ALWAYS call this first — your job is to propose NEW themes that "
            "complement existing ones, not duplicate them."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_market_context_today",
        "description": (
            "Returns S&P 500 (SXR8.DE) RSI and price over the last 30 days. "
            "Useful to anchor themes in the current market regime."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_fundamentals",
        "description": (
            "Real fundamental metrics for a ticker from yfinance. Use this when "
            "you want to verify that a candidate ticker actually fits the theme "
            "(e.g. is it really exposed to the trend you're proposing)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}},
            "required": ["ticker"],
        },
    },
    {
        "name": "get_recent_8k_filings",
        "description": (
            "Recent SEC 8-K filings (US tickers only) including earnings press "
            "releases with management commentary. Use to verify how a ticker is "
            "actually positioned vs the theme you're proposing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "days":   {"type": "integer", "default": 90},
                "limit":  {"type": "integer", "default": 5},
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "submit_theme_proposal",
        "description": (
            "Validate and persist a proposed theme. Status starts as 'proposed' — "
            "user must approve via dashboard before it becomes active. Required: "
            "name (unique), narrative_text (≥200 chars, 2-3 paragraphs), "
            "horizon_years (2-5), importance (1-5), potential (1-5), "
            "3-12 candidate_tickers (all from universe), ≥2 specific invalidators. "
            "Forbidden: Cramer/'Wall Street says' phrasings, claims > 100%."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Theme name (e.g. 'Hyperscaler power demand surge')"},
                "narrative_text": {"type": "string", "description": "2-3 paragraphs explaining the macro/tech argument."},
                "horizon_years": {"type": "integer", "description": "2-5 years."},
                "importance": {"type": "integer", "description": "1-5: how big is this shift?"},
                "potential": {"type": "integer", "description": "1-5: how much upside if it plays out?"},
                "candidate_tickers": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "3-12 tickers from the universe.",
                },
                "invalidators": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "≥2 specific, measurable conditions that would falsify the theme.",
                },
            },
            "required": [
                "name", "narrative_text", "horizon_years", "importance",
                "potential", "candidate_tickers", "invalidators",
            ],
        },
    },
    {
        "name": "submit_theme_review",
        "description": (
            "Log an informational observation about an active theme. Does NOT "
            "modify the theme — only the user edits ratings. The note appears "
            "in the dashboard for the user to read/dismiss/act on. Use for: "
            "weakening narratives ('Crane delay'), strengthening narratives, "
            "competitor moves, regulatory developments. severity: info | warning | critical."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "theme_id":       {"type": "integer", "description": "ID from get_active_themes."},
                "observation":    {"type": "string",  "description": "What you noticed (specific, sourced)."},
                "recommendation": {"type": "string",  "description": "Suggested action ('archive', 'lower importance', 'monitor', etc.)"},
                "severity":       {"type": "string",  "description": "info | warning | critical"},
            },
            "required": ["theme_id", "observation", "recommendation"],
        },
    },
]


# ── Dispatcher ────────────────────────────────────────────────────────────────

def dispatch(tool_name: str, tool_input: dict) -> str:
    """Route a strategist tool call. Wrapped in try/except so a single tool
    failure (e.g. yfinance rate limit) returns a JSON error to Claude rather
    than crashing the agent loop.
    """
    try:
        return _dispatch_inner(tool_name, tool_input)
    except Exception as e:
        log.exception("strategist_tools.dispatch: tool=%s failed", tool_name)
        return json.dumps({
            "error": f"Tool '{tool_name}' raised {type(e).__name__}: {e}",
            "hint": "Try a different ticker or tool, or skip this candidate.",
        })


def _dispatch_inner(tool_name: str, tool_input: dict) -> str:
    if tool_name == "get_universe_with_sectors":
        return get_universe_with_sectors()
    if tool_name == "get_active_themes":
        return get_active_themes()
    if tool_name == "get_market_context_today":
        return get_market_context_today()
    if tool_name == "get_fundamentals":
        return get_fundamentals(tool_input["ticker"])
    if tool_name == "get_recent_8k_filings":
        return get_recent_8k_filings(
            tool_input["ticker"],
            tool_input.get("days", 90),
            tool_input.get("limit", 5),
        )
    if tool_name == "submit_theme_proposal":
        return submit_theme_proposal(
            name=tool_input["name"],
            narrative_text=tool_input["narrative_text"],
            horizon_years=tool_input["horizon_years"],
            importance=tool_input["importance"],
            potential=tool_input["potential"],
            candidate_tickers=tool_input["candidate_tickers"],
            invalidators=tool_input["invalidators"],
        )
    if tool_name == "submit_theme_review":
        return submit_theme_review(
            theme_id=tool_input["theme_id"],
            observation=tool_input["observation"],
            recommendation=tool_input["recommendation"],
            severity=tool_input.get("severity", "info"),
        )
    return json.dumps({"error": f"Unknown tool: {tool_name}"})
