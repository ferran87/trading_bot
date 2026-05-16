"""Shared dashboard helpers.

Small utilities used by multiple Streamlit tabs.  Previously each tab defined
its own copy (``_utcnow`` was in both ``thesis_tab`` and ``themes_tab``; ``_md``
lived only in ``thesis_tab`` even though ``themes_tab`` and ``strategy_lab_tab``
display the same kind of agent-generated text and need the same sanitisation).
"""
from __future__ import annotations

from datetime import datetime, timezone

# ── Time ──────────────────────────────────────────────────────────────────────

def _utcnow() -> datetime:
    """Return the current UTC datetime (timezone-aware)."""
    return datetime.now(timezone.utc)


# ── Agent-text sanitisation for Streamlit markdown ────────────────────────────

_HEX = frozenset("0123456789abcdefABCDEF")


def _decode_unicode_escapes(text: str) -> str:
    """Convert literal ``\\uXXXX`` sequences in text to real Unicode characters.

    Claude sometimes emits Catalan accented characters as raw JSON unicode
    escapes (e.g. the 6-character string ``\\u00e9`` instead of ``é``).
    When the JSON-parsed string still literally contains these sequences they
    show verbatim.  We scan character-by-character and replace each valid
    ``\\uXXXX`` with the corresponding character.

    A pure-Python implementation is used (no regex) because Python 3.14
    refuses to parse ``\\u`` in regex patterns as a literal.
    """
    if "\\" not in text:
        return text  # fast path — nothing to do
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        if (
            text[i] == "\\"
            and i + 5 < n
            and text[i + 1] == "u"
            and all(c in _HEX for c in text[i + 2 : i + 6])
        ):
            out.append(chr(int(text[i + 2 : i + 6], 16)))
            i += 6
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _md(text: str | None) -> str:
    """Sanitise agent-generated text for safe Streamlit markdown rendering.

    Two problems this fixes:

    1. **Dollar-sign / LaTeX** — Streamlit treats ``$...$`` as inline LaTeX and
       ``$$...$$`` as block LaTeX.  Any financial figure such as ``$8.55 EPS``
       or ``$80-83B guidance`` breaks into garbled math output.  We escape every
       ``$`` as ``\\$`` so Streamlit renders it as a literal dollar sign.

    2. **Literal \\uXXXX sequences** — see :func:`_decode_unicode_escapes`.
    """
    if not text:
        return text or ""
    text = _decode_unicode_escapes(text)
    text = text.replace("$", r"\$")
    return text
