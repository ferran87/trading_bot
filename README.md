# Trading bot

Autonomous **paper-trading** stack: multiple strategy bots share one **IBKR paper** account while each keeps a **virtual €1,000 book** in **SQLite**. Config-driven (YAML + `.env`), with a **Streamlit** dashboard and **Windows Task Scheduler** hooks.

**Status:** **Bot 1** (UCITS ETF momentum) is implemented end-to-end with **MockBroker** and **IBKRBroker**. Bots 2 and 3 are specified in [`PROJECT_PLAN.md`](PROJECT_PLAN.md) but not wired in `core/runner.py` yet.

Repository: [github.com/ferran87/trading_bot](https://github.com/ferran87/trading_bot)

---

## Requirements

- **Windows** (primary target), **Python 3.11+** (3.14 supported with current pins).
- **IB Gateway** or **TWS** for paper trading when `BROKER_BACKEND=ibkr`.
- Git, PowerShell.

---

## Quick start

```powershell
cd "c:\path\to\trading bot"
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install --upgrade pip
pip install -r requirements.txt

Copy-Item .env.example .env
# Edit .env: leave BROKER_BACKEND=mock until IBKR is configured

python main.py --init-db
python main.py --once
streamlit run dashboard/app.py
```

Run tests:

```powershell
pytest tests/ -q
```

---

## Documentation

| Doc | Content |
|-----|---------|
| [`PROJECT_PLAN.md`](PROJECT_PLAN.md) | Original spec, guardrails, roadmap. |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Data flow, modules, virtual book vs IBKR. |
| [`docs/BRANCHING.md`](docs/BRANCHING.md) | `main`, `staging`, `live` branch policy. |
| [`docs/OPERATIONS.md`](docs/OPERATIONS.md) | IBKR, scheduler, CLI flags, resets. |
| [`scripts/TASK_SCHEDULER.md`](scripts/TASK_SCHEDULER.md) | Windows scheduled tasks. |

---

## Configuration

| File | Role |
|------|------|
| `config/settings.yaml` | Fees, mock slippage, guardrails, logging. |
| `config/watchlists.yaml` | Universes and venue tags per ticker. |
| `config/strategies.yaml` | Bot enablement and per-strategy parameters. |
| `.env` | Secrets and `BROKER_BACKEND` (`mock` or `ibkr`). **Never commit.** |

See [`.env.example`](.env.example) for all variables.

---

## IBKR paper (Bot 1)

1. Gateway logged into **paper**, API port open (often **4002**).
2. `.env`: `BROKER_BACKEND=ibkr`, host/port/client id.
3. `python scripts/check_ibkr.py`
4. `python scripts/resolve_contracts.py` → generates local `data/contracts.json` (gitignored; regenerate on each machine).
5. `python main.py --once`

Details: [`docs/OPERATIONS.md`](docs/OPERATIONS.md).

---

## Branches

- **`main`** — default development branch.
- **`staging`** — integration / pre-release testing.
- **`live`** — stable line used for scheduled runs you trust.

Workflow: [`docs/BRANCHING.md`](docs/BRANCHING.md).

---

## Guardrails (enforced)

Implemented in `core/risk.py` (see tests): per-asset position caps, portfolio floor, daily trade limit, fee-aware skip. Strategies only propose orders; they do not enforce caps.

---

## Security

- Do not commit `.env`, databases, logs, or `data/contracts.json`.
- The file `initial chat.pdf` is excluded from the repo by `.gitignore` (local brief).

---

## License

All rights reserved unless you add an explicit `LICENSE` file.
