"""Fee model, executable prices, and the net-edge calculation the scanner thresholds on.

Prices are 0-1 YES probabilities. Both platforms now charge takers with the
same shape of formula, so the fee on one leg per $1 of payoff is

    fee = rate * p * (1 - p)          (symmetric: a NO leg at 1-p costs the same)

Sources (re-check periodically; schedules change):
  * Kalshi: general taker rate 0.07 (some series differ; makers usually free).
    https://kalshi.com/docs/kalshi-fee-schedule.pdf
  * Polymarket: per-category taker coefficients, makers never charged.
    Schedule effective 2026-11-20 23:59 UTC:
    https://docs.polymarket.com/programs/fee-and-rebate-changes-november-2026
    Each Gamma market says whether fees apply (feesEnabled) and which schedule
    (feeType); polymarket_fee_rate() maps that to a coefficient at ingest time.

Arb legs are assumed to be taker orders (we cross the spread to get both legs
filled), so taker rates apply on both sides.
"""
import os

KALSHI_FEE_RATE = 0.07
SAFETY_MARGIN = float(os.environ.get("SCAN_SAFETY_MARGIN", "0.02"))  # slippage / price moves between legs

# Polymarket feeType -> taker coefficient (2026-11-20 schedule).
# politics and crypto are split further by question text, see polymarket_fee_rate().
POLYMARKET_FEE_BY_TYPE = {
    "sports": 0.05,
    "finance": 0.05,
    "tech": 0.05,
    "general": 0.05,
    "combo": 0.05,
    "economics": 0.01,
    "culture": 0.01,
    "weather": 0.01,
    "mentions": 0.01,
    "geopolitics": 0.0,
}
POLYMARKET_DEFAULT_FEE = 0.05   # unknown/unlisted schedule: assume the higher rate
_ELECTION_WORDS = ("election", "nominee", "nomination", "primary", "electoral",
                   "popular vote", "win the", "presidential", "senate race",
                   "governor race", "mayor", "seat")
_SHORT_CRYPTO_WORDS = ("up or down", "5 minute", "15 minute", "5-minute", "15-minute")

# Liquidity floors. A thin market's quoted price is unreliable (often a stale
# last trade), so a "gap" against it is usually a phantom, not a real arb.
# Units differ by platform: Polymarket volume is USD, Kalshi is contracts.
MIN_VOLUME_POLYMARKET = float(os.environ.get("SCAN_MIN_VOL_POLY", "500"))
MIN_VOLUME_KALSHI = float(os.environ.get("SCAN_MIN_VOL_KALSHI", "100"))

# Sanity cap: real cross-platform arbs on verified-same events are small. A gap
# this large means the match or a price is wrong — quarantine, don't trade it.
MAX_PLAUSIBLE_EDGE = float(os.environ.get("SCAN_MAX_EDGE", "0.25"))


def polymarket_fee_rate(fees_enabled, fee_type: str | None, question: str = "") -> float:
    """Taker coefficient for one Polymarket market, from its Gamma fee fields."""
    if fees_enabled is False:
        return 0.0
    if not fee_type:
        return POLYMARKET_DEFAULT_FEE
    t = fee_type.lower()
    q = (question or "").lower()
    if t.startswith("politics"):
        return 0.05 if any(w in q for w in _ELECTION_WORDS) else 0.01
    if t.startswith("crypto"):
        return 0.05 if any(w in q for w in _SHORT_CRYPTO_WORDS) else 0.01
    for prefix, rate in POLYMARKET_FEE_BY_TYPE.items():
        if t.startswith(prefix):
            return rate
    return POLYMARKET_DEFAULT_FEE


def leg_fee(price: float, rate: float) -> float:
    """Taker fee per $1 payoff for one leg at YES price `price`."""
    return rate * price * (1.0 - price)


def _valid_quote(bid, ask) -> bool:
    return (bid is not None and ask is not None and 0 < bid <= ask < 1)


def executable_prices(a: dict, b: dict) -> tuple[float, float, str] | None:
    """YES prices the two arb legs would actually transact against.

    a = Polymarket market dict, b = Kalshi market dict (from load_latest_markets).
    With a live book on both sides there are two possible arbs:
      * buy YES on Polymarket at its ask, buy NO on Kalshi at 1 - Kalshi bid
        -> returns (poly_ask, kalshi_bid)
      * buy YES on Kalshi at its ask, buy NO on Polymarket at 1 - Polymarket bid
        -> returns (poly_bid, kalshi_ask)
    whichever is better. Either way |price_a - price_b| is the gross edge and
    execute/trade.py derives the same two legs from (price_a, price_b).
    Without a book on both sides, falls back to mid/last prices ('mid' basis).
    """
    if _valid_quote(a.get("yes_bid"), a.get("yes_ask")) and _valid_quote(b.get("yes_bid"), b.get("yes_ask")):
        poly_yes_edge = b["yes_bid"] - a["yes_ask"]
        kalshi_yes_edge = a["yes_bid"] - b["yes_ask"]
        if poly_yes_edge >= kalshi_yes_edge:
            return a["yes_ask"], b["yes_bid"], "book"
        return a["yes_bid"], b["yes_ask"], "book"
    if a.get("prices") and b.get("prices"):
        return a["prices"][0], b["prices"][0], "mid"
    return None


def gross_spread(price_a: float, price_b: float) -> float:
    """Absolute price gap = gross arbitrage edge per $1 payoff before costs."""
    return abs(price_a - price_b)


def liquid_enough(poly_volume: float, kalshi_volume: float) -> bool:
    return (poly_volume >= MIN_VOLUME_POLYMARKET
            and kalshi_volume >= MIN_VOLUME_KALSHI)


def is_plausible_edge(edge: float) -> bool:
    return edge <= MAX_PLAUSIBLE_EDGE


def net_edge(price_a: float, price_b: float,
             poly_fee_rate: float = POLYMARKET_DEFAULT_FEE,
             kalshi_fee_rate: float = KALSHI_FEE_RATE,
             margin: float = SAFETY_MARGIN) -> float:
    """Gross edge minus both legs' taker fees and a safety margin, per $1 payoff.

    The two-leg arb (YES on the cheap side, NO on the expensive side) costs
    p_cheap + (1 - p_expensive) for a guaranteed $1, so gross edge == spread.
    """
    spread = gross_spread(price_a, price_b)
    fees = leg_fee(price_a, poly_fee_rate) + leg_fee(price_b, kalshi_fee_rate)
    return spread - fees - margin


def market_fee_rate(m: dict) -> float:
    """Fee coefficient for a loaded market dict, defaulting conservatively."""
    if m.get("fee_rate") is not None:
        return m["fee_rate"]
    return KALSHI_FEE_RATE if m.get("platform") == "kalshi" else POLYMARKET_DEFAULT_FEE


def pair_edge(a: dict, b: dict) -> dict | None:
    """Everything the scanner and the execution freshness check need for one pair."""
    px = executable_prices(a, b)
    if px is None:
        return None
    pa, pb, basis = px
    return {
        "price_a": pa, "price_b": pb, "basis": basis,
        "spread": gross_spread(pa, pb),
        "net_edge": net_edge(pa, pb, market_fee_rate(a), market_fee_rate(b)),
    }
