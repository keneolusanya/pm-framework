"""Polymarket ingestion via the public Gamma API.

Normalizes open markets into the shared `markets` schema. `outcomes` and
`prices` come back from Gamma as JSON-encoded strings (not arrays), so we
decode them here; `prices` is a list of floats aligned to `outcomes`.
"""
import json
import os

import httpx

GAMMA = "https://gamma-api.polymarket.com"


PAGE_SIZE = 500  # Gamma caps each page near 100; page until exhausted anyway.
# Upper bound on markets pulled per run (the loop stops earlier on an empty
# page). It's a coverage/cost dial, not a hard requirement — env-tunable.
DEFAULT_MAX_MARKETS = int(os.environ.get("INGEST_POLY_MAX", "6000"))


def fetch_polymarket_markets(max_markets: int = DEFAULT_MAX_MARKETS,
                             timeout: float = 30.0) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    with httpx.Client(timeout=timeout) as client:
        while len(rows) < max_markets:
            r = client.get(
                f"{GAMMA}/markets",
                # Sort by volume desc: since Gamma caps pagination at ~2000, spend
                # that budget on the most liquid (tradeable, matchable) markets
                # rather than an arbitrary slice of joke/illiquid ones.
                params={"active": "true", "closed": "false",
                        "order": "volumeNum", "ascending": "false",
                        "limit": PAGE_SIZE, "offset": offset},
            )
            # Gamma refuses offsets past ~2000 with a 422 — that's the paginable
            # ceiling for this endpoint, so treat it as end-of-data, not an error.
            if r.status_code == 422:
                break
            r.raise_for_status()
            page = r.json()
            if not page:
                break
            for m in page:
                # Skip rows missing the fields we can't normalize without.
                try:
                    rows.append({
                        "platform": "polymarket",
                        "market_id": m["conditionId"],
                        "question": m["question"],
                        "rules_text": m.get("description", ""),
                        "category": m.get("category"),
                        "close_time": m.get("endDate"),
                        # These three come back as JSON-encoded strings, not arrays.
                        "outcomes": json.loads(m["outcomes"]),
                        "prices": [float(p) for p in json.loads(m["outcomePrices"])],
                        "token_ids": json.loads(m["clobTokenIds"]),  # kept for Day 5 execution
                        "volume": float(m.get("volume") or 0),
                    })
                except (KeyError, ValueError, TypeError):
                    continue
            offset += len(page)
    return rows


if __name__ == "__main__":
    markets = fetch_polymarket_markets()
    print(f"fetched {len(markets)} polymarket markets")
    for m in markets[:3]:
        print(f"  {m['question'][:60]!r} outcomes={m['outcomes']} prices={m['prices']}")
