"""Day 4 Block 1: probability normalization + fee-aware edge.

Prices are already stored as 0-1 YES probabilities by ingestion (Kalshi's
`*_dollars` and Polymarket's `outcomePrices` are both 0-1). What lives here is
the fee model and the net-edge calculation the scanner thresholds on.

Fee schedules change — these are constants to review against the live fee pages,
not gospel. Sources (check before trusting):
  * Kalshi trading fee: ceil(0.07 * contracts * p * (1-p)) per fill. As a
    fraction of $1 notional that's 0.07 * p * (1-p). Some series are 0.035.
  * Polymarket: 0% maker/taker trading fee currently (gas on Polygon is separate
    and tiny). Encoded as 0 but kept explicit so it's easy to update.
"""

import os

KALSHI_FEE_RATE = 0.07          # applied as rate * p * (1-p) per $1 notional
POLYMARKET_FEE_RATE = 0.0       # no trading fee today
SAFETY_MARGIN = 0.02            # extra cushion (slippage, price move between legs)

# Liquidity floors. A thin market's quoted price is unreliable (often a stale
# last-trade), so a "gap" against it is usually a phantom, not a real arb.
# Units differ by platform: Polymarket volume is USD, Kalshi is contracts.
MIN_VOLUME_POLYMARKET = float(os.environ.get("SCAN_MIN_VOL_POLY", "500"))
MIN_VOLUME_KALSHI = float(os.environ.get("SCAN_MIN_VOL_KALSHI", "100"))

# Sanity cap: real cross-platform arbs on verified-same events are small. A gap
# this large means the match or a price is wrong — quarantine, don't trade it.
MAX_PLAUSIBLE_EDGE = float(os.environ.get("SCAN_MAX_EDGE", "0.25"))


def kalshi_fee(price: float) -> float:
    """Kalshi fee as a fraction of $1 notional at a given YES probability."""
    return KALSHI_FEE_RATE * price * (1.0 - price)


def polymarket_fee(price: float) -> float:
    return POLYMARKET_FEE_RATE * price * (1.0 - price)


def gross_spread(price_a: float, price_b: float) -> float:
    """Absolute price gap = gross arbitrage edge per $1 before costs."""
    return abs(price_a - price_b)


def liquid_enough(poly_volume: float, kalshi_volume: float) -> bool:
    """Both legs must clear their platform's volume floor for the quoted prices
    to be trustworthy enough to act on."""
    return (poly_volume >= MIN_VOLUME_POLYMARKET
            and kalshi_volume >= MIN_VOLUME_KALSHI)


def is_plausible_edge(edge: float) -> bool:
    """A gap larger than the cap is almost certainly a bad match or stale price."""
    return edge <= MAX_PLAUSIBLE_EDGE


def net_edge(price_a: float, price_b: float,
             margin: float = SAFETY_MARGIN) -> float:
    """Spread after both legs' fees and a safety margin.

    The two-leg arb (buy YES on the cheap side, NO on the expensive side) costs
    p_cheap + (1 - p_expensive) for a $1 payoff, so gross edge == the spread.
    Fees are charged near each leg's execution price.
    """
    spread = gross_spread(price_a, price_b)
    fees = polymarket_fee(price_a) + kalshi_fee(price_b)
    return spread - fees - margin
