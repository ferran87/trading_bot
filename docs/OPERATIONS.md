# Operations guide

## Interactive Brokers (paper)

1. Install **IB Gateway** (or TWS), log in with **Paper Trading**.
2. Enable API: **Configure → Settings → API** — note the socket port (**4002** for Gateway paper is typical).
3. Copy `.env.example` to `.env` and set `IBKR_HOST`, `IBKR_PORT`, `IBKR_CLIENT_ID_BASE`, `BROKER_BACKEND=ibkr`.
4. Verify connectivity:

   ```powershell
   python scripts/check_ibkr.py
   ```

5. Build the contract cache (after any `watchlists.yaml` change):

   ```powershell
   python scripts/resolve_contracts.py
   ```

6. Optional smoke trade (does not use the bot database):

   ```powershell
   python scripts/test_live_order.py --ticker AAPL --qty 1
   ```

**Market hours:** EU-listed ETFs need the relevant exchange session for **market** orders to fill quickly. Weekend runs may time out; see `IBKR_ORDER_TIMEOUT_SEC` in `.env.example`.

## Scheduling (Windows)

See [`scripts/TASK_SCHEDULER.md`](../scripts/TASK_SCHEDULER.md). Wrapper: `scripts/run_once.ps1`.

## Useful `main.py` flags

| Flag | Meaning |
|------|---------|
| `--once` | Run one cycle for all enabled bots. |
| `--init-db` | Create tables and seed bots from YAML. |
| `--date YYYY-MM-DD` | Calendar date for snapshots and trade timestamps. |
| `--as-of YYYY-MM-DD` | Truncate yfinance bars to this date (must be `<= --date` or today). |
| `--force-rebalance` | ETF momentum ignores the Monday-only rule. |
| `--reset-virtual-book N --yes` | Delete trades, positions, and equity rows for bot `N` in SQLite only. |

## Virtual book reset

Use when you need a clean target portfolio for testing:

```powershell
python main.py --reset-virtual-book 1 --yes
```

This does **not** close positions at IBKR. Flatten manually in TWS if the paper account holds leftovers from experiments.
