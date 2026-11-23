"""Orchestrator for stages 1-2: markets -> candidate_pairs.

    python match/build_candidates.py [--min-sim 0.80] [--k 1] [--window-hours 48]

Pipeline: load latest snapshot -> tag canonical categories (drop unmapped) ->
structural filter (same category, close_time within window) -> embed within
category, keep top-k above the similarity threshold -> write candidate_pairs.
The funnel counts are recorded in pipeline_runs (see scripts/metrics.py).
"""
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from categorize import load_category_map  # noqa: E402
from data import connect, load_latest_markets  # noqa: E402
from embed import top_k_candidates  # noqa: E402
from filters import count_structural_pairs, tag_categories  # noqa: E402


def build(min_sim: float = 0.80, k: int = 1, window_hours: int = 48) -> int:
    con = connect(read_only=True)
    poly = load_latest_markets(con, "polymarket")
    kalshi = load_latest_markets(con, "kalshi")
    con.close()  # embedding takes a while; don't hold the lock

    cmap = load_category_map()
    poly_t, kalshi_t = tag_categories(poly, cmap), tag_categories(kalshi, cmap)
    cross = len(poly) * len(kalshi)
    structural = count_structural_pairs(poly_t, kalshi_t, window_hours)
    print(f"full cross product: {len(poly):,} x {len(kalshi):,} = {cross:,} pairs")
    print(f"mapped markets: polymarket={len(poly_t)}/{len(poly)}, "
          f"kalshi={len(kalshi_t)}/{len(kalshi)}")
    print(f"stage 1 structural filter (category + {window_hours}h window): {structural:,}")

    kept = top_k_candidates(poly_t, kalshi_t, k=k, min_sim=min_sim, window_hours=window_hours)
    print(f"stage 2 embedding top-{k} (sim >= {min_sim}): {len(kept)}")

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    con = connect()
    try:
        con.execute("BEGIN")
        con.execute("DELETE FROM candidate_pairs")  # rebuild fresh each run
        if kept:
            con.executemany(
                "INSERT INTO candidate_pairs VALUES (?, ?, ?, ?, ?)",
                [(pid, kid, cat, sim, now) for pid, kid, cat, sim in kept],
            )
        con.execute(
            "INSERT INTO pipeline_runs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [now, len(poly), len(kalshi), len(poly_t), len(kalshi_t), cross,
             structural, len(kept), min_sim, k, window_hours])
        con.execute("COMMIT")
    finally:
        con.close()
    return len(kept)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-sim", type=float, default=0.80)
    ap.add_argument("--k", type=int, default=1)
    ap.add_argument("--window-hours", type=int, default=48)
    args = ap.parse_args()
    n = build(min_sim=args.min_sim, k=args.k, window_hours=args.window_hours)
    print(f"wrote {n} rows to candidate_pairs "
          f"(next: python match/run_verify.py — only new pairs cost money)")
