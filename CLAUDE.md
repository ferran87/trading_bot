# Claude Code — trading bot

**Canonical:** [`AGENTS.md`](AGENTS.md) — edit agent instructions there first.

**Mirror:** This file stays a **compact** summary for Claude Code sessions; keep it aligned when behavior or bot wiring changes.

## What this repo is

Paper-trading stack: **SQLite virtual books per bot**, optional **IBKR paper** execution via **`ib_async`**, signals mostly from **yfinance**. Entry: **`main.py`**. No FastAPI server for the loop.

## Read order

1. [`AGENTS.md`](AGENTS.md) — full agent instructions, tables, links (canonical).
2. [`docs/CONTEXT_FOR_AI.md`](docs/CONTEXT_FOR_AI.md) — minimal behavioral facts.
3. [`PROJECT_PLAN.md`](PROJECT_PLAN.md) — historical product brief only; file tree is **not** authoritative.

## Stack (one line each)

- **DB:** SQLAlchemy + SQLite (`core/db.py`, local `data/trades.db`).
- **Orchestration:** `core/runner.py` → `run_bot` per enabled bot.
- **Orders:** `core/executor.py` → `core/risk.py` → `core/broker.py` → `core/portfolio.py`.
- **Brokers:** `MockBroker` | `IBKRBroker` (`BROKER_BACKEND` in `.env`). IBKR needs `data/contracts.json` from `scripts/resolve_contracts.py`.
- **UI:** `streamlit run dashboard/app.py`.

## `STRATEGY_REGISTRY` (`core/runner.py`)

YAML `strategy:` must match a registry key.

| Bot id | `strategy` | Module (typical) |
|--------|------------|------------------|
| 1 | `aggressive_momentum` | `strategies/aggressive_momentum.py` |
| 2 | `mean_reversion` | `strategies/mean_reversion.py` |
| 3 | `sharp_dip` | `strategies/sharp_dip.py` |
| (optional) | `etf_momentum` | `strategies/etf_momentum.py` |
| (not wired) | `news_sentiment` | YAML stub; not in registry yet |

New strategy: add class under `strategies/`, register dict in `runner.py`, add YAML under `strategies:` and optional `bots:`.

## Commands

```bash
pytest tests/ -q
python main.py --init-db
python main.py --once
python main.py --once --as-of YYYY-MM-DD --force-rebalance
python main.py --reset-virtual-book 1 --yes
python scripts/check_ibkr.py
python scripts/resolve_contracts.py
```

## Pitfalls

- **`--reset-virtual-book`:** SQLite only; does **not** close IBKR positions.
- **MKT + closed market:** timeouts; use RTH or `mock`. Optional `IBKR_ORDER_TIMEOUT_SEC`.
- **Shell env overrides `.env`:** e.g. `BROKER_BACKEND` set in the shell wins over `python-dotenv`.
- **`etf_momentum`:** Monday rebalance unless `main.py --force-rebalance` / `StrategyContext.force_rebalance`.

## Standards

Type hints on new functions; run **`pytest tests/ -q`** before finishing; scoped changes unless asked otherwise.
