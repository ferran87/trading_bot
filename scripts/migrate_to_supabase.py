"""One-time migration: copies all data from local SQLite → Supabase PostgreSQL.

Usage
-----
Set DATABASE_URL to your Supabase connection string, then run:

    $env:DATABASE_URL = "postgresql://postgres:<password>@db.<ref>.supabase.co:5432/postgres"
    python scripts/migrate_to_supabase.py

The script is safe to re-run — it uses UPSERT (merge) so existing rows are
updated rather than duplicated.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> None:
    pg_url = os.getenv("DATABASE_URL", "")
    if not pg_url or "sqlite" in pg_url:
        print(
            "ERROR: set DATABASE_URL to your Supabase PostgreSQL connection string.\n"
            "  $env:DATABASE_URL = \"postgresql://postgres:<password>@db.<ref>.supabase.co:5432/postgres\""
        )
        sys.exit(1)

    if pg_url.startswith("postgres://"):
        pg_url = pg_url.replace("postgres://", "postgresql://", 1)

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from core.config import DATA_DIR
    from core.db import (
        Base,
        Bot,
        CapitalAdjustment,
        EquitySnapshot,
        ErrorLog,
        Position,
        RunLog,
        Trade,
    )

    sqlite_url = f"sqlite:///{(DATA_DIR / 'trades.db').as_posix()}"
    if not (DATA_DIR / "trades.db").exists():
        print(f"ERROR: SQLite DB not found at {DATA_DIR / 'trades.db'}")
        sys.exit(1)

    print(f"Source : {sqlite_url}")
    print(f"Target : {pg_url[:60]}...")
    print()

    src_engine = create_engine(sqlite_url)
    dst_engine = create_engine(pg_url)

    # Create all tables in PostgreSQL (idempotent)
    print("Creating schema in PostgreSQL...")
    Base.metadata.create_all(dst_engine)
    print("OK Schema ready\n")

    SrcSession = sessionmaker(bind=src_engine, expire_on_commit=False)
    DstSession = sessionmaker(bind=dst_engine, expire_on_commit=False)

    # Migrate in FK-dependency order
    models = [Bot, Trade, Position, EquitySnapshot, RunLog, ErrorLog, CapitalAdjustment]

    with SrcSession() as src, DstSession() as dst:
        for Model in models:
            rows = src.query(Model).all()
            if not rows:
                print(f"  {Model.__tablename__}: empty — skipped")
                continue

            for row in rows:
                src.expunge(row)   # detach from SQLite session
                dst.merge(row)     # INSERT or UPDATE based on primary key

            dst.commit()
            print(f"  OK {Model.__tablename__}: {len(rows)} rows migrated")

    print("\nDONE Migration complete!")
    print("Next steps:")
    print("  1. Add DATABASE_URL to your .env file (for the bot)")
    print("  2. Add DATABASE_URL to Streamlit Cloud secrets (for the dashboard)")
    print("  3. Push your code to GitHub and deploy on Streamlit Cloud")


if __name__ == "__main__":
    main()
