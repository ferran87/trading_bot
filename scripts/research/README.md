# Offline research scripts

These modules are **not** imported by the live paper-trading loop (`main.py`) or Task Scheduler.

Run manually from the repository root (examples use the Windows venv interpreter):

```text
.venv\Scripts\python.exe -m scripts.research.optimize_bot4
.venv\Scripts\python.exe -m scripts.research.diagnose_sharp_dip
.venv\Scripts\python.exe -m scripts.research.diagnose_hold_extension
```

Generated CSVs are written under `analysis/out/` (gitignored).
