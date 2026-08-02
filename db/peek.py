"""Read-only browser for db/markets.duckdb.

    python db/peek.py                          # tables + row counts
    python db/peek.py markets                  # columns + 10 newest rows
    python db/peek.py "select * from trades"   # any SQL

Always opens read-only, so it never takes the write lock that would block
ingestion or the scanner.
"""

import os
import sys
from pathlib import Path

import duckdb

DB_PATH = Path(__file__).parent / "markets.duckdb"
COL_WIDTH = int(os.environ.get("PEEK_COL_WIDTH", "24"))


def render(cur) -> None:
    headers = [d[0] for d in cur.description]
    rows = cur.fetchall()

    cells = [
        [_clip(v) for v in row]
        for row in rows
    ]
    widths = [
        max(len(_clip(h)), *(len(r[i]) for r in cells)) if cells else len(_clip(h))
        for i, h in enumerate(headers)
    ]

    line = "  ".join(_clip(h).ljust(w) for h, w in zip(headers, widths))
    print(line)
    print("  ".join("-" * w for w in widths))
    for row in cells:
        print("  ".join(v.ljust(w) for v, w in zip(row, widths)))
    print(f"({len(rows)} rows)")


def _clip(value) -> str:
    """One line, hard-capped, so no cell can ever break column alignment."""
    text = "" if value is None else str(value)
    text = text.replace("\n", " ").replace("\r", " ").replace("\t", " ")
    return text if len(text) <= COL_WIDTH else text[: COL_WIDTH - 1] + "…"


def main() -> None:
    con = duckdb.connect(str(DB_PATH), read_only=True)
    arg = " ".join(sys.argv[1:]).strip()

    if not arg:
        names = [r[0] for r in con.execute(
            "select table_name from information_schema.tables order by 1"
        ).fetchall()]
        for name in names:
            count = con.execute(f'select count(*) from "{name}"').fetchone()[0]
            print(f"{name:20s} {count:>10,}")
        return

    if arg.isidentifier():
        render(con.execute(
            'select column_name, column_type, "null", "key" from '
            f'(describe "{arg}")'
        ))
        print()
        render(con.execute(f'select * from "{arg}" order by 1 desc limit 10'))
    else:
        render(con.execute(arg))


if __name__ == "__main__":
    main()
