"""One-off: re-classify existing needs_review pairs with the current rules.

    python match/reclassify.py            # dry-run: list what WOULD auto-reject
    python match/reclassify.py --apply    # actually move them to auto_reject

Uses the LLM outputs already stored in verified_pairs — no new API calls, no
cost. Those outputs came from an earlier prompt, so this only auto-rejects pairs
the model already marked NOT the same event at high confidence. Eyeball the
dry-run list before applying.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from data import connect, load_latest_markets  # noqa: E402
from verify import classify  # noqa: E402


def main(apply: bool) -> None:
    con = connect()
    poly = {m["market_id"]: m for m in load_latest_markets(con, "polymarket")}

    rows = con.execute(
        "SELECT market_a, market_b, same_event, confidence, resolution_mismatch "
        "FROM verified_pairs WHERE status = 'needs_review'"
    ).fetchall()

    to_reject = []
    for aid, bid, same, conf, mismatch in rows:
        status = classify({"same_event": same, "confidence": conf,
                           "resolution_mismatch": mismatch})
        if status == "auto_reject":
            to_reject.append((aid, bid))
            q = poly[aid]["question"][:56] if aid in poly else aid
            print(f"  reject  conf={conf:.2f}  {q}")

    print(f"\n{len(to_reject)} of {len(rows)} needs_review pairs would auto-reject "
          f"(confidently NOT the same event).")
    if not to_reject:
        con.close()
        return
    if apply:
        con.executemany(
            "UPDATE verified_pairs SET status='auto_reject' "
            "WHERE market_a=? AND market_b=?", to_reject)
        print("applied — those pairs are now excluded from your review queue.")
    else:
        print("dry-run only. Re-run with --apply to move them to auto_reject.")
    con.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="actually move the confidently-different pairs to auto_reject")
    main(ap.parse_args().apply)
