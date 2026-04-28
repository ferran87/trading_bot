# Trading bot

Autonomous multi-strategy trading stack: bots run in **paper** or **live** mode against **IBKR Gateway** accounts, each keeping a virtual book in the database. Config-driven (YAML + `.env`), with a **Streamlit** dashboard and fully-automated morning startup via **IBC + Windows Task Scheduler**.

**Status:** 9 strategies registered (see [`core/runner.py`](core/runner.py)); active bots defined in [`config/strategies.yaml`](config/strategies.yaml). Execution supports **MockBroker** (offline/tests) and **IBKRBroker** (`ib_async`). Start with [`AGENTS.md`](AGENTS.md).

Repository: [github.com/ferran87/trading_bot](https://github.com/ferran87/trading_bot)

---

## Requirements

- **Windows** (primary target), **Python 3.11+** (3.14 supported with current pins).
- **IB Gateway** installed at `C:\Jts\ibgateway\1037\`.
- **IBC** (Interactive Brokers Controller) at `C:\IBC\` — handles auto-login.
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
| [`AGENTS.md`](AGENTS.md) | **Start here** — stack, pipeline, pitfalls, links. |
| [`CLAUDE.md`](CLAUDE.md) | Claude Code compact mirror of `AGENTS.md`. |
| [`docs/CONTEXT_FOR_AI.md`](docs/CONTEXT_FOR_AI.md) | Minimal canonical facts for AI assistants. |
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

## Registered strategies

| Key | Module |
|-----|--------|
| `aggressive_momentum` | `strategies/aggressive_momentum.py` |
| `etf_momentum` | `strategies/etf_momentum.py` |
| `mean_reversion` | `strategies/mean_reversion.py` |
| `rsi_accumulator` | `strategies/rsi_accumulator.py` |
| `rsi_compounder` | `strategies/rsi_compounder.py` |
| `rsi_recovery` | `strategies/rsi_recovery.py` |
| `rsi_rotation` | `strategies/rsi_rotation.py` |
| `sharp_dip` | `strategies/sharp_dip.py` |
| `trend_momentum` | `strategies/trend_momentum.py` |

To add a strategy: implement under `strategies/`, register in `STRATEGY_REGISTRY` in `core/runner.py`, add a `strategies:` YAML block and optional `bots:` row.

---

## IBKR automation (IBC)

The system uses [IBC](https://github.com/IbcAlpha/IBC) to start and log in to IBKR Gateway automatically — no manual interaction required each morning.

**Infrastructure (on the trading machine, not in this repo):**

| File | Purpose |
|------|---------|
| `C:\IBC\StartGateway_Ferran.bat paper\|live` | Launches paper (port 4002) or live (port 4001) Gateway via IBC |
| `C:\IBC\start_trading_session.bat` | Master script called by Task Scheduler on session unlock |
| `C:\IBC\config_ferran_paper.ini` | IBC credentials + settings for paper account |
| `C:\IBC\config_ferran_live.ini` | IBC credentials + settings for live account |

**Morning flow (fully automatic):**

1. Laptop wakes from sleep → screen unlocked
2. Task Scheduler fires `start_trading_session.bat` (weekdays only)
3. Paper Gateway (port 4002) starts and logs in via IBC
4. Live Gateway (port 4001) starts and logs in via IBC
5. Wait 90 seconds for both to connect
6. `main.py --once` runs all enabled bots
7. Both Gateways are shut down automatically

**First-time setup per machine:**
1. Install IBKR Gateway offline installer to `C:\Jts\`
2. Extract IBC to `C:\IBC\`, set credentials in `config_ferran_*.ini`
3. Run `StartGateway_Ferran.bat paper` once manually to create settings dirs and configure API port
4. Add a Task Scheduler task with `SessionUnlock` trigger pointing to `start_trading_session.bat`
5. For live accounts: whitelist the machine's IP in IBKR Client Portal → Settings → IP Restrictions (eliminates 2FA)

For IBKR contract resolution:
```powershell
python scripts/resolve_contracts.py   # generates data/contracts.json (gitignored)
```

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
- IBC config files (contain IBKR credentials) live at `C:\IBC\` — outside this repo.
- The file `initial chat.pdf` is excluded by `.gitignore` (local brief).

---

## License

All rights reserved unless you add an explicit `LICENSE` file.
