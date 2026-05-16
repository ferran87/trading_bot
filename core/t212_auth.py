"""Shared Trading 212 credential resolution.

Both ``core/broker.py`` (used by the trading runner) and
``dashboard/queries.py`` (used by the Streamlit UI) need to resolve the same
T212 API key + secret for the same (demo, owner) pair.  Before this module
the lookup logic was duplicated in both places, which is fragile: changing
the env-var naming scheme required updating two files in lockstep.

This module is the single source of truth.

Lookup order (first non-empty wins):
  1. ``T212_API_KEY_{SUFFIX}_{OWNER}``  — per-owner credentials
  2. ``T212_API_KEY_{SUFFIX}``          — single-account default (Ferran)
  3. ``T212_API_KEY``                   — legacy single-env-var fallback

Where ``SUFFIX`` is ``PAPER`` (demo) or ``LIVE`` and ``OWNER`` is the bot owner
name uppercased (e.g. ``ANTONIO``).
"""
from __future__ import annotations

import base64
import logging
import os

# Side-effect import: ensures .env (local) and st.secrets (Streamlit Cloud)
# are mirrored into os.environ before we read any T212_* keys.  This makes
# the module robust to import order — callers do not have to remember to
# import core.config first.
from core import config as _config  # noqa: F401

log = logging.getLogger(__name__)


def _resolve_env(prefix: str, suffix: str, owner_suffix: str) -> tuple[str, str]:
    """Return ``(value, env_var_name_used)`` for the first non-empty env var.

    ``env_var_name_used`` is returned so callers can detect when an
    owner-specific lookup fell through to a generic fallback.  If nothing
    matches, the *expected* most-specific name is returned alongside an empty
    value, so error messages can reference the variable the user should set.
    """
    candidates: list[str] = []
    if owner_suffix:
        candidates.append(f"{prefix}_{suffix}_{owner_suffix}")
    candidates.append(f"{prefix}_{suffix}")
    candidates.append(prefix)
    for name in candidates:
        v = os.environ.get(name, "").strip()
        if v:
            return v, name
    return "", candidates[0]


def resolve_t212_credentials(
    demo: bool,
    owner: str | None,
    *,
    warn_on_fallback: bool = True,
) -> tuple[str, str]:
    """Return ``(api_key, api_secret)`` for the given environment + owner.

    Raises ``RuntimeError`` with an actionable message when either is missing.

    When ``warn_on_fallback`` is True (the default), logs a WARNING if an
    owner was requested but only a generic key was found — silent fallback
    would route orders to the wrong account.
    """
    suffix = "PAPER" if demo else "LIVE"
    owner_suffix = (owner.upper() if owner else "").strip()

    key,    key_var    = _resolve_env("T212_API_KEY",    suffix, owner_suffix)
    secret, secret_var = _resolve_env("T212_API_SECRET", suffix, owner_suffix)

    if warn_on_fallback and owner_suffix and key:
        expected_key_var = f"T212_API_KEY_{suffix}_{owner_suffix}"
        if key_var != expected_key_var:
            log.warning(
                "T212 credentials: owner=%r has no dedicated %s — "
                "falling back to %r which may point to a DIFFERENT account. "
                "Set %s in .env to suppress this warning.",
                owner, expected_key_var, key_var, expected_key_var,
            )

    owner_hint = f" for owner={owner!r}" if owner else ""
    if not key:
        raise RuntimeError(
            f"{key_var} not set in .env{owner_hint} -- cannot connect to "
            "Trading 212. Generate a key from T212 -> Settings -> API (Beta) "
            "and save both the key AND secret."
        )
    if not secret:
        raise RuntimeError(
            f"{secret_var} not set in .env{owner_hint} -- the secret is "
            "shown only once at key creation time. Delete the old key, "
            "generate a new one, and save both values."
        )
    return key, secret


def t212_basic_auth_header(key: str, secret: str) -> str:
    """Return the value for an HTTP ``Authorization`` header: ``Basic <b64>``."""
    token = base64.b64encode(f"{key}:{secret}".encode()).decode()
    return f"Basic {token}"


def t212_headers(
    demo: bool,
    owner: str | None,
    *,
    include_content_type: bool = False,
) -> dict[str, str] | None:
    """Return the request headers for a T212 API call, or ``None`` if creds missing.

    The dashboard uses this with ``include_content_type=False`` for plain GETs.
    The broker uses ``include_content_type=True`` because it POSTs JSON bodies.

    Unlike :func:`resolve_t212_credentials`, this never raises — it returns
    ``None`` when credentials are absent, so the dashboard can surface a friendly
    UI warning instead of crashing.
    """
    try:
        key, secret = resolve_t212_credentials(demo, owner)
    except RuntimeError:
        return None
    headers = {"Authorization": t212_basic_auth_header(key, secret)}
    if include_content_type:
        headers["Content-Type"] = "application/json"
    return headers


def t212_base_url(demo: bool) -> str:
    """Return the T212 REST API base URL (no trailing slash)."""
    return (
        "https://demo.trading212.com/api/v0"
        if demo
        else "https://live.trading212.com/api/v0"
    )
