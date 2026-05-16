"""Phase 6 — pure-function validators for the AI Thesis Bot.

Extracted into their own module so they can be unit-tested without spinning
up the full ``pm_tools`` import graph (which pulls in Anthropic, yfinance,
SQLAlchemy, etc.).

Three problems these fix:

1. **PEG drift.** The bot used to compute PEG in prose as
   ``forward_pe / growth_pct_as_int`` (e.g. ``19.8 / 73 = 0.27``).  The
   correct formula divides by the integer percentage value (so ``27.4 / 24.9
   = 1.10``), NOT by the decimal fraction.  :func:`compute_peg_safely` is
   the single source of truth.

2. **Digit fabrication.** Bot quoted ratios from memory of the tool call
   rather than from the tool's actual return value.
   :func:`validate_no_invented_digits` blocks any digit-bearing token in a
   narrative field that isn't in the allowed-displays set (the union of
   ``valuation_snapshot`` and ``peer_snapshot`` display strings for the
   thesis's ticker).

3. **Soundbite contamination.** Cramer / "the street" / "smart money"
   quotes pasted as bull-case evidence.
   :func:`check_forbidden_soundbites` rejects them at submit time.

:func:`percentile_rank_in_peers` is the helper used by the conviction
hard-cap: a ticker priced in the upper half of its industry peer set
cannot earn conviction ≥ 4.
"""
from __future__ import annotations

import re
from typing import Any

# ── PEG ───────────────────────────────────────────────────────────────────────

def compute_peg_safely(forward_pe: float | None,
                       growth_decimal: float | None) -> float | None:
    """Correct PEG: ``forward_pe / (growth_decimal * 100)``.

    The denominator is the long-term consensus growth rate expressed as a
    decimal (e.g. ``0.249`` for 24.9%).  We multiply by 100 because PEG is
    conventionally ``P/E divided by growth-as-percentage-integer`` (so 27/25
    = 1.08, not 27/0.25 = 108).

    Returns ``None`` when either input is missing or growth is non-positive.
    The bot is forbidden from claiming a PEG when this returns None.
    """
    if forward_pe is None or growth_decimal is None:
        return None
    if growth_decimal <= 0:
        return None
    growth_pct = growth_decimal * 100.0
    return forward_pe / growth_pct


# ── Digit fabrication ─────────────────────────────────────────────────────────

# Matches digit-bearing tokens we care about: dollar amounts, percentages,
# multiples (e.g. 27.4x), bare numbers with optional B/M/T/K magnitude.
# Examples that match:
#   $235.74   $5.7T   $80B   25%   24.9%   27.4x   1.10   1.7B   $80-83B
_NUMERIC_TOKEN = re.compile(
    r"""
    \$?                              # optional $
    \d+                              # at least one digit
    (?:[.,]\d+)?                     # optional decimal part (US or EU comma)
    (?:                              # optional unit suffix:
        \s*%                         #   percentage
      | \s*[xX]                      #   multiple
      | \s*[BMTKbmtk](?:n|illion)?   #   B / Bn / Billion / M / Million / T / K
    )?
    """,
    re.VERBOSE,
)

# Structural tokens — number-bearing patterns that are identifiers, not ratios.
# These are stripped from the text BEFORE the numeric-token scan, so the
# digits inside them (e.g. "1" in "Q1") never trigger a fabrication error.
_STRUCTURAL_TOKEN = re.compile(
    r"""
    \b(?:
        \d{4}                     # year: 2024, 2026
      | Q[1-4]                    # quarter: Q1..Q4
      | \d+(?:\.\d+)?\s*nm        # process node: 3nm, 5.5nm, "3 nm"
      | \d+\s*G                   # generation: 4G, 5G
      | 8\s*-?\s*K                # SEC filing: 8-K, 8 K, 8K
      | 10\s*-?\s*[QK]            # SEC filings: 10-Q, 10K
    )\b
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _normalise_display(s: str) -> str:
    """Normalise a display string for comparison: lowercase, strip whitespace."""
    return s.strip().lower().replace(" ", "")


def validate_no_invented_digits(
    text: str | None,
    allowed_displays: set[str],
) -> str | None:
    """Return None when every numeric token in ``text`` is allowed, else an
    actionable error message naming the first offending token.

    A token is "allowed" when its normalised form equals the normalised form
    of any string in ``allowed_displays`` OR when it matches one of the
    structural exemption patterns (year, quarter, filing type, process node).

    ``allowed_displays`` is built by the caller from the snapshot dicts;
    typically the union of every ``"display"`` field in ``valuation_snapshot``
    and ``peer_snapshot`` for the thesis's ticker.
    """
    if not text:
        return None
    # 1. Scrub structural tokens (Q1, 8-K, 2026, 3nm, ...) so the digits inside
    #    them don't trigger fabrication errors.
    scrubbed = _STRUCTURAL_TOKEN.sub(" ", text)
    # 2. Tokenise the remaining text for numeric tokens.
    allowed_norm = {_normalise_display(s) for s in allowed_displays}
    allowed_norm_no_dollar = {a.lstrip("$") for a in allowed_norm}
    for raw in _NUMERIC_TOKEN.findall(scrubbed):
        token = raw.strip()
        norm = _normalise_display(token)
        if not norm or norm == "$" or not any(c.isdigit() for c in norm):
            continue
        if norm in allowed_norm:
            continue
        # Try without the $ prefix (snapshot may have "$80B"; bot may write "80B")
        if norm.lstrip("$") in allowed_norm_no_dollar:
            continue
        return (
            f"unsourced numeric token '{token}' — must come from "
            f"get_fundamentals / get_peer_metrics snapshot or be a structural "
            f"token (year, Q1-Q4, 8-K, 10-Q, NnM process)"
        )
    return None


# ── Forbidden soundbites ──────────────────────────────────────────────────────

_FORBIDDEN_SOUNDBITES = [
    re.compile(r"\bcramer\b",            re.I),
    re.compile(r"\bjim\s+cramer\b",      re.I),
    re.compile(r"\bthe\s+street\b",      re.I),
    re.compile(r"\bsmart\s+money\b",     re.I),
    re.compile(r"\banalist[ae]s?\s+unàn", re.I),
    re.compile(r"\bunanimous\s+analyst", re.I),
    re.compile(r"\bconsens\s+analista\s+és\s+unànim", re.I),
    re.compile(r"\beveryone\s+agree",    re.I),
    re.compile(r"\btothom\s+creu\b",     re.I),
    re.compile(r"\bbuy\s+the\s+dip\b",   re.I),
    re.compile(r"\bto\s+the\s+moon\b",   re.I),
]


def check_forbidden_soundbites(text: str | None) -> str | None:
    """Return None when ``text`` is clean, else an error naming the soundbite.

    These phrases are vibes, not signal — pasting them into a bull/bear case
    correlates with the worst theses Claude has produced (e.g. Cramer-backed
    CEG, NVDA).  Block at submit time.
    """
    if not text:
        return None
    for pat in _FORBIDDEN_SOUNDBITES:
        m = pat.search(text)
        if m:
            return f"forbidden soundbite '{m.group(0)}'"
    return None


# ── Percentile rank in peer set ───────────────────────────────────────────────

def percentile_rank_in_peers(
    peers: list[dict[str, Any]],
    metric: str,
    ticker: str,
) -> float:
    """Return the percentile rank of ``ticker`` for ``metric`` in ``peers``.

    Higher value = more expensive (e.g. forward P/E percentile of 0.75 means
    the ticker is more expensive than 75% of its peers).  Returns ``-1.0`` if
    the ticker isn't in the peer set.

    The metric field can be a flat number or a nested ``{"value": x, "display":
    "..."}`` dict (matching the snapshot shape).  Peers whose metric is None
    are skipped.
    """
    def _val(peer: dict) -> float | None:
        m = peer.get(metric)
        if m is None:
            return None
        if isinstance(m, dict):
            v = m.get("value")
            return float(v) if v is not None else None
        return float(m)

    # Find the target ticker's value
    target = next((p for p in peers if p.get("ticker") == ticker), None)
    if target is None:
        return -1.0
    target_val = _val(target)
    if target_val is None:
        return -1.0

    # Collect values from peers with non-None metric (including the target)
    values = [v for p in peers if (v := _val(p)) is not None]
    if not values:
        return -1.0

    # Percentile = fraction of values strictly less than target's value
    below = sum(1 for v in values if v < target_val)
    return below / len(values)
