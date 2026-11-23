import pytest

from pricing import (executable_prices, leg_fee, net_edge, pair_edge,
                     polymarket_fee_rate)


def test_fee_formula_is_symmetric_and_peaks_at_half():
    assert leg_fee(0.5, 0.07) == pytest.approx(0.0175)
    assert leg_fee(0.2, 0.07) == pytest.approx(leg_fee(0.8, 0.07))
    assert leg_fee(0.0, 0.07) == 0


@pytest.mark.parametrize("enabled,fee_type,question,expected", [
    (False, "politics_fees", "anything", 0.0),
    (True, "politics_fees", "Will X win the 2028 presidential election?", 0.05),
    (True, "politics_fees", "Will Trump visit Germany in 2026?", 0.01),
    (True, "crypto_fees_v2", "Bitcoin up or down - 15 minute", 0.05),
    (True, "crypto_fees_v2", "Will Bitcoin hit $150k in 2026?", 0.01),
    (True, "sports_fees_v3", "", 0.05),
    (True, "economics_fees", "", 0.01),
    (True, "finance_prices_fees", "", 0.05),
    (True, "some_new_fees", "", 0.05),   # unknown schedule -> conservative default
    (None, None, "", 0.05),
])
def test_polymarket_fee_rate(enabled, fee_type, question, expected):
    assert polymarket_fee_rate(enabled, fee_type, question) == expected


def test_executable_prices_buys_poly_yes_when_poly_ask_below_kalshi_bid():
    a = {"yes_bid": 0.40, "yes_ask": 0.42, "prices": [0.41, 0.59]}
    b = {"yes_bid": 0.50, "yes_ask": 0.52, "prices": [0.51, 0.49]}
    assert executable_prices(a, b) == (0.42, 0.50, "book")


def test_executable_prices_buys_kalshi_yes_when_kalshi_ask_below_poly_bid():
    a = {"yes_bid": 0.60, "yes_ask": 0.62, "prices": [0.61, 0.39]}
    b = {"yes_bid": 0.50, "yes_ask": 0.52, "prices": [0.51, 0.49]}
    assert executable_prices(a, b) == (0.60, 0.52, "book")


def test_executable_prices_falls_back_to_mid_without_a_full_book():
    a = {"yes_bid": None, "yes_ask": 0.42, "prices": [0.41, 0.59]}
    b = {"yes_bid": 0.50, "yes_ask": 0.52, "prices": [0.51, 0.49]}
    assert executable_prices(a, b) == (0.41, 0.51, "mid")


def test_book_edge_is_smaller_than_mid_edge():
    """Crossing the spread costs money; mid-price edges overstate the arb."""
    a = {"platform": "polymarket", "yes_bid": 0.40, "yes_ask": 0.44, "prices": [0.42, 0.58], "fee_rate": 0.05}
    b = {"platform": "kalshi", "yes_bid": 0.50, "yes_ask": 0.54, "prices": [0.52, 0.48], "fee_rate": 0.07}
    book = pair_edge(a, b)
    mid = net_edge(0.42, 0.52, 0.05, 0.07)
    assert book["basis"] == "book"
    assert book["net_edge"] < mid


def test_net_edge_subtracts_both_fees_and_margin():
    edge = net_edge(0.40, 0.50, poly_fee_rate=0.05, kalshi_fee_rate=0.07, margin=0.02)
    assert edge == pytest.approx(0.10 - 0.05 * 0.4 * 0.6 - 0.07 * 0.5 * 0.5 - 0.02)
