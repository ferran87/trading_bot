"""One-time migration: add triggered_by column to run_logs."""
import sqlalchemy as sa
from core.db import engine as get_engine

with get_engine().connect() as conn:
    # Works for both PostgreSQL and SQLite
    try:
        dialect = conn.dialect.name
        if dialect == "postgresql":
            conn.execute(sa.text(
                "ALTER TABLE run_logs ADD COLUMN IF NOT EXISTS triggered_by TEXT DEFAULT 'auto'"
            ))
        else:  # sqlite
            cols = [r[1] for r in conn.execute(sa.text("PRAGMA table_info(run_logs)")).fetchall()]
            if "triggered_by" not in cols:
                conn.execute(sa.text(
                    "ALTER TABLE run_logs ADD COLUMN triggered_by TEXT DEFAULT 'auto'"
                ))
            else:
                print("Column already exists — nothing to do.")
                exit(0)
        conn.commit()
        print("Column 'triggered_by' added to run_logs.")
    except Exception as e:
        print(f"Error: {e}")
