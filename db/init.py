"""Create the DuckDB database file from schema.sql.

Usage: python db/init.py [db_path]
Defaults to db/markets.duckdb next to this script.
"""
import sys
from pathlib import Path

import duckdb

HERE = Path(__file__).resolve().parent
SCHEMA_PATH = HERE / "schema.sql"
DEFAULT_DB_PATH = HERE / "markets.duckdb"


def init_db(db_path: Path = DEFAULT_DB_PATH) -> None:
    schema = SCHEMA_PATH.read_text()
    con = duckdb.connect(str(db_path))
    try:
        con.execute(schema)
    finally:
        con.close()
    print(f"Initialized DuckDB at {db_path}")


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DB_PATH
    init_db(target)
