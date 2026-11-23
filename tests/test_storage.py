"""Schema migration, the funnel filter, the webhook's id handling, and metrics math."""
from datetime import datetime, timedelta

import duckdb
import numpy as np
import pytest

import data
from conftest import add_market, now_utc


def test_legacy_database_is_split_and_converted_to_utc(tmp_path, monkeypatch):
    path = tmp_path / "legacy.duckdb"
    raw = duckdb.connect(str(path))
    raw.execute("SET TimeZone='America/New_York'")
    raw.execute("""CREATE TABLE markets (platform VARCHAR, market_id VARCHAR, question VARCHAR,
        rules_text VARCHAR, category VARCHAR, close_time TIMESTAMP, outcomes JSON, prices JSON,
        volume DOUBLE, fetched_at TIMESTAMP NOT NULL)""")
    raw.execute("CREATE TABLE verified_pairs (market_a VARCHAR, market_b VARCHAR, same_event BOOLEAN, "
                "confidence DOUBLE, resolution_mismatch VARCHAR, status VARCHAR, rules_hash_a VARCHAR, "
                "rules_hash_b VARCHAR, verified_at TIMESTAMP NOT NULL, PRIMARY KEY (market_a, market_b))")
    t = datetime(2026, 7, 18, 10, 0)  # stored as New York local time by the old code
    for ts in (t, t + timedelta(minutes=5)):
        raw.execute("INSERT INTO markets VALUES ('kalshi','K1','Q','R',NULL,NULL,'[\"Yes\",\"No\"]',"
                    "'[0.5,0.5]',10,?)", [ts])
    raw.close()

    monkeypatch.setattr(data, "DB_PATH", path)
    data._schema_checked.clear()
    from migrate import _system_timezone
    src_tz = _system_timezone()
    con = data.connect()
    try:
        assert con.execute("SELECT table_type FROM information_schema.tables "
                           "WHERE table_name='markets'").fetchone()[0] == "VIEW"
        assert con.execute("SELECT count(*) FROM market_prices").fetchone()[0] == 2
        assert con.execute("SELECT count(*) FROM market_info").fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM ingest_runs").fetchone()[0] == 2
        expected = con.execute("SELECT timezone('UTC', timezone(?, ?::TIMESTAMP))", [src_tz, t]).fetchone()[0]
        assert con.execute("SELECT min(fetched_at) FROM market_prices").fetchone()[0] == expected
    finally:
        con.close()
    # Idempotent: a second open changes nothing.
    data._schema_checked.clear()
    con = data.connect()
    assert con.execute("SELECT count(*) FROM market_prices").fetchone()[0] == 2
    con.close()


def test_window_mask_keeps_unknown_close_times():
    from filters import window_mask
    a = np.array([0.0, np.nan])
    b = np.array([3600.0 * 47, 3600.0 * 49])
    mask = window_mask(a, b, 48)
    assert mask.tolist() == [[True, False], [True, True]]


def test_wilson_interval():
    from metrics import wilson
    lo, hi = wilson(95, 100)
    assert 0.88 < lo < 0.90 and 0.97 < hi < 0.99


@pytest.fixture
def webhook_client(db, monkeypatch):
    from fastapi.testclient import TestClient
    import webhook
    monkeypatch.setattr(webhook, "_valid_signature", lambda *a: True)
    monkeypatch.setenv("TWILIO_TO_NUMBER", "+15550000000")
    return TestClient(webhook.app), db


def _opp(con, market_a="P1", market_b="K1"):
    return con.execute(
        "INSERT INTO opportunities (market_a, market_b, question, price_a, price_b, spread, net_edge, "
        "status, created_at, quote_basis) VALUES (?,?,'Q?',0.42,0.5,0.08,0.05,'pending',?,'book') "
        "RETURNING id", [market_a, market_b, now_utc()]).fetchone()[0]


def test_webhook_rejects_other_senders(webhook_client):
    client, _ = webhook_client
    r = client.post("/sms-reply", data={"From": "+15559999999", "Body": "Y"})
    assert "Unauthorized" in r.text


def test_webhook_requires_id_when_several_pending(webhook_client):
    client, con = webhook_client
    _opp(con)
    _opp(con, "P2", "K2")
    con.close()  # the webhook opens its own connection
    r = client.post("/sms-reply", data={"From": "+15550000000", "Body": "Y"})
    assert "Several opportunities are open" in r.text


def test_webhook_executes_explicit_id(webhook_client):
    client, con = webhook_client
    add_market(con, "polymarket", "P1", bid=0.40, ask=0.42)
    add_market(con, "kalshi", "K1", bid=0.50, ask=0.52)
    oid = _opp(con)
    con.close()
    r = client.post("/sms-reply", data={"From": "+15550000000", "Body": f"y {oid}"})
    assert "executed (DRY_RUN" in r.text
    r = client.post("/sms-reply", data={"From": "+15550000000", "Body": f"Y {oid}"})
    assert "already actioned" in r.text
