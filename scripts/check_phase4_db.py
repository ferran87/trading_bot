"""Verify Phase 4 DB models created correctly."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parents[1]))

from sqlalchemy import inspect
from core.db import engine, Theme, ThemeStockProposal, Thesis  # noqa: F401

eng = engine()
ins = inspect(eng)
tables = ins.get_table_names()

print("Tables present:")
for t in ["themes", "theme_stock_proposals"]:
    status = "OK" if t in tables else "MISSING"
    print(f"  {t}: {status}")

print()
print("Theses columns (should include theme_id, positioning_vs_theme, etc.):")
cols = {c["name"] for c in ins.get_columns("theses")}
for c in [
    "theme_id",
    "positioning_vs_theme",
    "execution_evidence",
    "valuation_assessment",
]:
    status = "OK" if c in cols else "MISSING (run scripts/migrations/phase4_thesis_columns.sql in Supabase)"
    print(f"  {c}: {status}")
