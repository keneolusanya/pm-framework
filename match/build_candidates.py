"""Day 2 orchestrator: markets -> candidate_pairs.

    python match/build_candidates.py [--min-sim 0.55] [--k 3]

Pipeline: load latest snapshot -> tag canonical categories (drop unmapped) ->
embed within category, keep top-k above threshold -> apply the close_time
window -> write candidate_pairs.
"""
import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from categorize import categorize, load_category_map  # noqa: E402
from data import connect, load_latest_markets  # noqa: E402
from embed import top_k_candidates  # noqa: E402


def _tag(markets, cmap):
    tagged = []
    for m in markets:
        cat = categorize(m, cmap)
        if cat is not None:
            tagged.append({**m, "canonical_category": cat})
    return tagged


def build(min_sim: float = 0.80, k: int = 1, window_hours: int = 48) -> int:
    con = connect()
    poly = load_latest_markets(con, "polymarket")
    kalshi = load_latest_markets(con, "kalshi")

    cmap = load_category_map()
    poly_t, kalshi_t = _tag(poly, cmap), _tag(kalshi, cmap)
    print(f"mapped markets: polymarket={len(poly_t)}/{len(poly)}, "
          f"kalshi={len(kalshi_t)}/{len(kalshi)}")

    candidates = top_k_candidates(poly_t, kalshi_t, k=k, min_sim=min_sim)
    print(f"embedding candidates (sim >= {min_sim}): {len(candidates)}")

    # Apply the close_time window to the small candidate set.
    close_a = {m["market_id"]: m["close_time"] for m in poly_t}
    close_b = {m["market_id"]: m["close_time"] for m in kalshi_t}
    window = timedelta(hours=window_hours)
    kept = []
    for pid, kid, cat, sim in candidates:
        ca, cb = close_a.get(pid), close_b.get(kid)
        if ca and cb and abs(ca - cb) > window:
            continue
        kept.append((pid, kid, cat, sim))
    print(f"after {window_hours}h close_time window: {len(kept)}")

    now = datetime.now(timezone.utc)
    con.execute("DELETE FROM candidate_pairs")  # rebuild fresh each run
    if kept:
        con.executemany(
            "INSERT INTO candidate_pairs VALUES (?, ?, ?, ?, ?)",
            [(pid, kid, cat, sim, now) for pid, kid, cat, sim in kept],
        )
    con.close()
    return len(kept)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-sim", type=float, default=0.80)
    ap.add_argument("--k", type=int, default=1)
    ap.add_argument("--window-hours", type=int, default=48)
    args = ap.parse_args()
    n = build(min_sim=args.min_sim, k=args.k, window_hours=args.window_hours)
    print(f"wrote {n} rows to candidate_pairs")
