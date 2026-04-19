"""Pytest fixtures: isolated SQLite per test + seeded bots."""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    """Pin config to the repo config/ files but swap the DB path to tmp."""
    monkeypatch.setenv("BROKER_BACKEND", "mock")
    monkeypatch.chdir(Path(__file__).resolve().parents[1])


@pytest.fixture
def db_session(monkeypatch, tmp_path) -> Session:
    """Fresh SQLite file per test, tables created, 3 bots seeded at €1,000 each.

    We swap the module-level _engine and _SessionLocal in core.db so every
    call to get_session() in production code hits this test DB without
    touching the real data/trades.db.
    """
    from core import db as db_mod

    db_file = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_file.as_posix()}", future=True)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    original_engine = db_mod._engine
    original_sessionlocal = db_mod._SessionLocal
    db_mod._engine = engine
    db_mod._SessionLocal = SessionLocal

    def _restore():
        db_mod._engine = original_engine
        db_mod._SessionLocal = original_sessionlocal

    monkeypatch.setattr(db_mod, "engine", lambda: engine)
    monkeypatch.setattr(db_mod, "session_factory", lambda: SessionLocal)

    db_mod.Base.metadata.create_all(engine)

    with SessionLocal() as s:
        for i, (name, strat) in enumerate(
            [("Conservative", "etf_momentum"),
             ("Moderate", "mean_reversion"),
             ("Aggressive", "news_sentiment")],
            start=1,
        ):
            s.add(db_mod.Bot(id=i, name=name, strategy=strat,
                             initial_capital_eur=1000.0, enabled=1))
        s.commit()

    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        _restore()
