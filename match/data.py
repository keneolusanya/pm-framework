"""Shared helpers for reading market data out of DuckDB.

Ingestion appends a new snapshot each cycle (keyed by fetched_at), so matching
and scanning always want the *latest* snapshot per platform, not all history.
"""
import json
from pathlib import Path

import duckdb

DB_PATH = Path(__file__).resolve().parent.parent / "db" / "markets.duckdb"


def connect(read_only: bool = False) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(DB_PATH), read_only=read_only)


def load_latest_markets(con, platform: str) -> list[dict]:
    """All markets from the most recent snapshot for one platform."""
    rows = con.execute(
        """
        SELECT market_id, question, rules_text, category, close_time,
               outcomes, prices, volume
        FROM markets
        WHERE platform = ?
          AND fetched_at = (SELECT MAX(fetched_at) FROM markets WHERE platform = ?)
        """,
        [platform, platform],
    ).fetchall()

    out = []
    for mid, q, rules, cat, close, outcomes, prices, vol in rows:
        out.append({
            "platform": platform,
            "market_id": mid,
            "question": q or "",
            "rules_text": rules or "",
            "category": cat,
            "close_time": close,
            "outcomes": json.loads(outcomes) if outcomes else [],
            "prices": json.loads(prices) if prices else [],
            "volume": vol or 0.0,
        })
    return out
