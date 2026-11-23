"""Scanner safeguards: each one blocks, records why, and never creates a tradeable signal."""
import scanner
from conftest import add_market, add_verified


def _stats(con):
    opps, stats, present = scanner.scan(con)
    return opps, stats


def test_clean_gap_becomes_book_priced_opportunity(db):
    add_market(db, "polymarket", "P1", bid=0.40, ask=0.42)
    add_market(db, "kalshi", "K1", bid=0.55, ask=0.57)
    add_verified(db, "P1", "K1")
    opps, stats = _stats(db)
    assert len(opps) == 1 and opps[0]["basis"] == "book"
    assert opps[0]["price_a"] == 0.42 and opps[0]["price_b"] == 0.55
    assert stats["raw_signals"] == 1 and stats["new_opportunities"] == 1
    # Re-scanning doesn't duplicate the open opportunity.
    opps, stats = _stats(db)
    assert opps == [] and stats["already_open"] == 1


def test_edited_rules_pull_pair_to_review(db):
    add_market(db, "polymarket", "P1", bid=0.40, ask=0.42, rules="new rules")
    add_market(db, "kalshi", "K1", bid=0.55, ask=0.57)
    add_verified(db, "P1", "K1", rules_a="old rules")
    opps, stats = _stats(db)
    assert opps == [] and stats["blocked_stale"] == 1
    assert db.execute("SELECT status FROM verified_pairs").fetchone()[0] == "needs_review"
    assert db.execute("SELECT kind FROM safeguard_events").fetchone()[0] == "stale_rules"


def test_thin_market_is_not_a_signal(db):
    add_market(db, "polymarket", "P1", bid=0.40, ask=0.42, volume=10)
    add_market(db, "kalshi", "K1", bid=0.55, ask=0.57)
    add_verified(db, "P1", "K1")
    opps, stats = _stats(db)
    assert opps == [] and stats["blocked_illiquid"] == 1


def test_implausible_edge_is_quarantined(db):
    add_market(db, "polymarket", "P1", bid=0.10, ask=0.12)
    add_market(db, "kalshi", "K1", bid=0.80, ask=0.82)
    add_verified(db, "P1", "K1")
    opps, stats = _stats(db)
    assert opps == [] and stats["blocked_implausible"] == 1
    assert db.execute("SELECT status FROM verified_pairs").fetchone()[0] == "needs_review"


def test_pending_opportunity_expires_when_gap_closes(db):
    add_market(db, "polymarket", "P1", bid=0.40, ask=0.42)
    add_market(db, "kalshi", "K1", bid=0.55, ask=0.57)
    add_verified(db, "P1", "K1")
    _stats(db)
    # Next snapshot: prices converge.
    add_market(db, "polymarket", "P1", bid=0.54, ask=0.55,
               fetched_at=db.execute("SELECT max(fetched_at) + INTERVAL 5 MINUTE FROM market_prices").fetchone()[0])
    add_market(db, "kalshi", "K1", bid=0.55, ask=0.56,
               fetched_at=db.execute("SELECT max(fetched_at) FROM market_prices").fetchone()[0])
    opps, stats = _stats(db)
    assert stats["expired"] == 1
    assert db.execute("SELECT status FROM opportunities").fetchone()[0] == "expired"
