"""Day 5 Block 4: manual-confirm trade execution + the terminal loop.

Nothing here fires automatically. A trade executes only after a human confirms
it — `y` at the terminal (this file) or `Y` by SMS reply (webhook.py). Both
paths call the same execute_trade(), which enforces the concurrency guard:

    the opportunity's status flips pending -> actioned atomically BEFORE any
    order is sent; whichever channel confirms first wins, the other is a no-op.

execute_trade dry-runs by default. It places real orders only when EXECUTE_LIVE=1
and the platform credentials are present — and even then only via a confirmed
path. This human-in-the-loop design is deliberate; do not remove it.
"""
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "match"))
from data import connect  # noqa: E402
from risk import RiskError, check_trade  # noqa: E402

LIVE = os.environ.get("EXECUTE_LIVE", "").lower() in ("1", "true", "yes")
USE_DEMO = os.environ.get("KALSHI_DEMO", "1").lower() in ("1", "true", "yes")


def claim_opportunity(con, opp_id: int) -> bool:
    """Atomically flip pending -> actioned. True only if THIS call won the claim."""
    won = con.execute(
        "UPDATE opportunities SET status='actioned' "
        "WHERE id=? AND status='pending' RETURNING id",
        [opp_id],
    ).fetchone()
    return won is not None


OPP_COLS = "id, market_a, market_b, question, price_a, price_b, spread, net_edge, status"


def _legs_for(opp) -> list[dict]:
    """Two arb legs: buy the underpriced YES, buy the other side's NO."""
    _id, market_a, market_b, question, price_a, price_b, spread, edge, status = opp
    if price_a <= price_b:  # polymarket YES cheaper
        return [
            {"platform": "polymarket", "market_id": market_a, "side": "YES", "price": price_a},
            {"platform": "kalshi", "market_id": market_b, "side": "NO", "price": round(1 - price_b, 4)},
        ]
    return [
        {"platform": "kalshi", "market_id": market_b, "side": "YES", "price": price_b},
        {"platform": "polymarket", "market_id": market_a, "side": "NO", "price": round(1 - price_a, 4)},
    ]


def _place(leg, count) -> str:
    """Dispatch a single leg. Returns an order id (or 'DRY_RUN')."""
    if not LIVE:
        return "DRY_RUN"
    if leg["platform"] == "kalshi":
        from kalshi_order import place_kalshi_order
        resp = place_kalshi_order(leg["market_id"], leg["side"].lower(), count,
                                  int(round(leg["price"] * 100)), demo=USE_DEMO)
        return str(resp.get("order", {}).get("order_id", "?"))
    from polymarket_order import place_polymarket_order
    # token_id resolution for the YES/NO leg is required here; see NOTE below.
    resp = place_polymarket_order(leg["market_id"], "BUY", count, leg["price"])
    return str(resp.get("orderID", "?"))


def execute_trade(con, opp_id: int, confirmed_by: str, count: int = 5) -> str:
    """Execute a human-confirmed opportunity. Idempotent via the claim guard."""
    if not claim_opportunity(con, opp_id):
        return "already actioned (another channel got there first)"

    opp = con.execute(f"SELECT {OPP_COLS} FROM opportunities WHERE id=?", [opp_id]).fetchone()
    legs = _legs_for(opp)
    stake = sum(leg["price"] * count for leg in legs)
    try:
        check_trade(con, stake)
    except RiskError as e:
        # Undo the claim so the opportunity can be reconsidered.
        con.execute("UPDATE opportunities SET status='pending' WHERE id=?", [opp_id])
        return f"blocked by risk rails: {e}"

    now = datetime.now(timezone.utc)
    for leg in legs:
        order_id = _place(leg, count)
        con.execute(
            "INSERT INTO trades (opportunity_id, platform, market_id, side, price, size, order_id, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            [opp_id, leg["platform"], leg["market_id"], leg["side"],
             leg["price"], count, order_id, now])
    mode = "LIVE" if LIVE else "DRY_RUN"
    return f"executed ({mode}, confirmed_by={confirmed_by}): 2 legs logged"


def terminal_loop(con):
    """Poll pending opportunities and confirm them one at a time. Ctrl-C to stop."""
    print("Manual trade loop. For each opportunity type y to execute, n to skip.\n")
    while True:
        opp = con.execute(
            f"SELECT {OPP_COLS} FROM opportunities WHERE status='pending' "
            "ORDER BY net_edge DESC LIMIT 1"
        ).fetchone()
        if not opp:
            print("no pending opportunities.")
            return
        _id, ma, mb, question, pa, pb, spread, edge, status = opp
        print("=" * 70)
        print(f"opp #{_id}: {question[:60]}")
        print(f"  polymarket YES {pa:.2f}  vs  kalshi YES {pb:.2f}  "
              f"(spread {spread:.3f}, net edge {edge:.3f})")
        for leg in _legs_for(opp):
            print(f"    BUY {leg['platform']:<11} {leg['side']:<3} @ {leg['price']:.2f}")
        ans = input("  execute? [y/n/q]: ").strip().lower()
        if ans == "q":
            return
        if ans == "y":
            print("  ->", execute_trade(con, _id, confirmed_by="terminal"))
        else:
            con.execute("UPDATE opportunities SET status='expired' WHERE id=?", [_id])
            print("  skipped (marked expired)")


if __name__ == "__main__":
    con = connect()
    try:
        terminal_loop(con)
    finally:
        con.close()
