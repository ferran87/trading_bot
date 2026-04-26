# Trading Bot — Project Plan

## Status snapshot (read this first)

The **critical file tree** in this document comes from the **original brief** and is **aspirational / partially outdated**. The live repo uses **`ib_async`** (not `ib_insync`) for IBKR, includes `scripts/`, `tests/`, `docs/`, and additional strategies (for example **`aggressive_momentum`**, **`sharp_dip`**) beyond the three filenames in the tree. For **current** stack, bots, and pitfalls, use **[AGENTS.md](AGENTS.md)** and **[docs/CONTEXT_FOR_AI.md](docs/CONTEXT_FOR_AI.md)**.

## Context
Build an autonomous paper-trading bot that connects to Interactive Brokers (IBKR) via API and runs **three strategy "flavours" as independent bots in parallel**, each with its own virtual €1,000 book. After an open-ended paper-trading period, the user reviews the Streamlit dashboard and manually promotes the bot they like best to a real €1,000 live account. Code is written by Cursor; the user interacts with the system primarily via config files and the dashboard. Source of original brief: `C:\Users\ferra\trading bot\initial chat.pdf`.

## Locked decisions

### Environment
- **OS:** Windows (local execution).
- **Project path:** `C:\Users\ferra\trading bot\` (alongside `initial chat.pdf`).
- **Version control:** Git with Python `.gitignore` (venv, `__pycache__`, `data/*.db`, `data/logs/`, `.env`, `data/contracts.json`). Remote: [github.com/ferran87/trading_bot](https://github.com/ferran87/trading_bot); branches `main`, `staging`, `live` (see `docs/BRANCHING.md`).
- **Scheduling:** Windows Task Scheduler, two daily runs — **08:00 CET** (pre-EU open) and **22:30 CET** (post-US close).
- **Secrets:** `.env` file (Anthropic API key, IBKR paper credentials), never committed.

### Broker & accounts
- **Broker:** IBKR, cash account (no leverage, no shorts), European domicile (IBKR Ireland / UK).
- **Connectivity:** `ib_async` (IBKR socket API; same lineage as `ib_insync`).
- **Paper account:** single IBKR paper account shared by all 3 bots. Virtual P&amp;L is **per bot** in SQLite; broker code uses one `ib_async` connection per `run_once` (see `core/broker.py` / `IBKR_CLIENT_ID_BASE` for `clientId` assignment).
- **Market data:** IBKR feed; `yfinance` as fallback/backfill for features and for EU tickers where needed.
- **FX:** base currency EUR. IBKR handles multi-currency; dashboard reports everything in EUR using end-of-day FX rates.

### Capital model
- **3 independent bots**, each with a **€1,000 virtual book** tracked in SQLite (the brief's "virtual portfolio allocation" approach).
- Orders are executed in the shared IBKR paper account but **tagged per bot** and P&L is computed from each bot's virtual book, not from the raw IBKR balance.
- **Compounding:** continuous — each bot's virtual book grows/shrinks with its performance over the full paper period.
- **No auto-promotion:** user picks the winner manually after reviewing the dashboard.

### Guardrails (enforced per bot)
- Single-position cap: **20% for stocks, 35% for ETFs**.
- Crypto cap: **10%** of the bot's book.
- Portfolio floor: **€500** — if the virtual book drops below this, the bot stops trading and is flagged on the dashboard.
- Daily trade limit: **max 5 trades per bot per day**.
- Fee-aware execution: **skip trade if round-trip fees > 25% of expected profit** (expected profit = strategy's target move applied to proposed position size).
- All guardrails enforced in a single `core/risk.py` layer that every order passes through.

### Execution
- **Order type:** market orders for both entries and exits. Small book + liquid tickers → slippage is tiny; daily/weekly cadence is not price-sensitive intraday.
- **Error policy:** fail-safe. Any error (IBKR disconnect, stale data, unexpected exception) → log, send no orders, exit cleanly. Missing a day is cheaper than a wrong trade.

### Strategy specifications

**Bot 1 — Conservative: ETF Momentum Rotation**
- Universe: SPY, QQQ, IWM, DIA, EZU, EWJ, EEM, XLK, XLV, XLF, XLE, XLY.
- Signal: rank by 3-month (63 trading days) total return.
- Hold: **top 3 ETFs equal-weighted (~33% each)** — permitted under the 35% ETF cap.
- Trend filter: if all top-3 have negative 3-month return, go to 100% cash.
- Rebalance: Monday open, weekly. No intraweek action.

**Bot 2 — Moderate: Mean Reversion on Stocks**
- Universe US: AAPL, MSFT, GOOGL, AMZN, META, NVDA, JPM, V, UNH, HD.
- Universe EU: ASML, SAP, LVMH, NESN, SIE, AIR.
- Entry: RSI(14) < 30 at close → buy at next day's open.
- Exits (first to fire): RSI(14) > 55, +5% gain, −7% stop loss, or 10 trading days held.
- Sizing: up to 5 concurrent positions at 20% each.
- Cadence: evaluated every weekday.

**Bot 3 — Aggressive: News Sentiment + Crypto**
- Equity universe: same as Bot 2 (US + EU names).
- News pipeline: **Yahoo Finance RSS per ticker** (`finance.yahoo.com/rss/headline?s=<TICKER>`). Fetched at each scheduled run, deduped, scored with **Claude Haiku 4.5** (model ID `claude-haiku-4-5-20251001`) on a −5..+5 scale. Estimated cost: €2–5/month.
- Anomaly detector: volume spike > 2σ over 20-day mean OR overnight gap > 3%.
- **Trigger:** fires only when (a) avg sentiment > +2 across ≥3 recent headlines AND (b) price anomaly present.
- **Equity sizing (sentiment-weighted):** base 15%, scaled linearly with avg sentiment `s`: `size_pct = 15 + 2.5*(s−2)`, capped at **20%** by the single-position guardrail. Max 5 concurrent positions.
  - s=2 → 15%, s=3 → 17.5%, s=4/5 → 20%.
- Equity exit: 5 trading days held OR ±8%.
- Crypto sleeve (10% of book): weekly rotation on Monday into the best 7-day performer among EU-listed crypto ETPs **BTCE** (Bitcoin) and **ZETH** (Ether), since IBKR European accounts cannot trade native crypto via Paxos.

### Performance metrics (tracked on dashboard, not used for auto-promotion)
- Net return after fees (EUR).
- Sharpe ratio (daily returns, annualized).
- Max drawdown.
- Fees paid (cumulative).
- Win rate and average win/loss.

### Dashboard (Streamlit)
- KPI cards per bot: return, Sharpe, max drawdown, fees paid.
- Equity-curve chart with all 3 bots overlaid.
- Open-positions table per bot (ticker, entry price, current price, unrealized P&L, days held).
- Trade-history / audit log (timestamp, bot, ticker, side, size, fee, signal that fired).
- Guardrail status panel (trades-today counter per bot, floor-breach flags).

## Critical files (original brief — historical tree)

```
C:\Users\ferra\trading bot\
├── .env                        # Anthropic API key, IBKR paper creds (NEVER commit)
├── .gitignore                  # venv, __pycache__, data/*.db, data/logs/, .env
├── requirements.txt            # ib_async, pandas, numpy, sqlalchemy, streamlit,
│                               #   anthropic, yfinance, feedparser, python-dotenv, pydantic, pytest
├── README.md                   # setup + how to run
├── main.py                     # entry point; invoked by Task Scheduler
├── config/
│   ├── settings.yaml           # IBKR host/port, run time, global guardrails, FX settings
│   ├── watchlists.yaml         # ETFs, US stocks, EU stocks, crypto ETPs
│   └── strategies.yaml         # per-strategy parameters (RSI levels, momentum lookback,
│                               #   sentiment threshold, sizing formula, etc.)
├── core/
│   ├── broker.py               # ib_async: MockBroker + IBKRBroker, fills, fees
│   ├── portfolio.py            # virtual-book accounting per bot (cash, positions, equity curve)
│   ├── executor.py             # routes proposed orders → risk.py → broker.py, records trades + fees
│   └── risk.py                 # ALL guardrails (caps, floor, trade limit, fee-aware skip)
├── strategies/
│   ├── base.py                 # abstract Strategy: propose_orders(portfolio, data) -> [Order]
│   ├── etf_momentum.py         # example: UCITS momentum (see live repo for all strategy modules)
│   ├── mean_reversion.py       # Bot 2 (brief); plus other .py files in actual repo
│   └── news_sentiment.py       # Bot 3 (brief); YAML may exist before code is registered
├── analysis/
│   ├── price_signals.py        # volume-spike + gap detectors, RSI, momentum ranks
│   ├── news_fetcher.py         # Yahoo Finance RSS per ticker, dedup (if/when implemented)
│   └── sentiment.py            # Claude Haiku 4.5 scoring with structured prompt (if/when implemented)
├── data/
│   ├── trades.db               # SQLite: bots, trades, positions, equity_snapshots, errors
│   └── logs/                   # per-day log files
└── dashboard/
    └── app.py                  # Streamlit app (run with: streamlit run dashboard/app.py)
```

**Live repository** also contains `scripts/`, `tests/`, `docs/`, `.cursor/` rules, `AGENTS.md`, and other modules not listed above.

### SQLite schema (minimum)
- `bots(id, name, strategy, initial_capital_eur, created_at)`
- `trades(id, bot_id, timestamp, ticker, side, qty, price, fee_eur, signal_reason)`
- `positions(bot_id, ticker, qty, avg_entry, entry_date)` (current open positions)
- `equity_snapshots(bot_id, date, cash_eur, positions_value_eur, total_eur)` (daily)
- `errors(timestamp, bot_id, component, message, traceback)`

## Build phases (unchanged from original brief, scoped to locked decisions)

**Phase 1 — Infrastructure + Bot 1 (~1 week)**
Project skeleton, `.env` + `.gitignore`, IBKR connection on paper, SQLite schema, per-bot virtual-book accounting, `risk.py` guardrail layer, fee tracking, Bot 1 (ETF momentum), basic Streamlit dashboard, Windows Task Scheduler wiring. **Goal:** Bot 1 paper-trading on IBKR end-to-end.

**Phase 2 — Bot 2 + Comparison (~1 week)**
Strategy base class / plugin architecture, Bot 2 (mean reversion), full dashboard with side-by-side comparison and all KPIs. **Goal:** two bots paper-trading, performance compared.

**Phase 3 — Bot 3 (~1 week)**
Yahoo Finance RSS fetcher, Claude Haiku sentiment scorer with structured prompt, price-anomaly detectors, Bot 3 (sentiment-weighted sizing + crypto ETP rotation), full 3-bot dashboard. **Goal:** three bots paper-trading, ready for the user to eventually promote a winner to live.

## Verification (how to test each phase end-to-end)
- **Phase 1:** start IB Gateway in paper mode → `python main.py` → confirm Bot 1 connects, pulls prices for the ETF universe, computes 3-month momentum ranks, proposes an order, passes through `risk.py`, places it in the paper account, records the trade in SQLite with fee, updates the virtual book. Open Streamlit → confirm equity curve, open positions, and trade appear. Run Task Scheduler job manually → confirm it completes without interactive prompts.
- **Phase 2:** same flow with both bots active; verify Bot 1 and Bot 2 trade independently, virtual books diverge, and the dashboard shows overlaid equity curves + side-by-side KPIs.
- **Phase 3:** inject a known-bullish headline for a watchlist ticker during a paper session → confirm `news_fetcher.py` picks it up, Haiku scores it >+2, the anomaly detector evaluates, and (if both conditions fire) Bot 3 places a sentiment-weighted order. Verify crypto ETP rotates on Monday.
- **Guardrail tests:** unit tests in `tests/` covering every `risk.py` rule (20%/35% caps, crypto 10%, €500 floor, 5 trades/day, fee-aware skip). These must pass before Phase 1 is considered done.
- **Fail-safe test:** kill IB Gateway mid-run → confirm bot logs the error, places no orders, exits cleanly.

## Open flags for user awareness (not blockers)
- **IBKR account approval:** plan assumes the account is live and paper trading is enabled. If approval takes longer than 24h, Phase 1 can still scaffold code against the IBKR paper demo.
- **Claude API key:** must be in `.env` before Phase 3. Haiku 4.5 cost budget €2–5/month.
- **EU ETP tickers:** `BTCE` and `ZETH` ticker symbols on IBKR may differ by exchange — Cursor should verify in `watchlists.yaml` against IBKR's contract search during Phase 3.
