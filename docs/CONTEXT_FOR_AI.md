# Context for AI assistants (behavioral facts, short)

**Canonical:** [`AGENTS.md`](../AGENTS.md) — edit agent instructions there first.

Supplementary facts for assistants; use alongside [`AGENTS.md`](../AGENTS.md). Do not treat [`PROJECT_PLAN.md`](../PROJECT_PLAN.md) file-tree as the live source of truth for which files exist.

## Implemented behavior

- **Virtual P&L:** Each bot has `initial_capital_eur` and a ledger in SQLite (`Trade`, `Position`, `EquitySnapshot`). Cash is implied from trades, not a separate cash column on `Bot`.
- **Guardrails:** All in [`core/risk.py`](../core/risk.py). Strategies must not re-implement caps or floor.
- **Broker backends:** `BROKER_BACKEND` in `.env`: `mock` | `ibkr` | `t212`.
  - `ibkr`: uses [`ib_async`](../core/broker.py), requires `data/contracts.json` from `scripts/resolve_contracts.py`.
  - `t212`: uses Trading 212 REST API, requires `data/t212_instruments.json` from `scripts/resolve_t212_instruments.py`.
- **Order path:** `executor.run_orders` → `risk.check` → `broker.place_market_order` → `Portfolio.apply_fill` (same session; runner commits per bot).

## Active bots (as of 2026-05-06)

Bot **7** (RSI Compounder, paper, Ferran) and Bot **10** (Trend Momentum, paper, Ferran) are the only enabled bots. Live bots 17 and 20 exist but are disabled — toggle from the dashboard when ready. See `config/strategies.yaml` for all bot rows and `AGENTS.md` for the full bot table.

## Broker backends

| Value | What it does | Required data file |
|-------|-------------|-------------------|
| `mock` | In-memory fills; no external connections. Default for tests. | None |
| `ibkr` | IB Gateway via ib_async. | `data/contracts.json` |
| `t212` | Trading 212 REST API. | `data/t212_instruments.json` |

Paper bots share a single T212 demo account (capital divided equally among all enabled paper peers). Live bots use `live_capital_since` on the `Bot` DB row to isolate deposit history — only deposits on/after that date count as bot capital.

## ETF momentum specifics (when that strategy runs)

- **Params:** [`config/strategies.yaml`](../config/strategies.yaml) block `etf_momentum`.
- **Universe:** `watchlists.yaml` → `etfs_ucits` + `venue:` map.
- **Monday gate:** Skipped when `StrategyContext.force_rebalance` is true (`main.py --force-rebalance`).
- **Bars end date:** `prefetch_since(..., as_of=date)` when `main.py --as-of` is set.

## Trend momentum specifics

- **Earnings blackout:** skips entry if yfinance reports earnings within ±`earnings_blackout_days` (default 7) of today. Uses `@lru_cache` — called once per ticker per process run.
- **Universe:** `stocks_us` + `stocks_eu` from `watchlists.yaml`.

## Extending

1. New strategy class in `strategies/` implementing `propose_orders(snapshot, ctx)`.
2. Register in `STRATEGY_REGISTRY` in [`core/runner.py`](../core/runner.py).
3. YAML under `strategies:` + optional `bots:` row.
4. Tests: pure strategy in `tests/test_*.py`; executor/risk use `db_session` fixture from [`tests/conftest.py`](../tests/conftest.py).
5. **Update `AGENTS.md` bot table and this file** if adding new bots or env vars.

## Env vars that change runtime

| Variable | Effect |
|----------|--------|
| `BROKER_BACKEND` | `mock` \| `ibkr` \| `t212` |
| `IBKR_HOST` / `IBKR_PORT` / `IBKR_CLIENT_ID_BASE` | IB Gateway connection |
| `IBKR_ORDER_TIMEOUT_SEC` | MKT fill wait in `IBKRBroker` (optional) |
| `T212_API_KEY_PAPER` / `T212_API_SECRET_PAPER` | T212 demo API credentials |
| `T212_API_KEY_LIVE` / `T212_API_SECRET_LIVE` | T212 live API credentials |
| `DATABASE_URL` | Supabase PostgreSQL URL (overrides local SQLite `data/trades.db`) |
| `ANTHROPIC_API_KEY` | Claude API for trade explainer (optional) |
| `OPENAI_API_KEY` | OpenAI API for trade explainer (optional) |

## What is not implemented

- **`news_sentiment`:** YAML block exists for future work; not registered in `STRATEGY_REGISTRY` yet.
- **IBKR as primary bar source** for signals (still yfinance; IBKR used for execution when `BROKER_BACKEND=ibkr`).
