"""Reset PostgreSQL sequences after SQLite → Supabase migration.

When migrate_to_supabase.py inserts rows with explicit integer IDs, the
PostgreSQL auto-increment sequences are not updated.  The next INSERT then
generates id=1, which already exists → UniqueViolation.

Run this once after any migration:
    python scripts/fix_sequences.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text
from core.db import engine

TABLES = [
    "bots",
    "trades",
    "positions",
    "equity_snapshots",
    "run_logs",
    "errors",
    "capital_adjustments",
]

eng = engine()
with eng.connect() as conn:
    for table in TABLES:
        row = conn.execute(text(f"SELECT MAX(id) FROM {table}")).fetchone()
        max_id = row[0] if row and row[0] is not None else 0
        new_val = max(max_id, 1)
        seq = f"{table}_id_seq"
        conn.execute(text(f"SELECT setval('{seq}', {new_val})"))
        conn.commit()
        print(f"  {table}: max_id={max_id} -> sequence set to {new_val}")

print("Done — sequences reset.")
