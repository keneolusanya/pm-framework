"""Create (or upgrade) the DuckDB database from schema.sql.

Usage: python db/init.py
Uses db/markets.duckdb unless PM_DB_PATH is set. Safe to re-run: the schema
and migrations are idempotent.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "match"))
from data import DB_PATH, connect  # noqa: E402

if __name__ == "__main__":
    connect().close()
    print(f"Initialized DuckDB at {DB_PATH}")
