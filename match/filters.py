"""Structural pre-filter: cheaply cut the cross product before embeddings.

A Polymarket x Kalshi pair survives only if:
  (a) both markets map to the same canonical category, and
  (b) their close_times are within a window of each other (default 48h).

Two markets resolving weeks apart, or in unrelated categories, cannot be the
same event — so there's no point paying to embed and LLM-verify them.
"""
from datetime import timedelta

from categorize import categorize, load_category_map


def _tag_categories(markets: list[dict], cmap) -> list[dict]:
    """Attach a canonical category to each market; drop the unmapped ones."""
    tagged = []
    for m in markets:
        cat = categorize(m, cmap)
        if cat is not None:
            tagged.append({**m, "canonical_category": cat})
    return tagged


def structural_pairs(poly: list[dict], kalshi: list[dict],
                     window_hours: int = 48) -> list[tuple[dict, dict]]:
    cmap = load_category_map()
    poly_t = _tag_categories(poly, cmap)
    kalshi_t = _tag_categories(kalshi, cmap)

    # Bucket Kalshi by category so we don't scan the whole list per Polymarket market.
    by_cat: dict[str, list[dict]] = {}
    for m in kalshi_t:
        by_cat.setdefault(m["canonical_category"], []).append(m)

    window = timedelta(hours=window_hours)
    pairs = []
    for pm in poly_t:
        for km in by_cat.get(pm["canonical_category"], []):
            # Only apply the time window when both sides have a close_time.
            if pm["close_time"] and km["close_time"]:
                if abs(pm["close_time"] - km["close_time"]) > window:
                    continue
            pairs.append((pm, km))
    return pairs


if __name__ == "__main__":
    from data import connect, load_latest_markets

    con = connect(read_only=True)
    poly = load_latest_markets(con, "polymarket")
    kalshi = load_latest_markets(con, "kalshi")
    con.close()

    pairs = structural_pairs(poly, kalshi)
    full_cross = len(poly) * len(kalshi)
    print(f"poly={len(poly)} kalshi={len(kalshi)} full cross product={full_cross:,}")
    print(f"survived structural filter: {len(pairs):,} pairs "
          f"({100*len(pairs)/full_cross:.4f}% of cross product)")
