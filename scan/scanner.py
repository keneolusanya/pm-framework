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
from anthropic import AuthenticationError, PermissionDeniedError  # noqa: E402
from data import connect, load_latest_markets  # noqa: E402
from pricing import gross_spread, is_plausible_edge, liquid_enough, net_edge  # noqa: E402
from verify import (CredentialError, classify, health_check,  # noqa: E402
                    rules_hash, verify_pair)

AUDIT_FRACTION = 0.01  # ~1% of verified pairs re-verified per scan


def _index(markets):
    return {m["market_id"]: m for m in markets}


def _yes_price(m):
    return m["prices"][0] if m.get("prices") else None


def _expire_stale(con, poly, kalshi) -> None:
    """Retire pending opportunities that no longer hold at current prices — the
    gap converged, a market went illiquid/away, or the edge is now implausible.
    Keeps `pending` an accurate list of live opportunities for --list / alerts."""
    pending = con.execute(
        "SELECT id, market_a, market_b FROM opportunities WHERE status='pending'"
    ).fetchall()
    expired = 0
    for oid, aid, bid in pending:
        a, b = poly.get(aid), kalshi.get(bid)
        stale = not a or not b or not a["prices"] or not b["prices"]
        if not stale:
            pa, pb = a["prices"][0], b["prices"][0]
            edge = net_edge(pa, pb)
            stale = (not liquid_enough(a["volume"], b["volume"])
                     or edge <= 0 or not is_plausible_edge(edge))
        if stale:
            con.execute("UPDATE opportunities SET status='expired' WHERE id=?", [oid])
            expired += 1
    if expired:
        print(f"  expired {expired} stale opportunit{'y' if expired == 1 else 'ies'}")


def scan(con, notify: bool = False, run_audit: bool = True) -> list[dict]:
    poly = _index(load_latest_markets(con, "polymarket"))
    kalshi = _index(load_latest_markets(con, "kalshi"))

    _expire_stale(con, poly, kalshi)

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
        # Liquidity floor: a thin market's quoted price is unreliable, so a gap
        # against it is a phantom (stale last-trade), not a real arb.
        if not liquid_enough(a["volume"], b["volume"]):
            continue
        edge = net_edge(pa, pb)
        if edge <= 0:
            continue
        # Too-good-to-be-true: a large gap on a verified-same event means the
        # match or a price is wrong — quarantine for review, don't trade it.
        if not is_plausible_edge(edge):
            con.execute("UPDATE verified_pairs SET status='needs_review' "
                        "WHERE market_a=? AND market_b=?", [aid, bid])
            print(f"  implausible edge {edge:.2f} -> needs_review: {a['question'][:48]}")
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

    if run_audit:
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
        except (AuthenticationError, PermissionDeniedError, CredentialError) as e:
            # Key died/expired mid-scan — this is the loud case, not a quiet skip.
            print(f"[KEY ALERT] audit aborted, Anthropic key problem: {e}",
                  file=sys.stderr)
            return
        except Exception as e:
            print(f"  (audit skipped, LLM unavailable: {type(e).__name__})")
            return
        if classify(result) != "auto_accept":
            con.execute(
                "UPDATE verified_pairs SET status='needs_review' "
                "WHERE market_a=? AND market_b=?", [aid, bid])
            print(f"  audit flagged -> needs_review: {a['question'][:50]}")


def _alert_key_problem(err, notify: bool) -> None:
    """Make a dead/expired key LOUD: stderr warning + best-effort SMS. Without
    this, an expired key would silently disable the audit safety net."""
    msg = (f"[KEY ALERT] Anthropic key unavailable — the verification audit is "
           f"DISABLED. Spread scanning continues, but the staleness/audit safety "
           f"net is OFF until the key is fixed. Details: {err}")
    print(msg, file=sys.stderr)
    if not notify:
        return
    try:
        import os
        from sms import send_text
        creds = [os.environ.get(k) for k in
                 ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN",
                  "TWILIO_FROM_NUMBER", "TWILIO_TO_NUMBER")]
        if all(creds):
            send_text(*creds, "pm-framework: Anthropic key problem — "
                              "verification audit disabled. Check the key.")
    except Exception as e:
        print(f"  (key-alert SMS failed: {type(e).__name__})", file=sys.stderr)


def list_pending(con) -> None:
    """Print all still-open (pending) opportunities, best edge first."""
    rows = con.execute(
        "SELECT id, net_edge, spread, price_a, price_b, question "
        "FROM opportunities WHERE status='pending' ORDER BY net_edge DESC"
    ).fetchall()
    print(f"{len(rows)} pending opportunit{'y' if len(rows)==1 else 'ies'}:")
    for _id, edge, spread, pa, pb, q in rows:
        print(f"  #{_id}  edge={edge:.3f} spread={spread:.3f}  "
              f"poly={pa:.2f} kalshi={pb:.2f}  {(q or '')[:48]}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--notify", action="store_true")
    ap.add_argument("--list", dest="list_only", action="store_true",
                    help="print pending opportunities and exit (no scan)")
    args = ap.parse_args()

    if args.list_only:
        con = connect(read_only=True)
        list_pending(con)
        con.close()
        raise SystemExit(0)

    # Fail loud, not silent: verify the key before relying on the audit.
    key_ok = True
    try:
        health_check()
    except CredentialError as e:
        key_ok = False
        _alert_key_problem(e, notify=args.notify)
    except Exception as e:  # network/transient — don't block the scan
        print(f"(key health check inconclusive: {type(e).__name__}; proceeding)")

    con = connect()
    opps = scan(con, notify=args.notify, run_audit=key_ok)
    con.close()
    print(f"scan complete: {len(opps)} new opportunit{'y' if len(opps)==1 else 'ies'}")
    if not key_ok:
        raise SystemExit(1)  # non-zero so systemd flags the unit / OnFailure fires
