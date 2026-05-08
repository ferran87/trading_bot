# Agent instructions — trading bot

**Canonical:** Edit agent instructions here first; keep [`CLAUDE.md`](CLAUDE.md) (compact mirror) and [`docs/CONTEXT_FOR_AI.md`](docs/CONTEXT_FOR_AI.md) (behavioral facts) aligned when behavior changes.

Read this file first for work in **this repository**. Prefer it over loading the full [`PROJECT_PLAN.md`](PROJECT_PLAN.md) unless you need historical product detail.

## Stack

- **Python 3.11+** (3.14 OK with current `requirements.txt` pins).
- **SQLite** + SQLAlchemy (`core/db.py`, `data/trades.db` locally).
- **No web framework** for the bot loop: CLI entry [`main.py`](main.py), scheduled via PowerShell [`scripts/run_once.ps1`](scripts/run_once.ps1).
- **Market data:** `yfinance` in [`analysis/market_data.py`](analysis/market_data.py) (bars, optional `--as-of` truncation).
- **Broker:** [`core/broker.py`](core/broker.py) — `MockBroker` (tests / offline) or **`IBKRBroker`** via **`ib_async`** (not `ib_insync`). Contracts from **`data/contracts.json`** produced by [`scripts/resolve_contracts.py`](scripts/resolve_contracts.py).
- **Dashboard:** Streamlit [`dashboard/app.py`](dashboard/app.py).

## Bot status (truth table)

`STRATEGY_REGISTRY` lives in [`core/runner.py`](core/runner.py). Keys must match `strategy:` in [`config/strategies.yaml`](config/strategies.yaml). **Update this table whenever bots are added, removed, or toggled.**

Currently enabled bots (as of 2026-05-06):

| Bot id | Name | Strategy | Mode | Owner | Enabled |
|--------|------|----------|------|-------|---------|
| 7 | RSI Compounder (Ferran) — Paper | `rsi_compounder` | paper | Ferran | ✅ |
| 10 | Trend Momentum (Ferran) — Paper | `trend_momentum` | paper | Ferran | ✅ |
| 17 | RSI Compounder (Ferran) — Live | `rsi_compounder` | live | Ferran | ❌ toggle from dashboard |
| 20 | Trend Momentum (Ferran) — Live | `trend_momentum` | live | Ferran | ❌ toggle from dashboard |
| 8, 9 | RSI Compounder (Adria / Antonio) | `rsi_compounder` | paper | Adria, Antonio | ❌ pending Gateway |
| 11, 12 | Trend Momentum (Adria / Antonio) | `trend_momentum` | paper | Adria, Antonio | ❌ pending Gateway |
| 18, 19 | RSI Compounder (Adria / Antonio) — Live | `rsi_compounder` | live | Adria, Antonio | ❌ |
| 21, 22 | Trend Momentum (Adria / Antonio) — Live | `trend_momentum` | live | Adria, Antonio | ❌ |
| 1–6 | Legacy bots | various | paper | — | ❌ disabled |

All 9 strategies are registered in `STRATEGY_REGISTRY`. `news_sentiment` is the only exception — YAML stub exists, not yet wired (Phase 3).

Broker backends: `BROKER_BACKEND=mock | ibkr | t212`

To add a strategy: implement under `strategies/`, register in `STRATEGY_REGISTRY`, add a `strategies:` YAML block and optional `bots:` row.

## Execution pipeline (one sentence each)

1. **`main.py --once`** → `core.runner.run_once` → one `IB`/`Mock` connection for the run.
2. **`run_bot`** → `market_data.prefetch_since` → `Portfolio.snapshot` → `Strategy.propose_orders` → **`executor.run_orders`** → each order: **`risk.check`** → **`broker.place_market_order`** → **`Portfolio.apply_fill`** → optional equity snapshot.

## Key files

| Area | Path |
|------|------|
| Entry / CLI flags | [`main.py`](main.py) |
| Orchestration | [`core/runner.py`](core/runner.py) |
| Risk guardrails | [`core/risk.py`](core/risk.py) |
| Orders + fills | [`core/executor.py`](core/executor.py), [`core/broker.py`](core/broker.py) |
| Virtual book | [`core/portfolio.py`](core/portfolio.py) |
| Strategies | [`strategies/`](strategies/) (e.g. `aggressive_momentum`, `etf_momentum`, `mean_reversion`, `sharp_dip`) |
| Config | [`config/settings.yaml`](config/settings.yaml), [`config/watchlists.yaml`](config/watchlists.yaml), [`config/strategies.yaml`](config/strategies.yaml) |

## Commands

```powershell
pytest tests/ -q
python main.py --init-db
python main.py --once
python main.py --once --as-of YYYY-MM-DD --force-rebalance   # ETF momentum off-calendar; bars end at as_of
python main.py --reset-virtual-book 1 --yes                  # SQLite only; does not flatten IBKR
python scripts/check_ibkr.py
python scripts/resolve_contracts.py
streamlit run dashboard/app.py
```

## Pitfalls (save tokens — do not rediscover)

- **`--reset-virtual-book`** clears **SQLite** trades/positions/equity for that bot only. **IBKR paper** may still hold shares from earlier fills; flatten in TWS if you need a clean broker state.
- **EU ETF market orders** on weekends or when the exchange is closed: orders may **not fill** within `IBKR_ORDER_TIMEOUT_SEC`; broker cancels on timeout. Prefer **RTH weekdays** for IBKR smoke tests, or `BROKER_BACKEND=mock`.
- **`python-dotenv`** loads `.env`; a **shell `BROKER_BACKEND` env var overrides** `.env` — clear it if the wrong backend is used.
- **ETF momentum** (`etf_momentum`) normally rebalances **Monday only**; `--force-rebalance` bypasses that for manual runs (see `main.py` / `StrategyContext`).
- **T212 broker (`BROKER_BACKEND=t212`):** requires `data/t212_instruments.json` built by `scripts/resolve_t212_instruments.py`. Paper bots share one T212 demo account; capital is divided equally among all enabled paper bots. Live bots use per-bot deposit isolation via `live_capital_since` on the `Bot` DB row.
- **EU ticker instrument mapping (T212):** yfinance → T212 uses ISIN first; bare-symbol fallback can mis-map EU tickers. Always verify via `data/t212_instruments_override.json`. Known case: `TTE.PA → FPp_EQ` (TotalEnergies legacy T212 ticker — T212 kept the old "FP" symbol after the 2021 rebrand).
- **Supabase DDL:** never run `ALTER TABLE` via the pooler `DATABASE_URL`. Run DDL directly in the Supabase SQL Editor (project `mfrngzrzwxuygfyjektg`). Pooler connections get statement timeout on DDL.

## Where to read more

- **Ultra-short behavioral facts:** [`docs/CONTEXT_FOR_AI.md`](docs/CONTEXT_FOR_AI.md)
- **Diagram + module flow:** [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- **IBKR + Task Scheduler + flags:** [`docs/OPERATIONS.md`](docs/OPERATIONS.md), [`scripts/TASK_SCHEDULER.md`](scripts/TASK_SCHEDULER.md)
- **Branches:** [`docs/BRANCHING.md`](docs/BRANCHING.md)
- **Original long brief / phases:** [`PROJECT_PLAN.md`](PROJECT_PLAN.md) (includes aspirational file tree; cross-check repo)

## Standards

- Match existing style; type hints on new functions.
- Run **`pytest tests/ -q`** before declaring done.
- Keep changes scoped; do not expand unrelated bots unless asked.

## Before declaring a task done

If the task involved any of the following, update the corresponding context files:

| Change type | Files to update |
|-------------|----------------|
| New / removed / toggled bot | `AGENTS.md` bot table, `docs/CONTEXT_FOR_AI.md` active bots section |
| New broker backend or env var | `docs/CONTEXT_FOR_AI.md` env var table, `core/config.py` docstring |
| New architectural decision or non-obvious invariant | `docs/DECISIONS.md` (add a new entry) |
| New strategy wired / unwired | `AGENTS.md` bot table |
| Discovery of a gotcha, bug root cause, or persistent constraint | `docs/DECISIONS.md` + `memory/MEMORY.md` |
| Capital model change | `docs/DECISIONS.md` + `docs/CONTEXT_FOR_AI.md` |

At the end of any session where you made a discovery that would save tokens next session, add a 2–3 line entry to `memory/MEMORY.md`. The PostToolUse hook in `.claude/settings.json` will remind you automatically when high-impact files are edited.
