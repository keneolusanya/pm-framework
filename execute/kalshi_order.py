"""Day 5 Block 2: Kalshi order placement (authenticated).

Reuses the Day 1 RSA-PSS signer for POST /trade-api/v2/portfolio/orders.
Defaults to Kalshi's demo environment — always place and cancel a test order in
demo before pointing at production. Requires KALSHI_KEY_ID + the private key at
KALSHI_PRIVATE_KEY_PATH; raises if they're absent rather than silently no-op.
"""
import os
import sys
import uuid
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ingest"))
from kalshi import kalshi_auth_headers, load_private_key  # noqa: E402

DEMO_BASE = "https://demo-api.kalshi.co"
PROD_BASE = "https://api.elections.kalshi.com"
ORDERS_PATH = "/trade-api/v2/portfolio/orders"


def place_kalshi_order(ticker: str, side: str, count: int,
                       price_cents: int, demo: bool = True) -> dict:
    """Place a limit order. side is 'yes' or 'no'; price_cents is 1-99.

    Returns the parsed order response. This actually hits the API when creds
    are present — call it only from a human-confirmed path.
    """
    key_id = os.environ.get("KALSHI_KEY_ID")
    private_key = load_private_key()
    if not key_id or private_key is None:
        raise RuntimeError("KALSHI_KEY_ID / KALSHI_PRIVATE_KEY_PATH not configured")

    base = DEMO_BASE if demo else PROD_BASE
    headers = kalshi_auth_headers("POST", ORDERS_PATH, key_id, private_key)
    headers["Content-Type"] = "application/json"
    body = {
        "ticker": ticker,
        "action": "buy",
        "side": side,
        "count": count,
        "type": "limit",
        "yes_price": price_cents if side == "yes" else None,
        "no_price": price_cents if side == "no" else None,
        "client_order_id": str(uuid.uuid4()),
    }
    body = {k: v for k, v in body.items() if v is not None}

    r = httpx.post(f"{base}{ORDERS_PATH}", json=body, headers=headers, timeout=30)
    r.raise_for_status()
    return r.json()
