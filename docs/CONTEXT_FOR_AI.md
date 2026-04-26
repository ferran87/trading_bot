# Context for AI assistants (behavioral facts, short)

**Canonical:** [`AGENTS.md`](../AGENTS.md) — edit agent instructions there first.

Supplementary facts for assistants; use alongside [`AGENTS.md`](../AGENTS.md). Do not treat [`PROJECT_PLAN.md`](../PROJECT_PLAN.md) file-tree as the live source of truth for which files exist.

## Implemented behavior

- **Virtual P&amp;L:** Each bot has `initial_capital_eur` and a ledger in SQLite (`Trade`, `Position`, `EquitySnapshot`). Cash is implied from trades, not a separate cash column on `Bot`.
- **Guardrails:** All in [`core/risk.py`](../core/risk.py). Strategies must not re-implement caps or floor.
- **Broker backends:** `BROKER_BACKEND` in `.env`: `mock` | `ibkr`. IBKR uses [`ib_async`](../core/broker.py), `data/contracts.json` from `scripts/resolve_contracts.py`.
- **Order path:** `executor.run_orders` → `risk.check` → `broker.place_market_order` → `Portfolio.apply_fill` (same session; runner commits per bot).

## Active bots (default YAML)

Three bots enabled, strategies wired in `STRATEGY_REGISTRY`: **`aggressive_momentum`**, **`mean_reversion`**, **`sharp_dip`**. The **`etf_momentum`** strategy is also registered (used if you point a bot at it in YAML).

## ETF momentum specifics (when that strategy runs)

- **Params:** [`config/strategies.yaml`](../config/strategies.yaml) block `etf_momentum`.
- **Universe:** `watchlists.yaml` → `etfs_ucits` + `venue:` map.
- **Monday gate:** Skipped when `StrategyContext.force_rebalance` is true (`main.py --force-rebalance`).
- **Bars end date:** `prefetch_since(..., as_of=date)` when `main.py --as-of` is set.

## Extending

1. New strategy class in `strategies/` implementing `propose_orders(snapshot, ctx)`.
2. Register in `STRATEGY_REGISTRY` in [`core/runner.py`](../core/runner.py).
3. YAML under `strategies:` + optional `bots:` row.
4. Tests: pure strategy in `tests/test_*.py`; executor/risk use `db_session` fixture from [`tests/conftest.py`](../tests/conftest.py).

## Env vars that change runtime

| Variable | Effect |
|----------|--------|
| `BROKER_BACKEND` | `mock` vs `ibkr` |
| `IBKR_HOST` / `IBKR_PORT` / `IBKR_CLIENT_ID_BASE` | IB connection |
| `IBKR_ORDER_TIMEOUT_SEC` | MKT fill wait in `IBKRBroker` (optional) |

## What is not implemented

- **`news_sentiment`:** YAML block exists for future work; not registered in `STRATEGY_REGISTRY` yet.
- **IBKR as primary bar source** for signals (still yfinance; IBKR used for execution when `BROKER_BACKEND=ibkr`).
