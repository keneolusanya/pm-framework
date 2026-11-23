"""Test fixtures: every test gets its own throwaway DuckDB file.

Environment is pinned *before* any project import so a developer's .env can't
leak into tests (python-dotenv never overrides variables that already exist):
execution stays dry-run, the kill switch is off, and Twilio is unset so no test
can send a real SMS.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for env, value in {
    "EXECUTE_LIVE": "0", "KALSHI_DEMO": "1", "RISK_KILL_SWITCH": "0",
    "RISK_MAX_POSITION_USD": "25", "RISK_DAILY_CAP_USD": "200",
    "TWILIO_ACCOUNT_SID": "", "TWILIO_AUTH_TOKEN": "", "TWILIO_FROM_NUMBER": "",
    "TWILIO_TO_NUMBER": "", "POLYMARKET_PRIVATE_KEY": "", "KALSHI_KEY_ID": "",
}.items():
    os.environ[env] = value
for sub in ("match", "scan", "execute", "alerts", "db", "scripts"):
    sys.path.insert(0, str(ROOT / sub))

import pytest  # noqa: E402

import data  # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Point every module at a fresh database file and yield a writer connection."""
    monkeypatch.setattr(data, "DB_PATH", tmp_path / "test.duckdb")
    data._schema_checked.clear()
    con = data.connect()
    yield con
    con.close()


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def add_market(con, platform, market_id, *, bid, ask, volume=10_000, rules="rules",
               fee_rate=None, fetched_at=None, question="Will X happen?"):
    fetched_at = fetched_at or now_utc()
    mid = (bid + ask) / 2
    tokens = json.dumps([f"{market_id}-yes", f"{market_id}-no"]) if platform == "polymarket" else None
    con.execute(
        "INSERT OR REPLACE INTO market_info (platform, market_id, question, rules_text, close_time, "
        "outcomes, token_ids, fee_rate, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [platform, market_id, question, rules, fetched_at + timedelta(days=30), '["Yes","No"]',
         tokens, fee_rate if fee_rate is not None else (0.07 if platform == "kalshi" else 0.05),
         fetched_at, fetched_at])
    con.execute(
        "INSERT INTO market_prices VALUES (?,?,?,?,?,?,?)",
        [platform, market_id, json.dumps([mid, 1 - mid]), bid, ask, volume, fetched_at])


def add_verified(con, poly_id, kalshi_id, rules_a="rules", rules_b="rules", status="auto_accept"):
    from verify import rules_hash
    con.execute("INSERT INTO verified_pairs VALUES (?,?,?,?,?,?,?,?,?)",
                [poly_id, kalshi_id, True, 0.97, None, status,
                 rules_hash(rules_a), rules_hash(rules_b), now_utc()])
