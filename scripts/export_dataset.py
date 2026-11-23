"""Export the verified-pairs dataset as a standalone CSV.

    python scripts/export_dataset.py [out.csv]

verified_pairs is a research artifact few others have published: pairs of
prediction-market contracts on different platforms, confirmed to resolve on the
same real-world event. This joins in the human-readable questions and latest
prices. It contains no personal information (no trades, keys, or positions).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "match"))
from data import connect, latest_snapshot_sql  # noqa: E402

DEFAULT_OUT = Path(__file__).resolve().parent.parent / "verified_pairs.csv"

QUERY = """
COPY (
    WITH {latest},
    info AS (SELECT platform, market_id, question FROM market_info)
    SELECT v.market_a               AS polymarket_id,
           ia.question              AS polymarket_question,
           v.market_b               AS kalshi_id,
           ib.question              AS kalshi_question,
           v.same_event,
           v.confidence,
           v.resolution_mismatch,
           v.status,
           l.is_match               AS hand_label,
           CAST(pa.prices->>0 AS DOUBLE) AS polymarket_yes,
           CAST(kb.prices->>0 AS DOUBLE) AS kalshi_yes,
           v.verified_at
    FROM verified_pairs v
    LEFT JOIN info ia ON ia.market_id=v.market_a AND ia.platform='polymarket'
    LEFT JOIN info ib ON ib.market_id=v.market_b AND ib.platform='kalshi'
    LEFT JOIN latest pa ON pa.market_id=v.market_a AND pa.platform='polymarket'
    LEFT JOIN latest kb ON kb.market_id=v.market_b AND kb.platform='kalshi'
    LEFT JOIN precision_labels l ON l.market_a=v.market_a AND l.market_b=v.market_b
    WHERE v.status IN ('auto_accept', 'confirmed')
    ORDER BY v.confidence DESC
) TO '{out}' (HEADER, DELIMITER ',')
"""


def main():
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT
    con = connect(read_only=True)
    try:
        con.execute(QUERY.format(latest=latest_snapshot_sql(), out=str(out).replace("'", "''")))
        n = con.execute("SELECT COUNT(*) FROM verified_pairs "
                        "WHERE status IN ('auto_accept','confirmed')").fetchone()[0]
    finally:
        con.close()
    print(f"exported {n} verified pairs to {out}")


if __name__ == "__main__":
    main()
