"""Ingestion orchestrator: fetch both platforms, land them in DuckDB.

    python ingest/run.py           # one ingestion cycle, then exit (what the systemd timer runs)
    python ingest/run.py --loop    # repeat every 5 minutes (local alternative to the timer)

Each cycle upserts market_info (text/rules/fees, one row per market) and appends
one market_prices row per market, so the scanner always reads the latest quotes
while price history is retained (see db/prune.py for retention).

The network fetch happens *before* the database is opened, so the write lock is
held only for the few seconds of the bulk insert — the webhook and dashboard
aren't locked out for the whole pull.
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pandas as pd
from dotenv import load_dotenv

# Allow `python ingest/run.py` from the repo root to import sibling modules.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "match"))
from data import connect  # noqa: E402
from kalshi import fetch_kalshi_markets  # noqa: E402
from polymarket import fetch_polymarket_markets  # noqa: E402

load_dotenv()

LOOP_INTERVAL_SEC = 300  # 5 minutes


def _utc_naive(value):
    """ISO-8601 string (trailing 'Z' allowed) -> naive UTC datetime, or None."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


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
    return []


def _frame(markets: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame([{
        "platform": m["platform"],
        "market_id": m["market_id"],
        "question": m.get("question"),
        "rules_text": m.get("rules_text"),
        "category": m.get("category"),
        "close_time": _utc_naive(m.get("close_time")),
        "outcomes": json.dumps(m.get("outcomes")),
        "token_ids": json.dumps(m["token_ids"]) if m.get("token_ids") else None,
        "fee_rate": m.get("fee_rate"),
        "tick_size": m.get("tick_size"),
        "min_size": m.get("min_size"),
        "prices": json.dumps(m.get("prices")),
        "yes_bid": m.get("yes_bid"),
        "yes_ask": m.get("yes_ask"),
        "volume": float(m.get("volume") or 0),
    } for m in markets])
    # A platform occasionally lists the same market twice in one pull; keep one.
    return df.drop_duplicates(subset=["platform", "market_id"], keep="last")


def write_snapshot(con, markets: list[dict], fetched_at: datetime) -> dict:
    df = _frame(markets)  # noqa: F841 — referenced by name in the SQL below
    con.execute("BEGIN")
    try:
        con.execute("""
            INSERT INTO market_info (platform, market_id, question, rules_text, category,
                                     close_time, outcomes, token_ids, fee_rate, tick_size,
                                     min_size, first_seen, last_seen)
            SELECT platform, market_id, question, rules_text, category, close_time,
                   outcomes::JSON, token_ids::JSON, fee_rate, tick_size, min_size, $ts, $ts
            FROM df
            ON CONFLICT (platform, market_id) DO UPDATE SET
                question = excluded.question, rules_text = excluded.rules_text,
                category = excluded.category, close_time = excluded.close_time,
                outcomes = excluded.outcomes, token_ids = excluded.token_ids,
                fee_rate = excluded.fee_rate, tick_size = excluded.tick_size,
                min_size = excluded.min_size, last_seen = excluded.last_seen
        """, {"ts": fetched_at})
        con.execute("""
            INSERT INTO market_prices (platform, market_id, prices, yes_bid, yes_ask, volume, fetched_at)
            SELECT platform, market_id, prices::JSON, yes_bid, yes_ask, volume, $ts FROM df
        """, {"ts": fetched_at})
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return df.groupby("platform").size().to_dict()


def ingest_once() -> None:
    fetched_at = datetime.now(timezone.utc).replace(tzinfo=None)
    t0 = time.monotonic()
    poly = fetch_with_backoff(fetch_polymarket_markets, name="polymarket")
    kalshi = fetch_with_backoff(fetch_kalshi_markets, name="kalshi")
    fetch_s = time.monotonic() - t0

    t1 = time.monotonic()
    con = connect()
    try:
        counts = write_snapshot(con, poly + kalshi, fetched_at)
        write_s = time.monotonic() - t1
        con.execute("INSERT INTO ingest_runs VALUES (?, ?, ?, ?, ?)",
                    [fetched_at, counts.get("polymarket", 0), counts.get("kalshi", 0),
                     round(fetch_s, 2), round(write_s, 2)])
    finally:
        con.close()
    print(f"[{fetched_at:%Y-%m-%d %H:%M:%SZ}] inserted: "
          + ", ".join(f"{p}={c}" for p, c in sorted(counts.items()))
          + f"  (fetch {fetch_s:.0f}s, write {write_s:.1f}s)")


def main():
    parser = argparse.ArgumentParser(description="pm-framework market ingestion")
    parser.add_argument("--loop", action="store_true",
                        help="run continuously every 5 minutes")
    args = parser.parse_args()

    if not args.loop:
        ingest_once()
        return
    print("Looping every 5 minutes. Ctrl-C to stop.")
    while True:
        started = time.monotonic()
        try:
            ingest_once()
        except Exception as e:  # keep the loop alive across transient errors
            print(f"  cycle failed: {type(e).__name__}: {e}")
        time.sleep(max(0, LOOP_INTERVAL_SEC - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
