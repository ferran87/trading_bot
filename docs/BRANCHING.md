# Git branching

This repo uses three long-lived branches:

| Branch | Purpose |
|--------|---------|
| **`main`** | Default branch. Day-to-day development and features merge here. |
| **`staging`** | Integration / pre-release testing. Run full tests and paper checks before promoting. |
| **`live`** | Stable snapshot aligned with what you run on the scheduled host (paper or future live). Only merge from `staging` when you intend to deploy. |

## Suggested workflow

1. Create a feature branch from `main`: `git checkout -b feature/short-name`.
2. Open a pull request into `main`; keep commits focused and tests green (`pytest`).
3. When a release candidate is ready, merge `main` → `staging`, run the bot on paper with `BROKER_BACKEND=ibkr`, verify logs and dashboard.
4. When satisfied, merge `staging` → `live` and tag if you want a version marker: `git tag -a v0.2.0 -m "..."`.

## First-time setup on GitHub

- Default branch on GitHub can stay **`main`**.
- Protect **`live`** (and optionally **`staging`**) with required reviews or status checks in repository **Settings → Rules → Rulesets** if collaborators join later.
