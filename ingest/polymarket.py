"""Polymarket ingestion via the public Gamma API.

Normalizes open markets into the shared schema. `outcomes`, `outcomePrices`
and `clobTokenIds` come back from Gamma as JSON-encoded strings (not arrays),
so we decode them here. We also keep what execution and the fee model need:
the CLOB token ids (orders are placed per outcome token), best bid/ask, tick
size, minimum order size, and the market's taker fee coefficient.
"""
import json
import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scan"))
from pricing import polymarket_fee_rate  # noqa: E402

GAMMA = "https://gamma-api.polymarket.com"

PAGE_SIZE = 500  # Gamma returns at most ~100 per page; we page until exhausted anyway.
# Upper bound on markets pulled per run (the loop stops earlier on an empty
# page). It's a coverage/cost dial, not a hard requirement — env-tunable.
DEFAULT_MAX_MARKETS = int(os.environ.get("INGEST_POLY_MAX", "6000"))


def _num(x):
    try:
        return float(x) if x is not None else None
    except (TypeError, ValueError):
        return None


def _normalize(m: dict) -> dict:
    question = m["question"]
    return {
        "platform": "polymarket",
        "market_id": m["conditionId"],
        "question": question,
        "rules_text": m.get("description", ""),
        "category": m.get("category"),
        "close_time": m.get("endDate"),
        "outcomes": json.loads(m["outcomes"]),
        "prices": [float(p) for p in json.loads(m["outcomePrices"])],
        "token_ids": json.loads(m["clobTokenIds"]),
        "yes_bid": _num(m.get("bestBid")),
        "yes_ask": _num(m.get("bestAsk")),
        "volume": float(m.get("volume") or 0),
        "fee_rate": polymarket_fee_rate(m.get("feesEnabled"), m.get("feeType"), question),
        "tick_size": _num(m.get("orderPriceMinTickSize")),
        "min_size": _num(m.get("orderMinSize")),
    }


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
                try:
                    rows.append(_normalize(m))
                except (KeyError, ValueError, TypeError):
                    continue  # missing a field we can't normalize without
            offset += len(page)
    return rows


if __name__ == "__main__":
    markets = fetch_polymarket_markets()
    print(f"fetched {len(markets)} polymarket markets")
    for m in markets[:3]:
        print(f"  {m['question'][:60]!r} bid={m['yes_bid']} ask={m['yes_ask']} fee={m['fee_rate']}")
