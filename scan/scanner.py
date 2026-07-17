"""Day 4 Block 2: mispricing scanner + staleness check + random audit.

    python scan/scanner.py            # one scan
    python scan/scanner.py --notify   # also fire SMS / RSS on new opportunities

For each trusted verified pair it pulls current prices, computes the fee-aware
net edge, and logs an opportunity when the gap clears fees + margin. Two cheap
safeguards run alongside:
  * Staleness check: if a market's rules_text hash no longer matches the hash
    stored at verification time, a platform edited its resolution criteria —
    the pair is dropped back to needs_review and NOT traded.
  * Random audit: a small daily sample is re-verified regardless of hash, to
    catch verifications that were wrong to begin with.
"""
import argparse
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "match"))
from data import connect, load_latest_markets  # noqa: E402
from pricing import gross_spread, net_edge  # noqa: E402
from verify import classify, rules_hash, verify_pair  # noqa: E402

AUDIT_FRACTION = 0.01  # ~1% of verified pairs re-verified per scan


def _index(markets):
    return {m["market_id"]: m for m in markets}


def _yes_price(m):
    return m["prices"][0] if m.get("prices") else None


def scan(con, notify: bool = False) -> list[dict]:
    poly = _index(load_latest_markets(con, "polymarket"))
    kalshi = _index(load_latest_markets(con, "kalshi"))

    verified = con.execute(
        """
        SELECT market_a, market_b, rules_hash_a, rules_hash_b
        FROM verified_pairs WHERE status IN ('auto_accept', 'confirmed')
        """
    ).fetchall()

    now = datetime.now(timezone.utc)
    found = []
    for aid, bid, ha, hb in verified:
        a, b = poly.get(aid), kalshi.get(bid)
        if not a or not b:
            continue

        # --- staleness check (free) ---
        if rules_hash(a["rules_text"]) != ha or rules_hash(b["rules_text"]) != hb:
            con.execute(
                "UPDATE verified_pairs SET status='needs_review' "
                "WHERE market_a=? AND market_b=?", [aid, bid])
            print(f"  stale rules -> needs_review: {a['question'][:50]}")
            continue

        pa, pb = _yes_price(a), _yes_price(b)
        if pa is None or pb is None:
            continue
        edge = net_edge(pa, pb)
        if edge <= 0:
            continue

        # Dedupe: don't re-log a pair that already has an open opportunity.
        existing = con.execute(
            "SELECT 1 FROM opportunities WHERE market_a=? AND market_b=? "
            "AND status='pending' LIMIT 1", [aid, bid]).fetchone()
        if existing:
            continue

        spread = gross_spread(pa, pb)
        con.execute(
            "INSERT INTO opportunities "
            "(market_a, market_b, question, price_a, price_b, spread, net_edge, status, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            [aid, bid, a["question"], pa, pb, spread, edge, "pending", now])
        opp = {"market_a": aid, "market_b": bid, "question": a["question"],
               "platform_a": "polymarket", "platform_b": "kalshi",
               "price_a": pa, "price_b": pb, "spread": spread, "net_edge": edge}
        found.append(opp)
        print(f"  OPPORTUNITY edge={edge:.3f} spread={spread:.3f}  {a['question'][:50]}")

        if notify:
            _dispatch_alerts(opp)

    _random_audit(con, verified, poly, kalshi)
    return found


def _dispatch_alerts(opp):
    """Fire SMS + RSS for a new opportunity; never let alerting crash a scan."""
    try:
        from pathlib import Path as _P
        sys.path.insert(0, str(_P(__file__).resolve().parent.parent / "alerts"))
        import rss
        rss.append_opportunity(opp)
    except Exception as e:
        print(f"  (rss skipped: {type(e).__name__})")
    try:
        import os
        from sms import notify_sms
        sid, tok = os.environ.get("TWILIO_ACCOUNT_SID"), os.environ.get("TWILIO_AUTH_TOKEN")
        frm, to = os.environ.get("TWILIO_FROM_NUMBER"), os.environ.get("TWILIO_TO_NUMBER")
        if all([sid, tok, frm, to]):
            notify_sms(sid, tok, frm, to, opp)
        else:
            print("  (sms skipped: Twilio env not fully set)")
    except Exception as e:
        print(f"  (sms skipped: {type(e).__name__})")


def _random_audit(con, verified, poly, kalshi):
    """Re-verify a small random sample; drop any that fail re-verification."""
    if not verified:
        return
    k = max(1, int(len(verified) * AUDIT_FRACTION))
    for aid, bid, _, _ in random.sample(verified, min(k, len(verified))):
        a, b = poly.get(aid), kalshi.get(bid)
        if not a or not b:
            continue
        try:
            result = verify_pair(a, b)
        except Exception as e:
            print(f"  (audit skipped, LLM unavailable: {type(e).__name__})")
            return
        if classify(result) != "auto_accept":
            con.execute(
                "UPDATE verified_pairs SET status='needs_review' "
                "WHERE market_a=? AND market_b=?", [aid, bid])
            print(f"  audit flagged -> needs_review: {a['question'][:50]}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--notify", action="store_true")
    args = ap.parse_args()
    con = connect()
    opps = scan(con, notify=args.notify)
    con.close()
    print(f"scan complete: {len(opps)} new opportunit{'y' if len(opps)==1 else 'ies'}")
