"""Ingestion orchestrator: fetch both platforms, land them in DuckDB.

    python ingest/run.py           # one ingestion cycle, then exit
    python ingest/run.py --loop    # repeat every 5 minutes

Each cycle appends a fresh snapshot (rows are keyed by fetched_at), so the
scanner can always read the latest prices while history is retained.
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import httpx
from dotenv import load_dotenv

# Allow `python ingest/run.py` from the repo root to import sibling modules.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from kalshi import fetch_kalshi_markets  # noqa: E402
from polymarket import fetch_polymarket_markets  # noqa: E402

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "db" / "markets.duckdb"
SCHEMA_PATH = REPO_ROOT / "db" / "schema.sql"
LOOP_INTERVAL_SEC = 300  # 5 minutes

# Column order must match db/schema.sql. outcomes/prices are cast to JSON.
INSERT_SQL = """
INSERT INTO markets VALUES
(?, ?, ?, ?, ?, ?, ?::JSON, ?::JSON, ?, ?)
"""


def _parse_ts(value):
    """ISO-8601 string (with trailing 'Z' allowed) -> datetime, or None."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def fetch_with_backoff(fetch_fn, *, name, max_retries=5):
    """Call a fetcher, retrying with exponential backoff on HTTP 429."""
    delay = 2.0
    for attempt in range(1, max_retries + 1):
        try:
            return fetch_fn()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429 and attempt < max_retries:
                print(f"  [{name}] 429 rate-limited; backing off {delay:.0f}s "
                      f"(attempt {attempt}/{max_retries})")
                time.sleep(delay)
                delay *= 2
                continue
            raise
    return []  # unreachable, but keeps the type checker happy


def _to_row(m, fetched_at):
    """Map a normalized market dict onto the schema's column order."""
    return (
        m["platform"],
        m["market_id"],
        m.get("question"),
        m.get("rules_text"),
        m.get("category"),
        _parse_ts(m.get("close_time")),
        json.dumps(m.get("outcomes")),
        json.dumps(m.get("prices")),
        float(m.get("volume") or 0),
        fetched_at,
    )


def ingest_once(con) -> None:
    fetched_at = datetime.now(timezone.utc)

    poly = fetch_with_backoff(fetch_polymarket_markets, name="polymarket")
    kalshi = fetch_with_backoff(fetch_kalshi_markets, name="kalshi")

    rows = [_to_row(m, fetched_at) for m in poly + kalshi]
    con.executemany(INSERT_SQL, rows)

    counts = con.execute(
        "SELECT platform, COUNT(*) FROM markets "
        "WHERE fetched_at = ? GROUP BY platform ORDER BY platform",
        [fetched_at],
    ).fetchall()
    stamp = fetched_at.strftime("%Y-%m-%d %H:%M:%SZ")
    print(f"[{stamp}] inserted: " +
          ", ".join(f"{p}={c}" for p, c in counts))


def main():
    parser = argparse.ArgumentParser(description="pm-framework market ingestion")
    parser.add_argument("--loop", action="store_true",
                        help="run continuously every 5 minutes")
    args = parser.parse_args()

    if not DB_PATH.exists():
        con = duckdb.connect(str(DB_PATH))
        con.execute(SCHEMA_PATH.read_text())
    else:
        con = duckdb.connect(str(DB_PATH))

    try:
        if args.loop:
            print("Looping every 5 minutes. Ctrl-C to stop.")
            while True:
                try:
                    ingest_once(con)
                except Exception as e:  # keep the loop alive across transient errors
                    print(f"  cycle failed: {type(e).__name__}: {e}")
                time.sleep(LOOP_INTERVAL_SEC)
        else:
            ingest_once(con)
    finally:
        con.close()


if __name__ == "__main__":
    main()
