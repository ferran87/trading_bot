"""One-time migration: add status column to trades table in Supabase."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text
from core.db import engine

eng = engine()
with eng.connect() as conn:
    try:
        conn.execute(text("ALTER TABLE trades ADD COLUMN status TEXT NOT NULL DEFAULT 'filled'"))
        conn.commit()
        print("Migration applied: trades.status column added.")
    except Exception as e:
        print(f"Column likely already exists (OK): {e}")
