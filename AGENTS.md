# Agent instructions — trading bot

**Canonical:** Edit agent instructions here first; keep [`CLAUDE.md`](CLAUDE.md) (compact mirror) and [`docs/CONTEXT_FOR_AI.md`](docs/CONTEXT_FOR_AI.md) (behavioral facts) aligned when behavior changes.

Read this file first for work in **this repository**. Prefer it over loading the full [`PROJECT_PLAN.md`](PROJECT_PLAN.md) unless you need historical product detail.

## Stack

- **Python 3.11+** (3.14 OK with current `requirements.txt` pins).
- **SQLite** + SQLAlchemy locally; Supabase Postgres on Streamlit Cloud (`core/db.py`).
- **No web framework** for the bot loop: CLI entry [`main.py`](main.py), scheduled via PowerShell [`scripts/run_once.ps1`](scripts/run_once.ps1).
- **Market data:** `yfinance` in [`analysis/market_data.py`](analysis/market_data.py) (bars, optional `--as-of` truncation).
- **Broker:** [`core/broker.py`](core/broker.py) — `MockBroker` (tests / offline) or **`Trading212Broker`** (`BROKER_BACKEND=t212`). T212 credentials live in [`core/t212_auth.py`](core/t212_auth.py); instruments map from [`data/t212_instruments.json`](data/t212_instruments.json) (built by [`scripts/resolve_t212_instruments.py`](scripts/resolve_t212_instruments.py)).
- **Dashboard:** Streamlit [`dashboard/app.py`](dashboard/app.py).

## Bot status (truth table)

`STRATEGY_REGISTRY` lives in [`core/runner.py`](core/runner.py). Keys must match `strategy:` in [`config/strategies.yaml`](config/strategies.yaml). **Update this table whenever bots are added, removed, or toggled.**

Currently enabled bots (as of 2026-05-16):

| Bot id | Name | Strategy | Mode | Owner | Enabled |
|--------|------|----------|------|-------|---------|
| 7 | RSI Compounder (Ferran) — Paper | `rsi_compounder` | paper | Ferran | ✅ |
| 10 | Trend Momentum (Ferran) — Paper | `trend_momentum` | paper | Ferran | ✅ |
| 9 | RSI Compounder (Antonio) — Paper | `rsi_compounder` | paper | Antonio | ✅ |
| 12 | Trend Momentum (Antonio) — Paper | `trend_momentum` | paper | Antonio | ✅ |
| 17, 20 | Live counterparts (Ferran) | `rsi_compounder`, `trend_momentum` | live | Ferran | ❌ toggle from dashboard |
| 19, 22 | Live counterparts (Antonio) | `rsi_compounder`, `trend_momentum` | live | Antonio | ❌ |
| 30 | AI Thesis Bot | `ai_thesis` | paper | Ferran | ❌ (Phase 2) |
| 1–6, 8, 11 | Legacy / placeholder bots | various | paper | various | ❌ disabled |

All wired strategies are registered in `STRATEGY_REGISTRY`.

Broker backends: `BROKER_BACKEND=mock | t212`

To add a strategy: implement under `strategies/`, register in `STRATEGY_REGISTRY`, add a `strategies:` YAML block and optional `bots:` row.

To add a user: add an entry to [`config/users.yaml`](config/users.yaml) with `role: viewer`, add the four T212 env vars (`T212_API_KEY_PAPER_<NAME>`, `T212_API_SECRET_PAPER_<NAME>`, plus `_LIVE_` variants), add the bot entries to `config/strategies.yaml`, and run `python main.py --init-db`.

## Execution pipeline (one sentence each)

1. **`main.py --once`** → `core.runner.run_once` → resolves T212 pending orders → runs each enabled bot.
2. **`run_bot`** → `market_data.prefetch_since` → `Portfolio.snapshot` → `Strategy.propose_orders` → **`executor.run_orders`** → each order: **`risk.check`** → **`broker.place_market_order`** (Trading212 REST) → **`Portfolio.apply_fill`** → optional equity snapshot.

## Key files

| Area | Path |
|------|------|
| Entry / CLI flags | [`main.py`](main.py) |
| Orchestration | [`core/runner.py`](core/runner.py) |
| Risk guardrails | [`core/risk.py`](core/risk.py) |
| Orders + fills | [`core/executor.py`](core/executor.py), [`core/broker.py`](core/broker.py) |
| T212 auth (shared) | [`core/t212_auth.py`](core/t212_auth.py) |
| Virtual book | [`core/portfolio.py`](core/portfolio.py) |
| Strategies | [`strategies/`](strategies/) |
| Config | [`config/settings.yaml`](config/settings.yaml), [`config/watchlists.yaml`](config/watchlists.yaml), [`config/strategies.yaml`](config/strategies.yaml), [`config/users.yaml`](config/users.yaml) |
| AI agents | [`agents/`](agents/) — shared loop in [`agents/_loop.py`](agents/_loop.py) |

## Commands

```powershell
pytest tests/ -q
python main.py --init-db
python main.py --once
python main.py --once --as-of YYYY-MM-DD --force-rebalance   # ETF momentum off-calendar; bars end at as_of
python main.py --reset-virtual-book 1 --yes                  # SQLite only; does not flatten T212
python scripts/resolve_t212_instruments.py
streamlit run dashboard/app.py
```

## Pitfalls (save tokens — do not rediscover)

- **`--reset-virtual-book`** clears **SQLite** trades/positions/equity for that bot only. T212 paper account may still hold shares from earlier fills; close them in the T212 UI if you need a clean broker state.
- **`python-dotenv`** loads `.env`; a **shell `BROKER_BACKEND` env var overrides** `.env` — clear it if the wrong backend is used.
- **ETF momentum** (`etf_momentum`) normally rebalances **Monday only**; `--force-rebalance` bypasses that for manual runs (see `main.py` / `StrategyContext`).
- **T212 broker (`BROKER_BACKEND=t212`):** requires `data/t212_instruments.json` built by `scripts/resolve_t212_instruments.py`. Each owner has their own T212 account; capital is divided equally among that owner's enabled paper bots. Live bots use per-bot deposit isolation via `live_capital_since` on the `Bot` DB row.
- **EU ticker instrument mapping (T212):** yfinance → T212 uses ISIN first; bare-symbol fallback can mis-map EU tickers. Always verify via `data/t212_instruments_override.json`. Known case: `TTE.PA → FPp_EQ` (TotalEnergies legacy T212 ticker — T212 kept the old "FP" symbol after the 2021 rebrand).
- **T212 fill prices** come back in **native currency** (USD for US stocks, EUR for EU). `walletImpact.fxRate` gives EUR/local rate; `_resolve_t212_pending_orders` converts properly.
- **Supabase DDL:** never run `ALTER TABLE` via the pooler `DATABASE_URL`. Run DDL directly in the Supabase SQL Editor (project `mfrngzrzwxuygfyjektg`). Pooler connections get statement timeout on DDL.
- **Streamlit Cloud secrets:** secrets defined in the Streamlit Cloud secrets manager are mirrored into `os.environ` by `core/config.py` so all `os.environ.get()` lookups work identically locally and on the cloud.

## Where to read more

- **Ultra-short behavioral facts:** [`docs/CONTEXT_FOR_AI.md`](docs/CONTEXT_FOR_AI.md)
- **Diagram + module flow:** [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- **Task Scheduler + flags:** [`docs/OPERATIONS.md`](docs/OPERATIONS.md), [`scripts/TASK_SCHEDULER.md`](scripts/TASK_SCHEDULER.md)
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
