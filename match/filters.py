"""Stage 1 — structural filter: cheaply cut the cross product before embeddings.

A Polymarket x Kalshi pair survives only if:
  (a) both markets map to the same canonical category, and
  (b) their close_times are within a window of each other (default 48h), when
      both sides have one.

Two markets resolving weeks apart, or in unrelated categories, cannot be the
same event — so there's no point embedding-ranking or LLM-verifying them. The
filter is applied as a boolean mask over each category's similarity matrix
(see embed.py), so the full cross product is never materialized.

    python match/filters.py      # print the funnel for the latest snapshot
"""
from datetime import timezone

import numpy as np

from categorize import categorize, load_category_map


def tag_categories(markets: list[dict], cmap) -> list[dict]:
    """Attach a canonical category to each market; drop the unmapped ones."""
    tagged = []
    for m in markets:
        cat = categorize(m, cmap)
        if cat is not None:
            tagged.append({**m, "canonical_category": cat})
    return tagged


def close_seconds(markets: list[dict]) -> np.ndarray:
    """close_time (naive UTC) as float epoch seconds, NaN where unknown."""
    return np.array([m["close_time"].replace(tzinfo=timezone.utc).timestamp()
                     if m.get("close_time") else np.nan
                     for m in markets], dtype=np.float64)


def window_mask(close_a: np.ndarray, close_b: np.ndarray, window_hours: float) -> np.ndarray:
    """(len(a), len(b)) bool matrix: True where the pair passes the time window.
    A pair with an unknown close_time on either side passes (can't rule it out)."""
    diff = np.abs(close_a[:, None] - close_b[None, :])
    return np.isnan(diff) | (diff <= window_hours * 3600.0)


def count_structural_pairs(poly_t: list[dict], kalshi_t: list[dict],
                           window_hours: float = 48) -> int:
    """Number of pairs surviving the structural filter, without materializing them."""
    by_cat_k: dict[str, list[dict]] = {}
    for m in kalshi_t:
        by_cat_k.setdefault(m["canonical_category"], []).append(m)
    by_cat_p: dict[str, list[dict]] = {}
    for m in poly_t:
        by_cat_p.setdefault(m["canonical_category"], []).append(m)
    total = 0
    for cat, ps in by_cat_p.items():
        ks = by_cat_k.get(cat)
        if ks:
            ck = close_seconds(ks)
            cp = close_seconds(ps)
            for start in range(0, len(ps), 512):
                total += int(window_mask(cp[start:start + 512], ck, window_hours).sum())
    return total


if __name__ == "__main__":
    from data import connect, load_latest_markets

    con = connect(read_only=True)
    poly = load_latest_markets(con, "polymarket")
    kalshi = load_latest_markets(con, "kalshi")
    con.close()

    cmap = load_category_map()
    n = count_structural_pairs(tag_categories(poly, cmap), tag_categories(kalshi, cmap))
    full_cross = len(poly) * len(kalshi)
    print(f"poly={len(poly)} kalshi={len(kalshi)} full cross product={full_cross:,}")
    print(f"survived structural filter: {n:,} pairs "
          f"({100 * n / max(full_cross, 1):.4f}% of cross product)")
