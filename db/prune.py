"""Retention for market_prices: keep recent snapshots, keep matched markets forever.

    python db/prune.py               # delete price rows older than DB_KEEP_DAYS (default 3)
    python db/prune.py --days 7
    python db/prune.py --dry-run     # report what would be deleted

At ~48k markets every 5 minutes, market_prices grows by ~14M rows a day. The
scanner and dashboard only need the latest snapshot, so old rows are pruned —
except for markets that appear in verified_pairs, whose full price history is
the research dataset and is never deleted. ingest_runs keeps the throughput
record, so metrics survive pruning. Runs daily via deploy/pm-prune.timer.
"""
import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "match"))
from data import connect  # noqa: E402

KEEP_DAYS = float(os.environ.get("DB_KEEP_DAYS", "3"))

WHERE = """
    fetched_at < ?
    AND NOT EXISTS (
        SELECT 1 FROM verified_pairs v
        WHERE (market_prices.platform = 'polymarket' AND v.market_a = market_prices.market_id)
           OR (market_prices.platform = 'kalshi' AND v.market_b = market_prices.market_id))
"""


def prune(days: float, dry_run: bool = False) -> int:
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    con = connect()
    try:
        n = con.execute(f"SELECT count(*) FROM market_prices WHERE {WHERE}", [cutoff]).fetchone()[0]
        if not dry_run and n:
            con.execute(f"DELETE FROM market_prices WHERE {WHERE}", [cutoff])
            con.execute("CHECKPOINT")  # make freed blocks reusable
    finally:
        con.close()
    return n


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=KEEP_DAYS)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    n = prune(args.days, args.dry_run)
    verb = "would delete" if args.dry_run else "deleted"
    print(f"{verb} {n:,} price rows older than {args.days:g} days (matched markets kept)")
