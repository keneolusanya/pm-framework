"""Manual-confirm trade execution + the terminal loop.

Nothing here fires automatically. A trade executes only after a human confirms
it — `y` at the terminal (this file) or `Y <id>` by SMS reply (webhook.py). Both
paths call the same execute_trade(), which enforces the concurrency guard:

    the opportunity's status flips pending -> actioned atomically BEFORE any
    order is sent; whichever channel confirms first wins, the other is a no-op.

Before claiming, execute_trade re-checks the opportunity against the latest
snapshot: if prices are too old, or the edge has closed, nothing is sent.

execute_trade dry-runs by default. It places real orders only when EXECUTE_LIVE=1
and the platform credentials are present — and even then only via a confirmed
path. This human-in-the-loop design is deliberate; do not remove it.
"""
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "execute"))
sys.path.insert(0, str(ROOT / "match"))
sys.path.insert(0, str(ROOT / "scan"))
from data import connect  # noqa: E402
from pricing import pair_edge  # noqa: E402
from risk import RiskError, check_trade  # noqa: E402

LIVE = os.environ.get("EXECUTE_LIVE", "").lower() in ("1", "true", "yes")
USE_DEMO = os.environ.get("KALSHI_DEMO", "1").lower() in ("1", "true", "yes")
MAX_PRICE_AGE_MIN = float(os.environ.get("EXECUTE_MAX_PRICE_AGE_MIN", "15"))
DEFAULT_COUNT = int(os.environ.get("EXECUTE_CONTRACTS", "5"))  # Polymarket's usual minimum is 5 shares

OPP_COLS = "id, market_a, market_b, question, price_a, price_b, spread, net_edge, status"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def claim_opportunity(con, opp_id: int) -> bool:
    """Atomically flip pending -> actioned. True only if THIS call won the claim."""
    won = con.execute(
        "UPDATE opportunities SET status='actioned', closed_at=? "
        "WHERE id=? AND status='pending' RETURNING id",
        [_now(), opp_id],
    ).fetchone()
    return won is not None


def _latest(con, platform: str, market_id: str) -> dict | None:
    """Newest snapshot of one market, in the same shape load_latest_markets returns."""
    import json
    row = con.execute(
        """
        SELECT p.prices, p.yes_bid, p.yes_ask, p.volume, p.fetched_at,
               i.outcomes, i.token_ids, i.fee_rate, i.min_size
        FROM market_prices p
        JOIN market_info i ON i.platform = p.platform AND i.market_id = p.market_id
        WHERE p.platform = ? AND p.market_id = ?
        ORDER BY p.fetched_at DESC LIMIT 1
        """, [platform, market_id]).fetchone()
    if not row:
        return None
    prices, bid, ask, vol, fetched_at, outcomes, tokens, fee_rate, min_size = row
    return {"platform": platform, "market_id": market_id,
            "prices": json.loads(prices) if prices else [],
            "yes_bid": bid, "yes_ask": ask, "volume": vol, "fetched_at": fetched_at,
            "outcomes": json.loads(outcomes) if outcomes else [],
            "token_ids": json.loads(tokens) if tokens else [],
            "fee_rate": fee_rate, "min_size": min_size}


def still_live(con, opp) -> str | None:
    """Reason the opportunity should NOT be executed now, or None if it holds."""
    market_a, market_b = opp[1], opp[2]
    a, b = _latest(con, "polymarket", market_a), _latest(con, "kalshi", market_b)
    if not a or not b:
        return "a market is no longer listed"
    age_min = (_now() - min(a["fetched_at"], b["fetched_at"])).total_seconds() / 60
    if age_min > MAX_PRICE_AGE_MIN:
        return (f"latest prices are {age_min:.0f} min old (limit {MAX_PRICE_AGE_MIN:.0f}) "
                f"— is ingestion running?")
    e = pair_edge(a, b)
    if e is None or e["net_edge"] <= 0:
        return "the edge has closed at the latest prices"
    return None


def _poly_tokens(con, market_id: str) -> dict:
    """{'YES': token_id, 'NO': token_id} for a Polymarket market, or {}."""
    m = _latest(con, "polymarket", market_id)
    if not m or not m["token_ids"]:
        return {}
    return {str(o).upper(): t for o, t in zip(m["outcomes"], m["token_ids"])}


def _legs_for(opp, poly_tokens: dict | None = None) -> list[dict]:
    """Two arb legs: buy YES on the cheaper side, buy NO on the other.
    price_a / price_b are the YES prices each side transacts at (see scanner)."""
    _id, market_a, market_b, question, price_a, price_b, spread, edge, status = opp
    poly_tokens = poly_tokens or {}
    if price_a <= price_b:  # polymarket YES cheaper
        return [
            {"platform": "polymarket", "market_id": market_a, "side": "YES",
             "price": round(price_a, 4), "token_id": poly_tokens.get("YES")},
            {"platform": "kalshi", "market_id": market_b, "side": "NO",
             "price": round(1 - price_b, 4)},
        ]
    return [
        {"platform": "kalshi", "market_id": market_b, "side": "YES",
         "price": round(price_b, 4)},
        {"platform": "polymarket", "market_id": market_a, "side": "NO",
         "price": round(1 - price_a, 4), "token_id": poly_tokens.get("NO")},
    ]


def _place(leg, count) -> str:
    """Dispatch a single leg. Returns an order id (or 'DRY_RUN'). Raises on failure."""
    if leg["platform"] == "polymarket" and not leg.get("token_id"):
        raise RuntimeError("no Polymarket token id for this outcome — re-run ingestion")
    if not LIVE:
        return "DRY_RUN"
    if leg["platform"] == "kalshi":
        from kalshi_order import place_kalshi_order
        resp = place_kalshi_order(leg["market_id"], leg["side"].lower(), count,
                                  int(round(leg["price"] * 100)), demo=USE_DEMO)
        return str(resp.get("order", {}).get("order_id", "?"))
    from polymarket_order import place_polymarket_order
    resp = place_polymarket_order(leg["token_id"], "BUY", count, leg["price"])
    if not resp.get("success", True) or resp.get("errorMsg"):
        raise RuntimeError(f"Polymarket rejected order: {resp.get('errorMsg') or resp}")
    return str(resp.get("orderID", "?"))


def _alert(text: str) -> None:
    """Best-effort SMS for an execution problem the human must act on."""
    try:
        sys.path.insert(0, str(ROOT / "alerts"))
        from sms import send_text, twilio_creds
        creds = twilio_creds()
        if creds:
            send_text(*creds, text)
    except Exception as e:
        print(f"  (alert SMS failed: {type(e).__name__})", file=sys.stderr)


def execute_trade(con, opp_id: int, confirmed_by: str, count: int = DEFAULT_COUNT) -> str:
    """Execute a human-confirmed opportunity. Idempotent via the claim guard."""
    opp = con.execute(f"SELECT {OPP_COLS} FROM opportunities WHERE id=?", [opp_id]).fetchone()
    if not opp:
        return f"no opportunity #{opp_id}"
    if opp[8] != "pending":
        return "already actioned (another channel got there first)"

    reason = still_live(con, opp)
    if reason:
        if "edge has closed" in reason or "no longer listed" in reason:
            con.execute("UPDATE opportunities SET status='expired', closed_at=? "
                        "WHERE id=? AND status='pending'", [_now(), opp_id])
        return f"not executed: {reason}"

    if not claim_opportunity(con, opp_id):
        return "already actioned (another channel got there first)"

    legs = _legs_for(opp, _poly_tokens(con, opp[1]))
    stake = sum(leg["price"] * count for leg in legs)
    try:
        check_trade(con, stake)
    except RiskError as e:
        # Undo the claim so the opportunity can be reconsidered.
        con.execute("UPDATE opportunities SET status='pending', closed_at=NULL WHERE id=?", [opp_id])
        return f"blocked by risk rails: {e}"

    mode = "LIVE" if LIVE else "DRY_RUN"
    for i, leg in enumerate(legs):
        try:
            order_id, status, error = _place(leg, count), ("placed" if LIVE else "dry_run"), None
        except Exception as e:
            order_id, status, error = None, "failed", f"{type(e).__name__}: {e}"[:500]
        con.execute(
            "INSERT INTO trades (opportunity_id, platform, market_id, side, price, size, "
            "order_id, created_at, status, error) VALUES (?,?,?,?,?,?,?,?,?,?)",
            [opp_id, leg["platform"], leg["market_id"], leg["side"], leg["price"],
             count, order_id, _now(), status, error])
        if status == "failed":
            if i == 0:
                # Left 'actioned' on purpose: an error after the request was sent
                # can still mean the order exists. Check the platform before retrying.
                return (f"FAILED on leg 1 ({leg['platform']}): {error[:120]}. "
                        f"Leg 2 not sent. Check {leg['platform']} for a stray order.")
            msg = (f"pm-framework #{opp_id}: LEG 2 FAILED ({leg['platform']} {leg['side']}). "
                   f"Leg 1 ({legs[0]['platform']} {legs[0]['side']} @ {legs[0]['price']:.3f}) "
                   f"is OPEN and unhedged — hedge manually now.")
            _alert(msg)
            return msg
    return f"executed ({mode}, confirmed_by={confirmed_by}): 2 legs logged"


def terminal_loop():
    """Show pending opportunities one at a time and confirm each. Ctrl-C to stop.
    The database is only opened between prompts, never while waiting on input."""
    print("Manual trade loop. For each opportunity type y to execute, n to skip.\n")
    while True:
        con = connect(read_only=True)
        try:
            opp = con.execute(
                f"SELECT {OPP_COLS} FROM opportunities WHERE status='pending' "
                "ORDER BY net_edge DESC LIMIT 1").fetchone()
            legs = _legs_for(opp, _poly_tokens(con, opp[1])) if opp else []
        finally:
            con.close()
        if not opp:
            print("no pending opportunities.")
            return
        _id, ma, mb, question, pa, pb, spread, edge, status = opp
        print("=" * 70)
        print(f"opp #{_id}: {question[:60]}")
        print(f"  polymarket {pa:.3f}  vs  kalshi {pb:.3f}  "
              f"(spread {spread:.3f}, net edge {edge:.3f})")
        for leg in legs:
            print(f"    BUY {leg['platform']:<11} {leg['side']:<3} @ {leg['price']:.3f}")
        ans = input("  execute? [y/n/q]: ").strip().lower()
        if ans == "q":
            return
        con = connect()
        try:
            if ans == "y":
                print("  ->", execute_trade(con, _id, confirmed_by="terminal"))
            else:
                con.execute("UPDATE opportunities SET status='expired', closed_at=? "
                            "WHERE id=? AND status='pending'", [_now(), _id])
                print("  skipped (marked expired)")
        finally:
            con.close()


if __name__ == "__main__":
    terminal_loop()
