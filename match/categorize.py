"""Assign a canonical category to a market using match/category_map.csv.

Kalshi markets already carry a native category (from the event); we map that to
our canonical label. Polymarket markets have no category at all, so we infer one
by keyword-matching the question text. Markets we can't place return None and
are dropped by the structural filter.
"""
import csv
from pathlib import Path

MAP_PATH = Path(__file__).resolve().parent / "category_map.csv"


def load_category_map() -> list[dict]:
    with open(MAP_PATH, newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["_poly_kw"] = [k.strip().lower() for k in r["polymarket_keywords"].split("|") if k.strip()]
        r["_kalshi_cats"] = {c.strip().lower() for c in r["kalshi_categories"].split("|") if c.strip()}
    return rows


def categorize(market: dict, cmap: list[dict]) -> str | None:
    """Return the canonical category for a market, or None if unmapped."""
    if market["platform"] == "kalshi":
        native = (market.get("category") or "").strip().lower()
        for r in cmap:
            if native in r["_kalshi_cats"]:
                return r["category"]
        return None

    # Polymarket: keyword-match the question text.
    text = (market.get("question") or "").lower()
    for r in cmap:
        if any(kw in text for kw in r["_poly_kw"]):
            return r["category"]
    return None
