"""Manual review queue for needs_review pairs.

    python match/review.py

Prints each needs_review pair side by side with the LLM's stated reason for
uncertainty and the two live prices, and takes y/n/s:
    y -> status = confirmed   (you vouch for it)
    n -> status = rejected    (not the same event)
    s -> skip for now (stays needs_review)

This is the highest-leverage step: it's what makes verified_pairs trustworthy.
Decisions are persisted immediately, so you can stop and resume anytime. The
database is opened only to save each answer, never while waiting on you.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from data import connect, load_latest_markets  # noqa: E402


def _index(markets):
    return {m["market_id"]: m for m in markets}


def _set_status(aid, bid, status):
    con = connect()
    try:
        con.execute("UPDATE verified_pairs SET status = ? WHERE market_a = ? AND market_b = ?",
                    [status, aid, bid])
    finally:
        con.close()


def main():
    con = connect(read_only=True)
    poly = _index(load_latest_markets(con, "polymarket"))
    kalshi = _index(load_latest_markets(con, "kalshi"))

    queue = con.execute(
        """
        SELECT market_a, market_b, confidence, resolution_mismatch
        FROM verified_pairs WHERE status = 'needs_review'
        ORDER BY confidence DESC
        """
    ).fetchall()
    con.close()

    if not queue:
        print("Nothing in the review queue. 🎉")
        return

    print(f"{len(queue)} pairs to review.  "
          "[y]es / [n]o / [s]kip / [b]ack / [q]uit\n")
    history: list[tuple[int, str, str, bool]] = []  # (index, aid, bid, changed_status)
    i = 0
    while i < len(queue):
        aid, bid, conf, mismatch = queue[i]
        a, b = poly.get(aid), kalshi.get(bid)
        if not a or not b:
            print(f"[{i+1}] (a market is no longer in the latest snapshot — skipping)")
            i += 1
            continue
        print("=" * 72)
        print(f"[{i+1}/{len(queue)}]  LLM confidence={conf:.2f}")
        print(f"  A (polymarket): {a['question']}")
        print(f"     prices={a['prices']}")
        print(f"  B (kalshi):     {b['question']}")
        print(f"     prices={b['prices']}")
        if mismatch:
            print(f"  ⚠ LLM flagged: {mismatch}")
        ans = input("  same event? [y/n/s/b/q]: ").strip().lower()

        if ans == "q":
            break
        if ans == "b":
            if not history:
                print("  (already at the start — nothing to undo)")
                continue
            j, paid, pbid, changed = history.pop()
            if changed:
                _set_status(paid, pbid, "needs_review")
                print(f"  <- undid pair {j+1}, back to needs_review")
            i = j  # re-present that pair
            continue
        if ans == "y":
            new = "confirmed"
        elif ans == "n":
            new = "rejected"
        elif ans == "s":
            history.append((i, aid, bid, False))  # skipped — no status change
            i += 1
            continue
        else:
            continue  # invalid input — re-prompt the same pair

        _set_status(aid, bid, new)
        print(f"  -> {new}")
        history.append((i, aid, bid, True))
        i += 1

    print("\nReview session saved.")


if __name__ == "__main__":
    main()
