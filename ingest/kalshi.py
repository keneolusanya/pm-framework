"""Kalshi ingestion.

The public `/markets` endpoint needs no auth, but we build the RSA-PSS request
signer now because Day 5 execution requires it.

Data-shape notes (the API differs from older docs):
  * Price/volume fields come as strings in the `*_dollars` / `*_fp` families,
    e.g. `yes_bid_dollars="0.5300"`. A YES contract settles at $1, so the
    dollar price *is* the probability already (0-1) — no cents conversion.
  * Most open markets are illiquid (all-zero prices); we keep only markets
    with a real price signal and bound how many we scan so pagination always
    terminates.
  * Markets carry `yes_sub_title` / `event_ticker`, not a full `title` or
    `category`; a proper question needs the event title (a Day 2 enrichment).
"""
import base64
import os
import time

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import load_pem_private_key

BASE = "https://api.elections.kalshi.com"
MARKETS_PATH = "/trade-api/v2/markets"


def kalshi_auth_headers(method, path, key_id, private_key):
    ts = str(int(time.time() * 1000))
    msg = f"{ts}{method}{path}".encode()
    sig = private_key.sign(
        msg,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=hashes.SHA256().digest_size),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
    }


def load_private_key():
    """Load the RSA key from KALSHI_PRIVATE_KEY_PATH, or None if unavailable."""
    path = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
    if not path:
        return None
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return load_pem_private_key(f.read(), password=None)


def _f(x) -> float:
    """Kalshi numeric fields arrive as strings; parse leniently to float."""
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _yes_prob(market) -> float | None:
    """Best available YES probability in 0-1, or None if the market is unpriced."""
    bid, ask = _f(market.get("yes_bid_dollars")), _f(market.get("yes_ask_dollars"))
    if bid > 0 and ask > 0:
        return (bid + ask) / 2
    last = _f(market.get("last_price_dollars"))
    return last if last > 0 else None


def fetch_kalshi_markets(scan_cap: int = 10000, timeout: float = 30.0) -> list[dict]:
    # Auth is optional for this read; sign only if a key id is configured.
    key_id = os.environ.get("KALSHI_KEY_ID")
    private_key = load_private_key() if key_id else None

    rows: list[dict] = []
    scanned = 0
    cursor = None
    with httpx.Client(timeout=timeout) as client:
        while scanned < scan_cap:
            params = {"status": "open", "limit": 1000}
            if cursor:
                params["cursor"] = cursor

            headers = {}
            if key_id and private_key:
                headers = kalshi_auth_headers("GET", MARKETS_PATH, key_id, private_key)

            r = client.get(f"{BASE}{MARKETS_PATH}", params=params, headers=headers)
            r.raise_for_status()
            body = r.json()

            page = body.get("markets", [])
            if not page:
                break
            scanned += len(page)
            for m in page:
                prob = _yes_prob(m)
                if prob is None:
                    continue  # illiquid / unpriced — skip
                rows.append({
                    "platform": "kalshi",
                    "market_id": m["ticker"],
                    "question": m.get("yes_sub_title") or m["ticker"],
                    "rules_text": m.get("rules_primary", ""),
                    "category": m.get("category"),  # not present today; kept for parity
                    "close_time": m.get("close_time"),
                    "outcomes": ["Yes", "No"],
                    "prices": [round(prob, 4), round(1 - prob, 4)],
                    "volume": _f(m.get("volume_fp")),
                })

            cursor = body.get("cursor")
            if not cursor:
                break
    return rows


if __name__ == "__main__":
    markets = fetch_kalshi_markets()
    print(f"fetched {len(markets)} priced kalshi markets")
    for m in markets[:3]:
        print(f"  {m['market_id']:<24} {m['question'][:35]!r} prices={m['prices']} vol={m['volume']}")
