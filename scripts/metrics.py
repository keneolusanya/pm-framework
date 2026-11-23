"""Measured pipeline metrics — every headline number, computed from the database.

    python scripts/metrics.py            # human-readable report
    python scripts/metrics.py --json     # same numbers as JSON

Definitions (so each number can be defended):
  * rows/day           market price rows ingested per UTC day (ingest_runs),
                       i.e. markets per cycle x cycles per day
  * funnel             latest build_candidates run: cross product -> structural
                       filter -> embedding candidates -> accepted matches
  * precision          share of a random hand-labeled sample of auto_accept pairs
                       that are true matches (match/label_sample.py), with a
                       95% Wilson interval; human-confirmed pairs count as correct
  * signals/week       opportunities logged per week since the first scan
  * net edge           per-$1-payoff edge after both taker fees + safety margin
  * modeled P&L        sum over opportunities of contracts x net edge, where
                       contracts = MODEL_STAKE_USD / cost per contract pair. Assumes
                       each opportunity fills once at its logged prices — a model,
                       not realized trading
  * safeguards         share of raw signals (positive net edge before any check)
                       that a safeguard blocked, summed over scans
"""
import argparse
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "match"))
from data import connect  # noqa: E402

MODEL_STAKE_USD = float(os.environ.get("MODEL_STAKE_USD")
                        or os.environ.get("RISK_MAX_POSITION_USD") or "25")


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def _one(con, sql, params=()):
    return con.execute(sql, list(params)).fetchone()


def collect(con) -> dict:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    m: dict = {"generated_at": now.isoformat(timespec="seconds") + "Z"}

    # --- ingestion ---
    runs, first, last = _one(con, "SELECT count(*), min(fetched_at), max(fetched_at) FROM ingest_runs")
    daily = con.execute("""
        SELECT CAST(fetched_at AS DATE) AS day, count(*) AS cycles,
               sum(polymarket_rows + kalshi_rows) AS rows
        FROM ingest_runs GROUP BY 1 ORDER BY 1 DESC LIMIT 14""").fetchall()
    latest = _one(con, "SELECT polymarket_rows, kalshi_rows, fetch_seconds FROM ingest_runs "
                       "ORDER BY fetched_at DESC LIMIT 1")
    full_days = [d for d in daily if d[1] >= 250]  # a day with ~all 288 five-minute cycles
    m["ingestion"] = {
        "cycles_total": runs,
        "first_cycle": str(first) if first else None,
        "last_cycle": str(last) if last else None,
        "minutes_since_last_cycle": round((now - last).total_seconds() / 60, 1) if last else None,
        "markets_per_cycle_latest": (latest[0] + latest[1]) if latest else None,
        "fetch_seconds_latest": latest[2] if latest else None,
        "daily": [{"day": str(d), "cycles": c, "rows": r} for d, c, r in daily],
        "rows_per_full_day_avg": (round(sum(r for _, _, r in full_days) / len(full_days))
                                  if full_days else None),
        "full_days_observed": len(full_days),
    }

    # --- matching funnel ---
    pr = _one(con, "SELECT run_at, cross_product, structural_pairs, candidates, poly_markets, "
                   "kalshi_markets FROM pipeline_runs ORDER BY run_at DESC LIMIT 1")
    by_status = dict(con.execute("SELECT status, count(*) FROM verified_pairs GROUP BY 1").fetchall())
    accepted = by_status.get("auto_accept", 0) + by_status.get("confirmed", 0)
    m["matching"] = {
        "latest_run": str(pr[0]) if pr else None,
        "markets": (pr[4] + pr[5]) if pr else None,
        "cross_product": pr[1] if pr else None,
        "structural_pairs": pr[2] if pr else None,
        "embedding_candidates": pr[3] if pr else None,
        "verified_by_status": by_status,
        "accepted_matches": accepted,
    }

    # --- precision ---
    lab = con.execute("SELECT status_at_label, count(*), count(*) FILTER (WHERE is_match) "
                      "FROM precision_labels GROUP BY 1").fetchall()
    lab = {s: (n, k) for s, n, k in lab}
    n_auto, k_auto = lab.get("auto_accept", (0, 0))
    n_conf, k_conf = lab.get("confirmed", (0, 0))
    p_auto = k_auto / n_auto if n_auto else None
    lo, hi = wilson(k_auto, n_auto)
    p_conf = (k_conf / n_conf) if n_conf else 1.0  # confirmed = a human already said yes
    overall = None
    if p_auto is not None and accepted:
        overall = (by_status.get("auto_accept", 0) * p_auto
                   + by_status.get("confirmed", 0) * p_conf) / accepted
    m["precision"] = {
        "auto_accept_labeled": n_auto,
        "auto_accept_correct": k_auto,
        "auto_accept_precision": p_auto,
        "auto_accept_ci95": [lo, hi] if n_auto else None,
        "confirmed_labeled": n_conf,
        "overall_estimate": overall,
    }

    # --- signals + modeled P&L ---
    # Opportunities logged before the fee/order-book model (quote_basis IS NULL)
    # used mid prices and a 0% Polymarket fee; they're reported separately and
    # excluded from every figure below.
    opp = _one(con, """
        SELECT count(*), min(created_at), avg(net_edge), median(net_edge),
               sum(? / (1 - spread) * net_edge),
               count(*) FILTER (WHERE quote_basis = 'book'),
               avg(net_edge) FILTER (WHERE quote_basis = 'book'),
               sum(? / (1 - spread) * net_edge) FILTER (WHERE quote_basis = 'book'),
               median(epoch(closed_at) - epoch(created_at)) FILTER (WHERE status = 'expired')
        FROM opportunities
        WHERE quote_basis IS NOT NULL AND net_edge > 0 AND spread < 1""",
        [MODEL_STAKE_USD, MODEL_STAKE_USD])
    n_opp, first_opp, avg_edge, med_edge, pnl, n_book, avg_book, pnl_book, life_s = opp
    legacy = _one(con, "SELECT count(*) FROM opportunities WHERE quote_basis IS NULL")[0]
    weeks = max((now - first_opp).total_seconds() / (7 * 86400), 1.0) if first_opp else None
    weekly = con.execute("""
        SELECT date_trunc('week', created_at) AS wk, count(*), avg(net_edge)
        FROM opportunities WHERE quote_basis IS NOT NULL
        GROUP BY 1 ORDER BY 1 DESC LIMIT 8""").fetchall()
    m["signals"] = {
        "legacy_excluded": legacy,
        "opportunities_total": n_opp,
        "first_opportunity": str(first_opp) if first_opp else None,
        "per_week_avg": round(n_opp / weeks, 1) if weeks else None,
        "net_edge_avg": avg_edge,
        "net_edge_median": med_edge,
        "book_priced": n_book,
        "book_net_edge_avg": avg_book,
        "median_lifetime_minutes": round(life_s / 60, 1) if life_s is not None else None,
        "weekly": [{"week": str(w.date()), "count": c, "avg_net_edge": e} for w, c, e in weekly],
        "modeled_stake_usd": MODEL_STAKE_USD,
        "modeled_pnl_usd": round(pnl or 0, 2),
        "modeled_pnl_book_only_usd": round(pnl_book or 0, 2),
    }

    # --- safeguards ---
    sg = _one(con, """
        SELECT count(*), sum(raw_signals), sum(blocked_stale), sum(blocked_illiquid),
               sum(blocked_implausible), sum(audit_checked), sum(audit_flagged)
        FROM scan_runs""")
    scans, raw, stale, illiq, implaus, a_chk, a_flag = [x or 0 for x in sg]
    blocked = stale + illiq + implaus
    events = dict(con.execute("SELECT kind, count(*) FROM safeguard_events GROUP BY 1").fetchall())
    m["safeguards"] = {
        "scans": scans,
        "raw_signals": raw,
        "blocked_stale": stale,
        "blocked_illiquid": illiq,
        "blocked_implausible": implaus,
        "blocked_share": blocked / raw if raw else None,
        "pairs_pulled_to_review": events,
        "audit_checked": a_chk,
        "audit_flagged": a_flag,
        "audit_fail_rate": a_flag / a_chk if a_chk else None,
    }

    # --- execution ---
    m["execution"] = {"trade_legs_by_status": dict(con.execute(
        "SELECT COALESCE(status, 'unknown'), count(*) FROM trades GROUP BY 1").fetchall())}
    return m


def _pct(x, digits=1):
    return "n/a" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x * 100:.{digits}f}%"


def _n(x):
    return "n/a" if x is None else f"{x:,}"


def render(m: dict) -> str:
    i, mt, p, s, g = m["ingestion"], m["matching"], m["precision"], m["signals"], m["safeguards"]
    out = [f"pm-framework metrics  (generated {m['generated_at']})", ""]
    out += ["INGESTION",
            f"  cycles recorded        {_n(i['cycles_total'])}  ({i['first_cycle']} -> {i['last_cycle']})",
            f"  last cycle             {i['minutes_since_last_cycle']} min ago",
            f"  markets per cycle      {_n(i['markets_per_cycle_latest'])}  "
            f"(fetch {i['fetch_seconds_latest']}s)",
            f"  rows/day (full days)   {_n(i['rows_per_full_day_avg'])}  "
            f"over {i['full_days_observed']} full day(s)"]
    for d in i["daily"][:7]:
        out.append(f"      {d['day']}  {d['cycles']:>4} cycles  {d['rows']:>12,} rows")
    out += ["", "MATCHING FUNNEL (latest build)",
            f"  markets                {_n(mt['markets'])}",
            f"  cross product          {_n(mt['cross_product'])}",
            f"  structural filter      {_n(mt['structural_pairs'])}",
            f"  embedding candidates   {_n(mt['embedding_candidates'])}",
            f"  accepted matches       {_n(mt['accepted_matches'])}   {mt['verified_by_status']}"]
    ci = p["auto_accept_ci95"]
    out += ["", "PRECISION (hand-labeled random sample)",
            f"  auto_accept            {p['auto_accept_correct']}/{p['auto_accept_labeled']} correct = "
            f"{_pct(p['auto_accept_precision'])}"
            + (f"  (95% CI {_pct(ci[0])}-{_pct(ci[1])})" if ci else "  — run match/label_sample.py"),
            f"  overall (weighted)     {_pct(p['overall_estimate'])}"]
    out += ["", "SIGNALS",
            f"  opportunities          {_n(s['opportunities_total'])}  "
            f"({s['per_week_avg']} / week since {s['first_opportunity']}; "
            f"{s['legacy_excluded']} pre-fee-model excluded)",
            f"  net edge avg / median  {_pct(s['net_edge_avg'], 2)} / {_pct(s['net_edge_median'], 2)}",
            f"  book-priced            {_n(s['book_priced'])}  (avg {_pct(s['book_net_edge_avg'], 2)})",
            f"  median lifetime        {s['median_lifetime_minutes']} min",
            f"  modeled P&L            ${s['modeled_pnl_usd']:,.2f}  "
            f"(book-priced only ${s['modeled_pnl_book_only_usd']:,.2f}; "
            f"${s['modeled_stake_usd']:.0f}/opportunity)"]
    for w in s["weekly"][:4]:
        out.append(f"      week of {w['week']}  {w['count']:>4} signals  avg {_pct(w['avg_net_edge'], 2)}")
    out += ["", "SAFEGUARDS",
            f"  scans                  {_n(g['scans'])}",
            f"  raw signals            {_n(g['raw_signals'])}",
            f"  blocked                stale {g['blocked_stale']}, illiquid {g['blocked_illiquid']}, "
            f"implausible {g['blocked_implausible']}  = {_pct(g['blocked_share'])} of raw",
            f"  pulled to review       {g['pairs_pulled_to_review']}",
            f"  audit                  {g['audit_flagged']}/{g['audit_checked']} failed "
            f"re-verification ({_pct(g['audit_fail_rate'])})"]
    out += ["", "EXECUTION",
            f"  trade legs             {m['execution']['trade_legs_by_status'] or 'none'}"]
    return "\n".join(out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    con = connect(read_only=True)
    try:
        metrics = collect(con)
    finally:
        con.close()
    print(json.dumps(metrics, indent=2, default=str) if args.json else render(metrics))
