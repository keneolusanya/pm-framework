"""Bring any pm-framework database up to the current schema, idempotently.

    python db/migrate.py          # normally unnecessary: every writer connection runs it

Handles two one-time upgrades of databases created before the split schema:
  1. Timestamps: early versions let DuckDB store UTC datetimes converted to the
     machine's local time zone. They are rewritten to naive UTC once.
  2. Storage: the single `markets` snapshot table is split into market_info
     (one row per market) + market_prices (one row per snapshot), and
     `markets` becomes a view over them.
"""
import sys
from pathlib import Path

import duckdb

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

LEGACY_TABLES = ("markets", "candidate_pairs", "verified_pairs", "opportunities", "trades")

# (table, column) pairs written by pre-UTC code.
LEGACY_TIMESTAMP_COLUMNS = (
    ("markets_legacy", "close_time"),
    ("markets_legacy", "fetched_at"),
    ("candidate_pairs", "created_at"),
    ("verified_pairs", "verified_at"),
    ("opportunities", "created_at"),
    ("trades", "created_at"),
)


def _table_type(con, name: str) -> str | None:
    row = con.execute(
        "SELECT table_type FROM information_schema.tables "
        "WHERE table_schema = 'main' AND table_name = ?", [name]).fetchone()
    return row[0] if row else None


def _meta_get(con, key: str) -> str | None:
    row = con.execute("SELECT value FROM schema_meta WHERE key = ?", [key]).fetchone()
    return row[0] if row else None


def _meta_set(con, key: str, value: str) -> None:
    con.execute("INSERT OR REPLACE INTO schema_meta VALUES (?, ?)", [key, value])


def _system_timezone() -> str:
    """The zone DuckDB used by default on this machine (what legacy rows are in)."""
    tmp = duckdb.connect()
    try:
        return tmp.execute("SELECT current_setting('TimeZone')").fetchone()[0]
    finally:
        tmp.close()


def ensure_schema(con) -> None:
    """Apply schema.sql plus any pending one-time migrations. Safe to run often."""
    had_meta = _table_type(con, "schema_meta") is not None
    had_data = any(_table_type(con, t) is not None for t in LEGACY_TABLES)
    con.execute("CREATE TABLE IF NOT EXISTS schema_meta (key VARCHAR PRIMARY KEY, value VARCHAR)")

    if not had_meta and not had_data:
        _meta_set(con, "utc_timestamps", "fresh")  # created by current code: already UTC

    legacy_markets = _table_type(con, "markets") == "BASE TABLE"
    if legacy_markets:
        con.execute("ALTER TABLE markets RENAME TO markets_legacy")

    if _meta_get(con, "utc_timestamps") is None:
        src_tz = _system_timezone()
        for table, col in LEGACY_TIMESTAMP_COLUMNS:
            if _table_type(con, table) == "BASE TABLE":
                con.execute(
                    f"UPDATE {table} SET {col} = timezone('UTC', timezone(?, {col})) "
                    f"WHERE {col} IS NOT NULL", [src_tz])
        _meta_set(con, "utc_timestamps", f"converted_from:{src_tz}")

    con.execute(SCHEMA_PATH.read_text())

    if legacy_markets:
        _split_legacy_markets(con)


def _split_legacy_markets(con) -> None:
    con.execute("""
        INSERT INTO market_info (platform, market_id, question, rules_text, category,
                                 close_time, outcomes, first_seen, last_seen)
        SELECT platform, market_id,
               arg_max(question, fetched_at), arg_max(rules_text, fetched_at),
               arg_max(category, fetched_at), arg_max(close_time, fetched_at),
               arg_max(outcomes, fetched_at), min(fetched_at), max(fetched_at)
        FROM markets_legacy
        GROUP BY platform, market_id
        ON CONFLICT DO NOTHING
    """)
    con.execute("""
        INSERT INTO market_prices (platform, market_id, prices, volume, fetched_at)
        SELECT platform, market_id, prices, volume, fetched_at FROM markets_legacy
    """)
    con.execute("""
        INSERT INTO ingest_runs (fetched_at, polymarket_rows, kalshi_rows)
        SELECT fetched_at,
               count(*) FILTER (WHERE platform = 'polymarket'),
               count(*) FILTER (WHERE platform = 'kalshi')
        FROM markets_legacy GROUP BY fetched_at
    """)
    con.execute("DROP TABLE markets_legacy")
    _meta_set(con, "split_markets", "done")


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "match"))
    from data import connect  # runs ensure_schema on open

    connect().close()
    print("schema up to date")
