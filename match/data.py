"""Shared DuckDB access for every component.

Ingestion appends a price snapshot each cycle, so matching and scanning want
the *latest* snapshot per platform, not all history. Market text/rules live in
market_info (one row per market); quotes live in market_prices.

Concurrency: DuckDB allows either one read-write process or several read-only
processes on a file at a time. Ingest, scan, the webhook and the dashboard are
separate processes, so each opens the database briefly and `connect` waits out
another process's lock instead of failing.
"""
import json
import os
import sys
import time
from pathlib import Path

import duckdb

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("PM_DB_PATH") or REPO_ROOT / "db" / "markets.duckdb")
LOCK_WAIT_SEC = float(os.environ.get("DB_LOCK_WAIT_SEC", "120"))

sys.path.insert(0, str(REPO_ROOT / "db"))
from migrate import ensure_schema  # noqa: E402

_schema_checked: set[str] = set()


def connect(read_only: bool = False, wait: float | None = None) -> duckdb.DuckDBPyConnection:
    """Open the database (UTC session), retrying while another process holds the lock.

    Writer connections also apply db/schema.sql + pending migrations once per process.
    """
    path = str(DB_PATH)
    deadline = time.monotonic() + (LOCK_WAIT_SEC if wait is None else wait)
    delay = 0.2
    while True:
        try:
            con = duckdb.connect(path, read_only=read_only)
            break
        except duckdb.IOException as e:
            if "lock" not in str(e).lower() or time.monotonic() >= deadline:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 2.0)
    con.execute("SET TimeZone = 'UTC'")
    if not read_only and path not in _schema_checked:
        ensure_schema(con)
        _schema_checked.add(path)
    return con


def _loads(value):
    return json.loads(value) if value else []


def load_latest_markets(con, platform: str) -> list[dict]:
    """All markets from the most recent snapshot for one platform."""
    rows = con.execute(
        """
        SELECT p.market_id, i.question, i.rules_text, i.category, i.close_time,
               i.outcomes, p.prices, p.volume, p.yes_bid, p.yes_ask,
               i.token_ids, i.fee_rate, i.tick_size, i.min_size, p.fetched_at
        FROM market_prices p
        JOIN market_info i ON i.platform = p.platform AND i.market_id = p.market_id
        WHERE p.platform = ?
          AND p.fetched_at = (SELECT MAX(fetched_at) FROM market_prices WHERE platform = ?)
        """,
        [platform, platform],
    ).fetchall()

    out = []
    for (mid, q, rules, cat, close, outcomes, prices, vol, bid, ask,
         tokens, fee_rate, tick, min_size, fetched_at) in rows:
        out.append({
            "platform": platform,
            "market_id": mid,
            "question": q or "",
            "rules_text": rules or "",
            "category": cat,
            "close_time": close,
            "outcomes": _loads(outcomes),
            "prices": _loads(prices),
            "volume": vol or 0.0,
            "yes_bid": bid,
            "yes_ask": ask,
            "token_ids": _loads(tokens),
            "fee_rate": fee_rate,
            "tick_size": tick,
            "min_size": min_size,
            "fetched_at": fetched_at,
        })
    return out


def latest_snapshot_sql() -> str:
    """SQL for a CTE named `latest`: the newest snapshot row of every market on
    each platform, joined to its info. Cheap: filters to MAX(fetched_at) rather
    than ranking all history."""
    return """
        latest AS (
            SELECT p.platform, p.market_id, i.question, p.prices, p.yes_bid, p.yes_ask,
                   p.volume, p.fetched_at
            FROM market_prices p
            JOIN market_info i ON i.platform = p.platform AND i.market_id = p.market_id
            JOIN (SELECT platform, MAX(fetched_at) AS ts FROM market_prices GROUP BY platform) m
              ON m.platform = p.platform AND m.ts = p.fetched_at
        )
    """
