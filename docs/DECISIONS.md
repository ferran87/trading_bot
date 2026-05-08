# Architectural decisions & non-obvious invariants

This file captures the "why" behind specific design choices and behaviors that are
not obvious from reading the code. Each entry saves 200–500 tokens of re-derivation
in future AI sessions.

**Keep this file up to date.** The PostToolUse hook in `.claude/settings.json` will
remind you to add an entry whenever you edit a high-impact file.

---

## TTE.PA → FPp_EQ (not TTEp_EQ) in Trading 212

TotalEnergies rebranded from "Total" (old French ticker: FP) in 2021. Trading 212
kept the legacy internal ticker `FPp_EQ`. The yfinance ticker is `TTE.PA` and the
ISIN is `FR0000120271`.

This mapping is locked in `data/t212_instruments_override.json` so that re-running
`scripts/resolve_t212_instruments.py` never overwrites it with a wrong symbol-fallback
match. The resolve script warns when ISIN is unavailable for an EU ticker and symbol
fallback is used — those matches must always be manually verified.

---

## Executor processes SELLs before BUYs

`core/executor.py` sorts all orders for a given run: SELLs first, then BUYs. This
ensures that proceeds from a SELL are immediately available in the virtual cash
balance for a BUY placed in the same run — without needing a pre-reservation step.
If a SELL is rejected, its cash is not freed and subsequent BUYs cannot use it.

---

## Portfolio floor (€500) is a kill-switch, not a per-trade filter

`core/risk.py` Rule 2: if `portfolio_total < €500`, **all** orders (BUY and SELL)
are rejected. The bot freezes entirely until the portfolio recovers above the floor.
This is intentional — it prevents the bot from burning through remaining capital
on small positions after the strategy has already failed. It is a state machine,
not a per-order size check.

---

## Duplicate-buy guard prevents same-day double allocation

`core/executor.py` refuses to place a second BUY for the same ticker on the same
calendar day. Reason: IBKR and T212 orders can remain pending for hours. Without
this guard, a scheduled run plus a manual run would both fire, double-allocating
capital before either fill arrives. The guard checks `Trade.timestamp` date, not
`Trade.status`, so pending fills are also counted.

---

## Paper bots share one T212 demo account — capital is divided equally

All paper bots run against the same single T212 demo account. `_sync_t212_initial_capital()`
in `core/runner.py` divides the total deposited funds equally among all currently
**enabled** paper bots of the same mode (paper). Adding Adria's or Antonio's paper
bots will automatically halve / third Ferran's effective paper capital on the next
sync run.

---

## live_capital_since isolates live bot capital from pre-existing manual portfolio

`Bot.live_capital_since` (DATE column on the `bots` table) tells
`_fetch_total_deposited()` in `core/broker.py` to only count T212 deposits made
**on or after** that date. This excludes pre-existing manual portfolio deposits
from the bot's capital budget. It must be set before the live bot is first activated.

Bots 17 and 20 (Ferran's live bots) have `live_capital_since = 2026-05-06`.

---

## Supabase DDL must be run in the SQL Editor, not via the pooler

`ALTER TABLE` statements sent via the pooler `DATABASE_URL` (PgBouncer) receive a
`QueryCanceled: canceling statement due to statement timeout`. DDL must be executed
directly in the Supabase SQL Editor for project `mfrngzrzwxuygfyjektg`. The
`_migrate()` function in `core/db.py` uses `IF NOT EXISTS` to make migrations
idempotent, so re-running after manual DDL is safe.

---

## T212 EU market orders arrive as "NEW" status — poll for 120 seconds

When a market order is sent to T212 outside exchange hours (or for EU equities), the
order immediately gets status `NEW` rather than `FILLED`. `Trading212Broker` polls
`GET /equity/orders/{id}` for up to 120 seconds. If still `NEW` at timeout, it
records the fill as `is_pending=True`. The next bot run's pre-run reconciliation
step (`_resolve_pending_orders_all_bots`) picks up the fill when markets open.
EU orders also commonly return HTTP 404 for the first ~10 poll attempts right after
creation — this is normal T212 behavior, not an error.

---

## Earnings blackout filter in trend_momentum

`strategies/trend_momentum.py` skips entry into any stock with earnings reported
within ±`earnings_blackout_days` (default 7, configurable in `config/strategies.yaml`)
of today. It uses `yfinance.Ticker(ticker).calendar` and is cached with
`@lru_cache(maxsize=256)` so yfinance is called at most once per ticker per process
run. This was added after SHOP lost 12% on earnings day (2026-05-05).

---

## IBKR pending fills use "PreSubmitted" status as the pending signal

`IBKRBroker` uses a two-phase fill model. After submitting an order it waits ~5
seconds for a `Filled` event. If the order is in `PreSubmitted` or `Submitted`
status at timeout, it records `is_pending=True`. `_resolve_pending_orders_all_bots`
in `core/runner.py` is called at the top of every `run_once()` for IBKR mode to
close out these pending fills on the next scheduled run.

---

## Fee-aware skip (risk Rule 4) requires strategy to set expected_profit_eur > 0

`core/risk.py` Rule 4 only fires if `order.expected_profit_eur > 0`. If a strategy
leaves `expected_profit_eur` at its default of `0.0`, the fee-aware guardrail is
silently skipped. This is by design — not all strategies can estimate profit at
order time. Strategies that do (e.g. mean-reversion with a fixed profit target)
should populate this field to enable the guardrail.
