"""Mispricing scanner + staleness check + random audit.

    python scan/scanner.py              # one scan
    python scan/scanner.py --notify     # also fire SMS / RSS for new opportunities
    python scan/scanner.py --list       # print pending opportunities and exit
    python scan/scanner.py --no-audit   # skip the (paid, ~1 cent) LLM audit sample

For each trusted verified pair it takes the latest quotes, works out the
executable two-leg price (ask on the side you buy YES, bid on the side you buy
NO), subtracts both platforms' taker fees and a safety margin, and logs an
opportunity when the result is positive. Safeguards, each counted in scan_runs:
  * Staleness check: if a market's rules_text hash no longer matches the hash
    stored at verification time, a platform edited its resolution criteria —
    the pair drops back to needs_review and is NOT traded.
  * Liquidity floor: a leg below the volume floor has an untrustworthy price.
  * Plausibility cap: a huge edge on a verified-same event means the match or a
    price is wrong — quarantined to needs_review.
  * Random audit: a small sample is re-verified by the LLM each scan regardless
    of hash, to catch verifications that were wrong to begin with.
"""
import argparse
import os
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scan"))
sys.path.insert(0, str(ROOT / "match"))
sys.path.insert(0, str(ROOT / "alerts"))
from anthropic import AuthenticationError, PermissionDeniedError  # noqa: E402
from data import connect, load_latest_markets  # noqa: E402
from pricing import is_plausible_edge, liquid_enough, pair_edge  # noqa: E402
from verify import (CredentialError, classify, health_check,  # noqa: E402
                    rules_hash, verify_pair)

AUDIT_FRACTION = float(os.environ.get("SCAN_AUDIT_FRACTION", "0.01"))

STAT_KEYS = ("pairs_scanned", "raw_signals", "blocked_stale", "blocked_illiquid",
             "blocked_implausible", "already_open", "new_opportunities", "expired",
             "audit_checked", "audit_flagged")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _index(markets):
    return {m["market_id"]: m for m in markets}


def _to_review(con, aid, bid, kind, detail, now) -> None:
    """Pull a verified pair back to needs_review and record which safeguard did it."""
    con.execute("UPDATE verified_pairs SET status='needs_review' "
                "WHERE market_a=? AND market_b=?", [aid, bid])
    con.execute("INSERT INTO safeguard_events (event_at, market_a, market_b, kind, detail) "
                "VALUES (?,?,?,?,?)", [now, aid, bid, kind, detail])
    con.execute("UPDATE opportunities SET status='expired', closed_at=? "
                "WHERE market_a=? AND market_b=? AND status='pending'", [now, aid, bid])


def _expire_stale(con, poly, kalshi, trusted, now) -> int:
    """Retire pending opportunities that no longer hold: the pair lost trust, a
    market disappeared or went illiquid, or the edge closed / became implausible.
    Keeps `pending` an accurate list of live opportunities for --list / alerts."""
    pending = con.execute(
        "SELECT id, market_a, market_b FROM opportunities WHERE status='pending'").fetchall()
    expired = 0
    for oid, aid, bid in pending:
        a, b = poly.get(aid), kalshi.get(bid)
        e = pair_edge(a, b) if a and b else None
        stale = ((aid, bid) not in trusted or e is None or e["net_edge"] <= 0
                 or not is_plausible_edge(e["net_edge"])
                 or not liquid_enough(a["volume"], b["volume"]))
        if stale:
            con.execute("UPDATE opportunities SET status='expired', closed_at=? "
                        "WHERE id=? AND status='pending'", [now, oid])
            expired += 1
    return expired


def scan(con) -> tuple[list[dict], dict, list[tuple]]:
    """One pass over the trusted pairs. Returns (new opportunities, stats, the
    (aid, bid, a, b) pairs still trusted — the population the audit samples)."""
    now = _now()
    poly = _index(load_latest_markets(con, "polymarket"))
    kalshi = _index(load_latest_markets(con, "kalshi"))
    verified = con.execute(
        "SELECT market_a, market_b, rules_hash_a, rules_hash_b FROM verified_pairs "
        "WHERE status IN ('auto_accept', 'confirmed')").fetchall()

    stats = dict.fromkeys(STAT_KEYS, 0)
    found, trusted, present = [], set(), []
    for aid, bid, ha, hb in verified:
        a, b = poly.get(aid), kalshi.get(bid)
        if not a or not b:
            continue
        stats["pairs_scanned"] += 1
        e = pair_edge(a, b)
        signal = e is not None and e["net_edge"] > 0
        stats["raw_signals"] += signal

        # --- staleness check (free; runs whether or not there's a gap) ---
        changed = [p for p, m, h in (("polymarket", a, ha), ("kalshi", b, hb))
                   if rules_hash(m["rules_text"]) != h]
        if changed:
            _to_review(con, aid, bid, "stale_rules",
                       f"rules text changed on {', '.join(changed)}", now)
            stats["blocked_stale"] += signal
            print(f"  stale rules -> needs_review: {a['question'][:50]}")
            continue
        trusted.add((aid, bid))
        present.append((aid, bid, a, b))
        if not signal:
            continue

        # Liquidity floor: a thin market's quoted price is a phantom, not an arb.
        if not liquid_enough(a["volume"], b["volume"]):
            stats["blocked_illiquid"] += 1
            continue
        # Too-good-to-be-true: quarantine for review, don't trade it.
        if not is_plausible_edge(e["net_edge"]):
            _to_review(con, aid, bid, "implausible_edge", f"net edge {e['net_edge']:.3f}", now)
            trusted.discard((aid, bid))
            stats["blocked_implausible"] += 1
            print(f"  implausible edge {e['net_edge']:.2f} -> needs_review: {a['question'][:48]}")
            continue
        # Dedupe: don't re-log a pair that already has an open opportunity.
        if con.execute("SELECT 1 FROM opportunities WHERE market_a=? AND market_b=? "
                       "AND status='pending' LIMIT 1", [aid, bid]).fetchone():
            stats["already_open"] += 1
            continue

        oid = con.execute(
            "INSERT INTO opportunities (market_a, market_b, question, price_a, price_b, "
            "spread, net_edge, status, created_at, quote_basis) "
            "VALUES (?,?,?,?,?,?,?,'pending',?,?) RETURNING id",
            [aid, bid, a["question"], e["price_a"], e["price_b"], e["spread"],
             e["net_edge"], now, e["basis"]]).fetchone()[0]
        found.append({"id": oid, "market_a": aid, "market_b": bid, "question": a["question"],
                      "platform_a": "polymarket", "platform_b": "kalshi",
                      "price_a": e["price_a"], "price_b": e["price_b"], "spread": e["spread"],
                      "net_edge": e["net_edge"], "basis": e["basis"]})
        stats["new_opportunities"] += 1
        print(f"  OPPORTUNITY #{oid} edge={e['net_edge']:.3f} ({e['basis']})  {a['question'][:50]}")

    stats["expired"] = _expire_stale(con, poly, kalshi, trusted, now)
    if stats["expired"]:
        print(f"  expired {stats['expired']} stale opportunit{'y' if stats['expired'] == 1 else 'ies'}")
    return found, stats, present


def run_audit(present: list[tuple]) -> tuple[int, list[tuple]]:
    """Re-verify a small random sample with the LLM (no DB lock held while waiting
    on the API). Returns (checked, [(aid, bid, detail) that failed])."""
    if not present:
        return 0, []
    k = max(1, int(len(present) * AUDIT_FRACTION))
    checked, flagged = 0, []
    for aid, bid, a, b in random.sample(present, min(k, len(present))):
        try:
            result = verify_pair(a, b)
        except (AuthenticationError, PermissionDeniedError, CredentialError) as e:
            print(f"[KEY ALERT] audit aborted, Anthropic key problem: {e}", file=sys.stderr)
            break
        except Exception as e:
            print(f"  (audit skipped, LLM unavailable: {type(e).__name__})")
            break
        checked += 1
        if classify(result) != "auto_accept":
            detail = (result.get("resolution_mismatch")
                      or f"same_event={result['same_event']} conf={result['confidence']:.2f}")
            flagged.append((aid, bid, detail[:300]))
            print(f"  audit flagged -> needs_review: {a['question'][:50]}")
    return checked, flagged


def dispatch_alerts(opp: dict) -> None:
    """Fire RSS + SMS for a new opportunity; never let alerting crash a scan."""
    try:
        import rss
        rss.append_opportunity(opp)
    except Exception as e:
        print(f"  (rss skipped: {type(e).__name__})")
    try:
        from sms import notify_sms, twilio_creds
        creds = twilio_creds()
        if creds:
            notify_sms(*creds, opp)
        else:
            print("  (sms skipped: Twilio env not fully set)")
    except Exception as e:
        print(f"  (sms skipped: {type(e).__name__})")


def _alert_key_problem(err, notify: bool) -> None:
    """Make a dead/expired key LOUD: stderr warning + best-effort SMS. Without
    this, an expired key would silently disable the audit safety net."""
    print(f"[KEY ALERT] Anthropic key unavailable — the verification audit is "
          f"DISABLED. Spread scanning continues, but the audit safety net is OFF "
          f"until the key is fixed. Details: {err}", file=sys.stderr)
    if not notify:
        return
    try:
        from sms import send_text, twilio_creds
        creds = twilio_creds()
        if creds:
            send_text(*creds, "pm-framework: Anthropic key problem — "
                              "verification audit disabled. Check the key.")
    except Exception as e:
        print(f"  (key-alert SMS failed: {type(e).__name__})", file=sys.stderr)


def list_pending(con) -> None:
    """Print all still-open (pending) opportunities, best edge first."""
    rows = con.execute(
        "SELECT id, net_edge, spread, price_a, price_b, quote_basis, question "
        "FROM opportunities WHERE status='pending' ORDER BY net_edge DESC").fetchall()
    print(f"{len(rows)} pending opportunit{'y' if len(rows) == 1 else 'ies'}:")
    for _id, edge, spread, pa, pb, basis, q in rows:
        print(f"  #{_id}  edge={edge:.3f} spread={spread:.3f}  "
              f"poly={pa:.3f} kalshi={pb:.3f} [{basis or 'mid'}]  {(q or '')[:48]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--notify", action="store_true", help="send SMS + RSS for new opportunities")
    ap.add_argument("--list", dest="list_only", action="store_true",
                    help="print pending opportunities and exit (no scan)")
    ap.add_argument("--no-audit", action="store_true", help="skip the LLM audit sample")
    args = ap.parse_args()

    if args.list_only:
        con = connect(read_only=True)
        list_pending(con)
        con.close()
        return 0

    # Fail loud, not silent: verify the key before relying on the audit.
    key_ok = True
    if not args.no_audit:
        try:
            health_check()
        except CredentialError as e:
            key_ok = False
            _alert_key_problem(e, notify=args.notify)
        except Exception as e:  # network/transient — don't block the scan
            print(f"(key health check inconclusive: {type(e).__name__}; proceeding)")

    run_at = _now()
    con = connect()
    try:
        opps, stats, present = scan(con)
    finally:
        con.close()

    if key_ok and not args.no_audit:
        stats["audit_checked"], flagged = run_audit(present)
        stats["audit_flagged"] = len(flagged)
    else:
        flagged = []
    flagged_pairs = {(aid, bid) for aid, bid, _ in flagged}

    con = connect()
    try:
        for aid, bid, detail in flagged:
            _to_review(con, aid, bid, "audit_flag", detail, _now())
        con.execute(f"INSERT INTO scan_runs VALUES (?, {', '.join('?' * len(STAT_KEYS))})",
                    [run_at] + [stats[k] for k in STAT_KEYS])
    finally:
        con.close()

    if args.notify:
        for opp in opps:
            if (opp["market_a"], opp["market_b"]) not in flagged_pairs:
                dispatch_alerts(opp)

    print(f"scan complete: {stats['new_opportunities']} new, {stats['raw_signals']} raw signals "
          f"across {stats['pairs_scanned']} pairs")
    return 0 if key_ok else 1  # non-zero so systemd flags the unit / OnFailure fires


if __name__ == "__main__":
    raise SystemExit(main())
