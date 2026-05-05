"""Config loader. Reads YAML files in config/ and merges .env overrides.

Kept intentionally small — no Pydantic models yet; strategies read the raw
dicts. If the schema grows unwieldy we can wrap with Pydantic later.
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
        return _load_yaml("strategies.yaml")

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
