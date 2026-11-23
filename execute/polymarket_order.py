"""Polymarket order placement via py-clob-client.

Order placement goes through py-clob-client, never raw HTTP (the client handles
EIP-712 order signing). There is no Polymarket sandbox, so the first live order
must be trivially small. The Polygon wallet key comes from POLYMARKET_PRIVATE_KEY,
never hardcoded.

Orders are placed per outcome *token* (clobTokenIds from ingestion), not per
market conditionId. If your funds sit in a Polymarket.com account (a proxy
wallet), also set POLYMARKET_FUNDER to that account's address and
POLYMARKET_SIGNATURE_TYPE (1 = email/Magic login, 2 = browser wallet);
a bare EOA wallet needs neither.
"""
import os

CLOB_HOST = "https://clob.polymarket.com"
POLYGON_CHAIN_ID = 137


def _client():
    from py_clob_client.client import ClobClient

    key = os.environ.get("POLYMARKET_PRIVATE_KEY")
    if not key:
        raise RuntimeError("POLYMARKET_PRIVATE_KEY not configured")
    funder = os.environ.get("POLYMARKET_FUNDER") or None
    sig_type = os.environ.get("POLYMARKET_SIGNATURE_TYPE")
    client = ClobClient(CLOB_HOST, key=key, chain_id=POLYGON_CHAIN_ID,
                        signature_type=int(sig_type) if sig_type else None,
                        funder=funder)
    # API credentials are derived from the wallet key on first use.
    client.set_api_creds(client.create_or_derive_api_creds())
    return client


def place_polymarket_order(token_id: str, side: str, size: float,
                           price: float) -> dict:
    """Place a limit order. side is 'BUY' or 'SELL'; price is 0-1; size is shares.

    Actually posts when a wallet key is present — human-confirmed paths only.
    """
    from py_clob_client.clob_types import OrderArgs
    from py_clob_client.order_builder.constants import BUY, SELL

    client = _client()
    args = OrderArgs(
        token_id=token_id,
        price=price,
        size=size,
        side=BUY if side.upper() == "BUY" else SELL,
    )
    signed = client.create_order(args)
    return client.post_order(signed)
