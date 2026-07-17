"""Day 4 Block 5: Streamlit dashboard.

    streamlit run dashboard/app.py

Two views:
  * Opportunities (public): verified-equivalent pairs with live spread, sorted
    by size, plus a spread-history chart. Safe to broadcast.
  * Trades (password-gated): positions / P&L from Day 5. Gated behind
    DASHBOARD_PASSWORD so position sizing isn't public next to the feed.
"""
import os
from pathlib import Path

import duckdb
import pandas as pd
import streamlit as st
from dotenv import load_dotenv

load_dotenv()
DB_PATH = Path(__file__).resolve().parent.parent / "db" / "markets.duckdb"


@st.cache_resource
def get_con():
    # read_only so the dashboard never contends with the ingester's writer lock.
    return duckdb.connect(str(DB_PATH), read_only=True)


def verified_with_spreads(con) -> pd.DataFrame:
    return con.execute(
        """
        WITH latest AS (
            SELECT platform, market_id, question, prices,
                   ROW_NUMBER() OVER (PARTITION BY platform, market_id
                                      ORDER BY fetched_at DESC) rn
            FROM markets
        )
        SELECT v.status,
               pa.question                              AS question,
               CAST(pa.prices->>0 AS DOUBLE)            AS polymarket_yes,
               CAST(kb.prices->>0 AS DOUBLE)            AS kalshi_yes,
               ABS(CAST(pa.prices->>0 AS DOUBLE)
                   - CAST(kb.prices->>0 AS DOUBLE))     AS spread
        FROM verified_pairs v
        JOIN latest pa ON pa.market_id=v.market_a AND pa.platform='polymarket' AND pa.rn=1
        JOIN latest kb ON kb.market_id=v.market_b AND kb.platform='kalshi'     AND kb.rn=1
        WHERE v.status IN ('auto_accept', 'confirmed')
        ORDER BY spread DESC
        """
    ).df()


def opportunities_history(con) -> pd.DataFrame:
    return con.execute(
        "SELECT created_at, spread, question FROM opportunities ORDER BY created_at"
    ).df()


def page_opportunities(con):
    st.title("Cross-platform spread opportunities")
    st.caption("Verified-equivalent contracts on Polymarket vs Kalshi. "
               "The matching pipeline — structural filter → embeddings → LLM "
               "verification — is the point; this table is the first thing built on it.")

    df = verified_with_spreads(con)
    if df.empty:
        st.info("No verified pairs yet. Run the matching pipeline first.")
        return
    st.metric("Verified equivalent pairs", len(df))
    st.dataframe(
        df.style.format({"polymarket_yes": "{:.2f}", "kalshi_yes": "{:.2f}",
                         "spread": "{:.3f}"}),
        use_container_width=True, hide_index=True)

    hist = opportunities_history(con)
    if not hist.empty:
        st.subheader("Flagged-opportunity spread history")
        st.line_chart(hist, x="created_at", y="spread")


def page_trades(con):
    st.title("Trades (private)")
    expected = os.environ.get("DASHBOARD_PASSWORD")
    if not expected:
        st.warning("DASHBOARD_PASSWORD is not set; trades page is disabled.")
        return
    pw = st.text_input("Password", type="password")
    if pw != expected:
        st.stop()
    try:
        trades = con.execute(
            "SELECT created_at, platform, market_id, side, price, size, order_id "
            "FROM trades ORDER BY created_at DESC").df()
    except duckdb.CatalogException:
        trades = pd.DataFrame()
    if trades.empty:
        st.info("No trades recorded yet.")
    else:
        st.dataframe(trades, use_container_width=True, hide_index=True)


def main():
    st.set_page_config(page_title="pm-framework", layout="wide")
    page = st.sidebar.radio("View", ["Opportunities (public)", "Trades (private)"])
    con = get_con()
    if page.startswith("Opportunities"):
        page_opportunities(con)
    else:
        page_trades(con)


if __name__ == "__main__":
    main()
