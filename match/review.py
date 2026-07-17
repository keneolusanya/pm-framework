"""Day 3 Block 3: manual review queue for needs_review pairs.

    python match/review.py

Prints each needs_review pair side by side with the LLM's stated reason for
uncertainty and the two live prices, and takes y/n/s:
    y -> status = confirmed   (you vouch for it)
    n -> status = rejected    (not the same event)
    s -> skip for now (stays needs_review)

This is the highest-leverage step: it's what makes verified_pairs trustworthy.
Decisions are persisted immediately, so you can stop and resume anytime.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from data import connect, load_latest_markets  # noqa: E402


def _index(markets):
    return {m["market_id"]: m for m in markets}


def main():
    con = connect()
    poly = _index(load_latest_markets(con, "polymarket"))
    kalshi = _index(load_latest_markets(con, "kalshi"))

    queue = con.execute(
        """
        SELECT market_a, market_b, confidence, resolution_mismatch
        FROM verified_pairs WHERE status = 'needs_review'
        ORDER BY confidence DESC
        """
    ).fetchall()

    if not queue:
        print("Nothing in the review queue. 🎉")
        con.close()
        return

    print(f"{len(queue)} pairs to review.  [y]es / [n]o / [s]kip / [q]uit\n")
    for i, (aid, bid, conf, mismatch) in enumerate(queue, 1):
        a, b = poly.get(aid), kalshi.get(bid)
        if not a or not b:
            print(f"[{i}] (a market is no longer in the latest snapshot — skipping)")
            continue
        print("=" * 72)
        print(f"[{i}/{len(queue)}]  LLM confidence={conf:.2f}")
        print(f"  A (polymarket): {a['question']}")
        print(f"     prices={a['prices']}")
        print(f"  B (kalshi):     {b['question']}")
        print(f"     prices={b['prices']}")
        if mismatch:
            print(f"  ⚠ LLM flagged: {mismatch}")
        ans = input("  same event? [y/n/s/q]: ").strip().lower()
        if ans == "q":
            break
        if ans == "y":
            new = "confirmed"
        elif ans == "n":
            new = "rejected"
        else:
            continue  # skip: leave as needs_review
        con.execute(
            "UPDATE verified_pairs SET status = ? WHERE market_a = ? AND market_b = ?",
            [new, aid, bid],
        )
        print(f"  -> {new}")

    con.close()
    print("\nReview session saved.")


if __name__ == "__main__":
    main()
