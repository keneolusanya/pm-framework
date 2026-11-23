"""Hand-label a random sample of accepted pairs to measure matching precision.

    python match/label_sample.py             # label up to 50 random auto_accept pairs
    python match/label_sample.py --n 100     # bigger sample, tighter confidence interval
    python match/label_sample.py --include-confirmed   # also sample human-confirmed pairs

Precision = of the pairs the pipeline accepted, what fraction really are the
same event with the same resolution rules. The LLM's own confidence can't tell
you that; a human reading a random sample can. For each pair you see both
questions, both rules texts and both close times, and answer:
    y -> genuinely the same event (a correct match)
    n -> not the same event / rules differ enough to break the arb
    s -> skip (can't tell)      q -> quit (progress is saved)

Labels land in precision_labels; scripts/metrics.py turns them into a
precision estimate with a 95% confidence interval. Already-labeled pairs are
never shown again, so you can label in several sittings.
"""
import argparse
import random
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from data import connect, load_latest_markets  # noqa: E402


def _wrap(text: str, width: int = 100, limit: int = 700) -> str:
    text = " ".join((text or "").split())
    if len(text) > limit:
        text = text[:limit] + " …"
    return textwrap.indent(textwrap.fill(text, width), "      ")


def _save(aid, bid, status, is_match) -> None:
    con = connect()
    try:
        con.execute("INSERT OR REPLACE INTO precision_labels VALUES (?,?,?,?,?)",
                    [aid, bid, status, is_match, datetime.now(timezone.utc).replace(tzinfo=None)])
    finally:
        con.close()


def main(n: int, include_confirmed: bool, seed: int | None) -> None:
    statuses = ("auto_accept", "confirmed") if include_confirmed else ("auto_accept",)
    con = connect(read_only=True)
    poly = {m["market_id"]: m for m in load_latest_markets(con, "polymarket")}
    kalshi = {m["market_id"]: m for m in load_latest_markets(con, "kalshi")}
    pool = con.execute(
        f"""
        SELECT v.market_a, v.market_b, v.status, v.confidence
        FROM verified_pairs v
        LEFT JOIN precision_labels l ON l.market_a = v.market_a AND l.market_b = v.market_b
        WHERE v.status IN ({', '.join('?' * len(statuses))}) AND l.market_a IS NULL
        """, list(statuses)).fetchall()
    done = con.execute("SELECT count(*) FROM precision_labels").fetchone()[0]
    con.close()

    # Labels are taken from pairs still listed, so the pairs can be read in full.
    pool = [p for p in pool if p[0] in poly and p[1] in kalshi]
    random.Random(seed).shuffle(pool)
    sample = pool[:n]
    print(f"{done} pairs already labeled; {len(pool)} unlabeled in scope; "
          f"showing {len(sample)}.  [y]es / [n]o / [s]kip / [q]uit\n")

    yes = no = 0
    for i, (aid, bid, status, conf) in enumerate(sample, 1):
        a, b = poly[aid], kalshi[bid]
        print("=" * 104)
        print(f"[{i}/{len(sample)}]  status={status}  LLM confidence={conf:.2f}")
        print(f"  A (polymarket): {a['question']}")
        print(f"     closes {a['close_time']}")
        print(_wrap(a["rules_text"]))
        print(f"  B (kalshi):     {b['question']}")
        print(f"     closes {b['close_time']}")
        print(_wrap(b["rules_text"]))
        while True:
            ans = input("  same event, same resolution? [y/n/s/q]: ").strip().lower()
            if ans in ("y", "n", "s", "q"):
                break
        if ans == "q":
            break
        if ans == "s":
            continue
        _save(aid, bid, status, ans == "y")
        yes += ans == "y"
        no += ans == "n"

    print(f"\nsaved {yes + no} labels this session ({yes} match, {no} not). "
          f"See precision: python scripts/metrics.py")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Label a random sample for precision.")
    ap.add_argument("--n", type=int, default=50, help="pairs to show this session")
    ap.add_argument("--include-confirmed", action="store_true",
                    help="also sample pairs a human already confirmed")
    ap.add_argument("--seed", type=int, default=None, help="fix the sample order")
    args = ap.parse_args()
    main(args.n, args.include_confirmed, args.seed)
