"""The money path: freshness checks, the double-fire guard, risk rails, leg failures."""
from datetime import timedelta

import pytest

import trade
from conftest import add_market, now_utc


def _opportunity(con, price_a=0.42, price_b=0.50) -> int:
    return con.execute(
        "INSERT INTO opportunities (market_a, market_b, question, price_a, price_b, spread, "
        "net_edge, status, created_at, quote_basis) VALUES ('P1','K1','Q?',?,?,?,0.05,'pending',?,'book') "
        "RETURNING id", [price_a, price_b, abs(price_a - price_b), now_utc()]).fetchone()[0]


@pytest.fixture
def live_pair(db):
    add_market(db, "polymarket", "P1", bid=0.40, ask=0.42)
    add_market(db, "kalshi", "K1", bid=0.50, ask=0.52)
    return db


def test_dry_run_places_two_legs_with_polymarket_token(live_pair):
    con = live_pair
    oid = _opportunity(con)
    msg = trade.execute_trade(con, oid, confirmed_by="terminal")
    assert msg.startswith("executed (DRY_RUN")
    legs = con.execute("SELECT platform, side, price, status FROM trades ORDER BY id").fetchall()
    assert legs == [("polymarket", "YES", 0.42, "dry_run"), ("kalshi", "NO", 0.5, "dry_run")]
    assert con.execute("SELECT status FROM opportunities WHERE id=?", [oid]).fetchone()[0] == "actioned"


def test_second_confirmation_is_a_no_op(live_pair):
    """Terminal and SMS confirming the same opportunity must not double-fire."""
    con = live_pair
    oid = _opportunity(con)
    trade.execute_trade(con, oid, confirmed_by="sms")
    msg = trade.execute_trade(con, oid, confirmed_by="terminal")
    assert "already actioned" in msg
    assert con.execute("SELECT count(*) FROM trades").fetchone()[0] == 2


def test_claim_is_atomic(live_pair):
    oid = _opportunity(live_pair)
    assert trade.claim_opportunity(live_pair, oid) is True
    assert trade.claim_opportunity(live_pair, oid) is False


def test_stale_prices_block_execution(db):
    old = now_utc() - timedelta(hours=2)
    add_market(db, "polymarket", "P1", bid=0.40, ask=0.42, fetched_at=old)
    add_market(db, "kalshi", "K1", bid=0.50, ask=0.52, fetched_at=old)
    oid = _opportunity(db)
    msg = trade.execute_trade(db, oid, confirmed_by="sms")
    assert "min old" in msg
    assert db.execute("SELECT status FROM opportunities WHERE id=?", [oid]).fetchone()[0] == "pending"
    assert db.execute("SELECT count(*) FROM trades").fetchone()[0] == 0


def test_closed_edge_expires_instead_of_trading(db):
    add_market(db, "polymarket", "P1", bid=0.49, ask=0.50)
    add_market(db, "kalshi", "K1", bid=0.50, ask=0.51)
    oid = _opportunity(db)
    msg = trade.execute_trade(db, oid, confirmed_by="sms")
    assert "edge has closed" in msg
    assert db.execute("SELECT status FROM opportunities WHERE id=?", [oid]).fetchone()[0] == "expired"
    assert db.execute("SELECT count(*) FROM trades").fetchone()[0] == 0


def test_kill_switch_blocks_and_releases_claim(live_pair, monkeypatch):
    monkeypatch.setenv("RISK_KILL_SWITCH", "1")
    oid = _opportunity(live_pair)
    msg = trade.execute_trade(live_pair, oid, confirmed_by="terminal")
    assert "risk rails" in msg
    assert live_pair.execute("SELECT status FROM opportunities WHERE id=?", [oid]).fetchone()[0] == "pending"
    assert live_pair.execute("SELECT count(*) FROM trades").fetchone()[0] == 0


def test_leg_two_failure_is_reported_and_logged(live_pair, monkeypatch):
    calls = []

    def flaky(leg, count):
        calls.append(leg["platform"])
        if len(calls) == 2:
            raise RuntimeError("exchange down")
        return "DRY_RUN"

    alerts = []
    monkeypatch.setattr(trade, "_place", flaky)
    monkeypatch.setattr(trade, "_alert", alerts.append)
    oid = _opportunity(live_pair)
    msg = trade.execute_trade(live_pair, oid, confirmed_by="sms")
    assert "LEG 2 FAILED" in msg and alerts
    statuses = [r[0] for r in live_pair.execute("SELECT status FROM trades ORDER BY id").fetchall()]
    assert statuses == ["dry_run", "failed"]


def test_missing_token_fails_leg_one_without_sending_leg_two(live_pair):
    live_pair.execute("UPDATE market_info SET token_ids = NULL WHERE market_id = 'P1'")
    oid = _opportunity(live_pair)
    msg = trade.execute_trade(live_pair, oid, confirmed_by="sms")
    assert msg.startswith("FAILED on leg 1")
    assert live_pair.execute("SELECT count(*) FROM trades").fetchone()[0] == 1


def test_kalshi_yes_direction_uses_polymarket_no_token(db):
    add_market(db, "polymarket", "P1", bid=0.60, ask=0.62)
    add_market(db, "kalshi", "K1", bid=0.50, ask=0.52)
    oid = _opportunity(db, price_a=0.60, price_b=0.52)
    opp = db.execute(f"SELECT {trade.OPP_COLS} FROM opportunities WHERE id=?", [oid]).fetchone()
    legs = trade._legs_for(opp, trade._poly_tokens(db, "P1"))
    assert legs[0] == {"platform": "kalshi", "market_id": "K1", "side": "YES", "price": 0.52}
    assert legs[1]["side"] == "NO" and legs[1]["token_id"] == "P1-no"
    assert legs[1]["price"] == pytest.approx(0.40)
