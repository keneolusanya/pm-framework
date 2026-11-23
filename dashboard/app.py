"""Streamlit dashboard.

    streamlit run dashboard/app.py

Three views:
  * Opportunities (public): verified-equivalent pairs with live spread, open
    opportunities, and flagged-spread history. Safe to broadcast.
  * Metrics (public): the measured pipeline numbers from scripts/metrics.py.
  * Trades (password-gated): executed / dry-run / failed trade legs. Gated
    behind DASHBOARD_PASSWORD so position sizing isn't public next to the feed.

The database is opened read-only for each render and closed straight after:
DuckDB allows one writer *or* many readers per file, so a long-lived dashboard
connection would lock ingestion out.
"""
import os
import sys
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "match"))
sys.path.insert(0, str(ROOT / "scripts"))
from data import connect, latest_snapshot_sql  # noqa: E402

load_dotenv()


@st.cache_data(ttl=60)
def query(sql: str) -> pd.DataFrame:
    con = connect(read_only=True, wait=20)
    try:
        return con.execute(sql).df()
    finally:
        con.close()


@st.cache_data(ttl=60)
def metrics() -> dict:
    from metrics import collect
    con = connect(read_only=True, wait=20)
    try:
        return collect(con)
    finally:
        con.close()


def verified_with_spreads() -> pd.DataFrame:
    return query(f"""
        WITH {latest_snapshot_sql()}
        SELECT v.status,
               pa.question                              AS question,
               CAST(pa.prices->>0 AS DOUBLE)            AS polymarket_yes,
               CAST(kb.prices->>0 AS DOUBLE)            AS kalshi_yes,
               ABS(CAST(pa.prices->>0 AS DOUBLE)
                   - CAST(kb.prices->>0 AS DOUBLE))     AS mid_spread
        FROM verified_pairs v
        JOIN latest pa ON pa.market_id=v.market_a AND pa.platform='polymarket'
        JOIN latest kb ON kb.market_id=v.market_b AND kb.platform='kalshi'
        WHERE v.status IN ('auto_accept', 'confirmed')
        ORDER BY mid_spread DESC
    """)


def page_opportunities():
    st.title("Cross-platform spread opportunities")
    st.caption("Verified-equivalent contracts on Polymarket vs Kalshi. "
               "The matching pipeline — structural filter → embeddings → LLM "
               "verification — is the point; this table is the first thing built on it.")

    df = verified_with_spreads()
    if df.empty:
        st.info("No verified pairs yet. Run the matching pipeline first.")
        return
    opps = query("SELECT id, question, net_edge, spread, price_a AS polymarket, "
                 "price_b AS kalshi, quote_basis, created_at "
                 "FROM opportunities WHERE status='pending' ORDER BY net_edge DESC")
    c1, c2 = st.columns(2)
    c1.metric("Verified equivalent pairs", len(df))
    c2.metric("Open opportunities", len(opps))

    st.subheader("Open opportunities (flagged, not yet actioned)")
    st.caption("Prices are what each leg would pay: the ask where you buy YES, the "
               "bid where you buy NO. Net edge is after both platforms' taker fees and "
               "a safety margin.")
    if opps.empty:
        st.caption("None currently clear fees + margin — the equivalent pairs "
                   "are efficiently priced right now.")
    else:
        st.dataframe(
            opps.style.format({"net_edge": "{:.3f}", "spread": "{:.3f}",
                               "polymarket": "{:.3f}", "kalshi": "{:.3f}"}),
            width="stretch", hide_index=True)

    st.subheader("All verified pairs — live mid-price spread")
    st.dataframe(
        df.style.format({"polymarket_yes": "{:.3f}", "kalshi_yes": "{:.3f}",
                         "mid_spread": "{:.3f}"}),
        width="stretch", hide_index=True)

    hist = query("SELECT created_at, net_edge FROM opportunities ORDER BY created_at")
    if not hist.empty:
        st.subheader("Flagged-opportunity net edge over time")
        st.scatter_chart(hist, x="created_at", y="net_edge")


def page_metrics():
    st.title("Pipeline metrics")
    st.caption("Computed live from the database; definitions in scripts/metrics.py.")
    m = metrics()
    mt, p, s, g, i = m["matching"], m["precision"], m["signals"], m["safeguards"], m["ingestion"]

    def pct(x, d=1):
        return "—" if x is None else f"{x * 100:.{d}f}%"

    st.subheader("Matching funnel")
    funnel = pd.DataFrame({
        "stage": ["Cross product", "Structural filter", "Embedding candidates", "Accepted matches"],
        "pairs": [mt["cross_product"], mt["structural_pairs"], mt["embedding_candidates"],
                  mt["accepted_matches"]],
    })
    st.dataframe(funnel, width="stretch", hide_index=True)
    c1, c2 = st.columns(2)
    c1.metric("Precision (auto-accepted, labeled sample)", pct(p["auto_accept_precision"]),
              help=f"{p['auto_accept_correct']}/{p['auto_accept_labeled']} labeled pairs correct")
    c2.metric("Precision (all accepted, weighted)", pct(p["overall_estimate"]))

    st.subheader("Signals")
    c1, c2, c3 = st.columns(3)
    c1.metric("Signals / week", s["per_week_avg"] if s["per_week_avg"] is not None else "—")
    c2.metric("Median net edge", pct(s["net_edge_median"], 2))
    c3.metric("Modeled P&L", f"${s['modeled_pnl_usd']:,.0f}",
              help=f"${s['modeled_stake_usd']:.0f} per opportunity, one fill at logged prices")

    st.subheader("Safeguards")
    c1, c2 = st.columns(2)
    c1.metric("Raw signals blocked", pct(g["blocked_share"]),
              help=f"stale {g['blocked_stale']}, illiquid {g['blocked_illiquid']}, "
                   f"implausible {g['blocked_implausible']} of {g['raw_signals']}")
    c2.metric("Audit re-verification failures", pct(g["audit_fail_rate"]),
              help=f"{g['audit_flagged']}/{g['audit_checked']}")

    st.subheader("Ingestion")
    c1, c2 = st.columns(2)
    c1.metric("Markets per cycle", f"{i['markets_per_cycle_latest'] or 0:,}")
    c2.metric("Rows / full day", f"{i['rows_per_full_day_avg']:,}" if i["rows_per_full_day_avg"] else "—")
    if i["daily"]:
        st.bar_chart(pd.DataFrame(i["daily"]), x="day", y="rows")


def page_trades():
    st.title("Trades (private)")
    expected = os.environ.get("DASHBOARD_PASSWORD")
    if not expected:
        st.warning("DASHBOARD_PASSWORD is not set; trades page is disabled.")
        return
    pw = st.text_input("Password", type="password")
    if pw != expected:
        st.stop()
    trades = query("SELECT created_at, opportunity_id, platform, market_id, side, price, size, "
                   "status, order_id, error FROM trades ORDER BY created_at DESC")
    if trades.empty:
        st.info("No trades recorded yet.")
    else:
        st.dataframe(trades, width="stretch", hide_index=True)


def main():
    st.set_page_config(page_title="pm-framework", layout="wide")
    page = st.sidebar.radio("View", ["Opportunities (public)", "Metrics (public)",
                                     "Trades (private)"])
    if page.startswith("Opportunities"):
        page_opportunities()
    elif page.startswith("Metrics"):
        page_metrics()
    else:
        page_trades()


if __name__ == "__main__":
    main()
