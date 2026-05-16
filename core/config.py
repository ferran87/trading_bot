"""Config loader. Reads YAML files in config/ and merges .env overrides.

Kept intentionally small — no Pydantic models yet; strategies read the raw
dicts. If the schema grows unwieldy we can wrap with Pydantic later.

YAML structure
--------------
settings.yaml  → CONFIG.settings
    guardrails:  {initial_capital_eur, position_cap_stock, position_cap_etf,
                  position_cap_crypto, portfolio_floor_eur, max_trades_per_day,
                  fee_profit_ratio_cap}
    fees:        {xetra_eur, lse_gbp, nyse_usd, ...}  (per-venue fee rates for IBKR)
    fees_t212:   {xetra_eur, euronext_eur, lse_gbp, ...}  (per-venue fee rates for T212)
    fx_pairs:    [EURUSD=X, EURGBP=X, ...]

strategies.yaml → CONFIG.strategies
    bots:        [{id, name, strategy, trading_mode, enabled, owner,
                   ibkr_port, ibkr_port_paper, live_capital_since}, ...]
    strategies:  {strategy_name: {universe, lookback_days, rsi_period, ...}}

watchlists.yaml → CONFIG.watchlists
    venue:       {ticker: {class: stock|etf|crypto, venue: xetra|nyse|...}}
    stocks_us:   [AAPL, MSFT, ...]
    stocks_eu:   [SXR8.DE, MC.PA, ...]
    etfs_ucits:  [VWRL.AS, ...]
    crypto_etps: [BTCE.DE, ...]

users.yaml → CONFIG.users, CONFIG.admin_owner
    users:       [{name, role}]  — role: admin | viewer
    Admin is the first entry with role=admin.  Only admin can approve / reject
    in the Strategy Lab, Themes, and Thesis dashboard tabs.
    Adding a new user: add entry here + T212 env vars + YAML bot entries + --init-db.

Environment variables (all from .env or shell; shell overrides .env)
---------------------------------------------------------------------
BROKER_BACKEND              mock | ibkr | t212  (default: mock)
DATABASE_URL                Supabase PostgreSQL — overrides local SQLite
DATABASE_URL_IBKR           PostgreSQL for IBKR backend
DATABASE_URL_T212           PostgreSQL for T212 backend

IBKR_HOST                   IB Gateway host  (default: 127.0.0.1)
IBKR_PORT                   IB Gateway port  (default: 4002)
IBKR_CLIENT_ID_BASE         Base client ID   (default: 10)
IBKR_ORDER_TIMEOUT_SEC      Seconds to wait for MKT fill  (default: 30)

T212_API_KEY_PAPER          T212 demo API key
T212_API_SECRET_PAPER       T212 demo API secret
T212_API_KEY_LIVE           T212 live API key
T212_API_SECRET_LIVE        T212 live API secret

ANTHROPIC_API_KEY           Claude API for trade explainer  (optional)
OPENAI_API_KEY              OpenAI API for trade explainer  (optional)
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR = DATA_DIR / "logs"

load_dotenv(PROJECT_ROOT / ".env", override=True)


def _load_yaml(name: str) -> dict[str, Any]:
    path = CONFIG_DIR / name
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


class Config:
    """Lazy container for the three config files + env."""

    def __init__(self) -> None:
        self._settings: dict[str, Any] | None = None
        self._watchlists: dict[str, Any] | None = None
        self._strategies: dict[str, Any] | None = None
        self._users: list[dict[str, Any]] | None = None

    @property
    def settings(self) -> dict[str, Any]:
        if self._settings is None:
            self._settings = _load_yaml("settings.yaml")
        return self._settings

    @property
    def watchlists(self) -> dict[str, Any]:
        if self._watchlists is None:
            self._watchlists = _load_yaml("watchlists.yaml")
        return self._watchlists

    @property
    def strategies(self) -> dict[str, Any]:
        # Cached so the Strategy Lab can apply temporary in-process overrides
        # to bot parameters during a backtest without losing them on the
        # next access. The cache survives until ``reload_strategies()`` is
        # called explicitly (e.g. after the dashboard approves a YAML edit).
        if self._strategies is None:
            self._strategies = _load_yaml("strategies.yaml")
        return self._strategies

    @property
    def users(self) -> list[dict[str, Any]]:
        """User roster from ``config/users.yaml``.

        Returns an empty list when the file does not exist so the rest of the
        codebase degrades gracefully (e.g. no admin → all action buttons are
        hidden as a safe default).
        """
        if self._users is None:
            users_path = CONFIG_DIR / "users.yaml"
            if users_path.exists():
                raw = _load_yaml("users.yaml")
                self._users = raw.get("users", [])
            else:
                self._users = []
        return self._users

    @property
    def admin_owner(self) -> str | None:
        """Name of the admin user, or *None* if no admin is defined.

        The first entry with ``role: admin`` in ``config/users.yaml`` is used.
        """
        for u in self.users:
            if u.get("role") == "admin":
                return u["name"]
        return None

    def reload_strategies(self) -> None:
        """Invalidate the cached strategies dict so the next access re-reads
        the YAML from disk.

        Call this from any code path that has just edited ``strategies.yaml``
        (notably the Strategy Lab approval flow). Tests that mutate strategies
        between assertions can also use it to reset the cache.
        """
        self._strategies = None

    @property
    def broker_backend(self) -> str:
        return os.getenv("BROKER_BACKEND", "mock").lower()

    @property
    def db_path(self) -> Path:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        return DATA_DIR / "trades.db"

    @property
    def db_url(self) -> str:
        """Return the SQLAlchemy database URL.

        Priority:
          1. DATABASE_URL_IBKR  — when BROKER_BACKEND=ibkr
          2. DATABASE_URL_T212  — when BROKER_BACKEND=t212
          3. DATABASE_URL       — generic fallback (legacy / Streamlit Cloud single-secret)
          4. Local SQLite       — offline / development

        Supabase and some PaaS providers give a `postgres://` URL — SQLAlchemy
        requires `postgresql://`, so we normalise it automatically.
        """
        backend = os.getenv("BROKER_BACKEND", "mock").lower()
        candidates = []
        if backend == "ibkr":
            candidates.append(os.getenv("DATABASE_URL_IBKR"))
        elif backend == "t212":
            candidates.append(os.getenv("DATABASE_URL_T212"))
        candidates.append(os.getenv("DATABASE_URL"))

        for url in candidates:
            if url:
                if url.startswith("postgres://"):
                    url = url.replace("postgres://", "postgresql://", 1)
                return url
        return f"sqlite:///{self.db_path.as_posix()}"


CONFIG = Config()
