# Claude Code — trading bot

**Canonical:** [`AGENTS.md`](AGENTS.md) — edit agent instructions there first.

**Mirror:** This file stays a **compact** summary for Claude Code sessions; keep it aligned when behavior or bot wiring changes.

## What this repo is

Paper-trading stack: **SQLite virtual books per bot**, **Trading 212 paper** execution via REST API, signals mostly from **yfinance**. Entry: **`main.py`**. No FastAPI server for the loop.

## Read order

1. [`AGENTS.md`](AGENTS.md) — full agent instructions, tables, links (canonical).
2. [`docs/CONTEXT_FOR_AI.md`](docs/CONTEXT_FOR_AI.md) — minimal behavioral facts.
3. [`PROJECT_PLAN.md`](PROJECT_PLAN.md) — historical product brief only; file tree is **not** authoritative.

## Stack (one line each)

- **DB:** SQLAlchemy + SQLite or Supabase Postgres (`core/db.py`).
- **Orchestration:** `core/runner.py` → `run_bot` per enabled bot.
- **Orders:** `core/executor.py` → `core/risk.py` → `core/broker.py` → `core/portfolio.py`.
- **Brokers:** `MockBroker` (offline/tests) | `Trading212Broker` (`BROKER_BACKEND=t212` in `.env`). T212 needs `data/t212_instruments.json` from `scripts/resolve_t212_instruments.py`.
- **T212 auth:** shared in `core/t212_auth.py`; per-owner credentials via `T212_API_KEY_PAPER_<OWNER>` etc.
- **UI:** `streamlit run dashboard/app.py`.

## `STRATEGY_REGISTRY` (`core/runner.py`)

YAML `strategy:` must match a registry key.

| Bot id | `strategy` | Module (typical) |
|--------|------------|------------------|
| 1 | `aggressive_momentum` | `strategies/aggressive_momentum.py` |
| 2 | `mean_reversion` | `strategies/mean_reversion.py` |
| 3 | `sharp_dip` | `strategies/sharp_dip.py` |
| 7 | `rsi_compounder` | active paper (Ferran) |
| 10 | `trend_momentum` | active paper (Ferran) |
| 9, 12 | rsi_compounder, trend_momentum | active paper (Antonio) |
| 30 | `ai_thesis` | AI Thesis Bot (Phase 2) |

New strategy: add class under `strategies/`, register dict in `runner.py`, add YAML under `strategies:` and optional `bots:`.

## Commands

```bash
pytest tests/ -q
python main.py --init-db
python main.py --once
python main.py --once --as-of YYYY-MM-DD --force-rebalance
python main.py --reset-virtual-book 1 --yes
python scripts/resolve_t212_instruments.py
```

## Pitfalls

- **`--reset-virtual-book`:** SQLite only; does **not** close T212 positions.
- **Shell env overrides `.env`:** e.g. `BROKER_BACKEND` set in the shell wins over `python-dotenv`.
- **`etf_momentum`:** Monday rebalance unless `main.py --force-rebalance` / `StrategyContext.force_rebalance`.
- **T212 demo orders:** EU stocks placed pre-market arrive as `NEW`; broker logs a pending fill and reconciles on the next run.

## Standards

Type hints on new functions; run **`pytest tests/ -q`** before finishing; scoped changes unless asked otherwise.
