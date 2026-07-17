"""Day 6 Block 3: export the verified-pairs dataset as a standalone CSV.

    python scripts/export_dataset.py [out.csv]

verified_pairs is a research artifact almost nobody else has published: pairs of
prediction-market contracts on different platforms, confirmed to resolve on the
same real-world event. This joins in the human-readable questions and current
prices. It contains no personal information (no trades, keys, or position data).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "match"))
from data import connect  # noqa: E402

DEFAULT_OUT = Path(__file__).resolve().parent.parent / "verified_pairs.csv"

QUERY = """
COPY (
    WITH latest AS (
        SELECT platform, market_id, question, prices,
               ROW_NUMBER() OVER (PARTITION BY platform, market_id
                                  ORDER BY fetched_at DESC) rn
        FROM markets
    )
    SELECT v.market_a               AS polymarket_id,
           pa.question              AS polymarket_question,
           v.market_b               AS kalshi_id,
           kb.question              AS kalshi_question,
           v.same_event,
           v.confidence,
           v.resolution_mismatch,
           v.status,
           CAST(pa.prices->>0 AS DOUBLE) AS polymarket_yes,
           CAST(kb.prices->>0 AS DOUBLE) AS kalshi_yes,
           v.verified_at
    FROM verified_pairs v
    LEFT JOIN latest pa ON pa.market_id=v.market_a AND pa.platform='polymarket' AND pa.rn=1
    LEFT JOIN latest kb ON kb.market_id=v.market_b AND kb.platform='kalshi'     AND kb.rn=1
    WHERE v.status IN ('auto_accept', 'confirmed')
    ORDER BY v.confidence DESC
) TO '{out}' (HEADER, DELIMITER ',')
"""


def main():
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT
    con = connect(read_only=True)
    con.execute(QUERY.format(out=out))
    n = con.execute(
        "SELECT COUNT(*) FROM verified_pairs WHERE status IN ('auto_accept','confirmed')"
    ).fetchone()[0]
    con.close()
    print(f"exported {n} verified pairs to {out}")


if __name__ == "__main__":
    main()
