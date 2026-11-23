"""Verify every new candidate pair with Claude, write to verified_pairs.

    python match/run_verify.py --limit 3     # smoke-test a few pairs first (~2 cents)
    python match/run_verify.py               # then the full run (~0.7 cents per new pair)

Needs ANTHROPIC_API_KEY in .env. Resumable: pairs already in verified_pairs are
skipped, so an interrupted run can be re-run later to finish. rules_hash_* are
snapshotted for the scanner's staleness check.
"""
import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from data import connect, load_latest_markets  # noqa: E402
from verify import classify, rules_hash, verify_pair  # noqa: E402


def _index(markets):
    return {m["market_id"]: m for m in markets}


def _save(row) -> None:
    con = connect()
    try:
        con.execute("INSERT OR REPLACE INTO verified_pairs VALUES (?,?,?,?,?,?,?,?,?)", row)
    finally:
        con.close()


def run(limit: int | None = None, throttle: float = 0.5) -> None:
    # Read everything up front and release the lock: the LLM loop takes minutes
    # and ingestion/scanning must keep running meanwhile.
    con = connect(read_only=True)
    poly = _index(load_latest_markets(con, "polymarket"))
    kalshi = _index(load_latest_markets(con, "kalshi"))

    todo = con.execute(
        """
        SELECT market_a, market_b FROM candidate_pairs
        WHERE (market_a, market_b) NOT IN (SELECT market_a, market_b FROM verified_pairs)
        ORDER BY similarity DESC
        """
    ).fetchall()
    con.close()
    if limit:
        todo = todo[:limit]
    print(f"{len(todo)} candidate pairs to verify (already-verified are skipped)")

    accepted = review = rejected = failed = 0
    for i, (aid, bid) in enumerate(todo, 1):
        a, b = poly.get(aid), kalshi.get(bid)
        if not a or not b:
            continue  # market dropped out of the latest snapshot
        try:
            result = verify_pair(a, b)
        except Exception as e:
            failed += 1
            print(f"  [{i}/{len(todo)}] FAILED {type(e).__name__}: {str(e)[:60]}")
            continue

        status = classify(result)
        accepted += status == "auto_accept"
        review += status == "needs_review"
        rejected += status == "auto_reject"
        _save([aid, bid, result["same_event"], result["confidence"],
               result.get("resolution_mismatch"), status,
               rules_hash(a["rules_text"]), rules_hash(b["rules_text"]),
               datetime.now(timezone.utc).replace(tzinfo=None)])
        if i % 10 == 0 or i == len(todo):
            print(f"  [{i}/{len(todo)}] accepted={accepted} rejected={rejected} "
                  f"needs_review={review} failed={failed}")
        time.sleep(throttle)

    print(f"done: auto_accept={accepted}, auto_reject={rejected}, "
          f"needs_review={review}, failed={failed}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Verify candidate pairs with Claude -> verified_pairs.",
        epilog="Tip: run with --limit 3 first to smoke-test (~2 cents) before the "
               "full run. The job is resumable, so the smoke-tested pairs "
               "aren't re-verified.")
    ap.add_argument("--limit", type=int, default=None,
                    help="verify at most N new pairs (highest-similarity first)")
    ap.add_argument("--throttle", type=float, default=0.5,
                    help="seconds to sleep between calls (default 0.5)")
    args = ap.parse_args()
    run(limit=args.limit, throttle=args.throttle)
