"""Daily SMS digest of still-open (untraded) opportunities.

    python alerts/daily_summary.py           # dry-run: print the digest
    python alerts/daily_summary.py --send     # send via Twilio (if configured)

One digest a day instead of a ping per opportunity. Summarizes `pending`
opportunities — the ones the scanner flagged that you haven't actioned yet —
top by net edge, with a link to the dashboard for the full list. Meant to run
on a daily timer (deploy/pm-summary.timer).
"""
import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "match"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from data import connect  # noqa: E402

load_dotenv()

DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "the dashboard")
TOP_N = 5


def build_summary(con) -> str | None:
    rows = con.execute(
        "SELECT id, question, net_edge FROM opportunities "
        "WHERE status='pending' ORDER BY net_edge DESC"
    ).fetchall()
    if not rows:
        return None
    n = len(rows)
    lines = [f"pm-framework: {n} open arb opportunit{'y' if n == 1 else 'ies'}"]
    for oid, q, edge in rows[:TOP_N]:
        lines.append(f"#{oid} {(q or '')[:40]} ({edge * 100:.1f}%)")
    if n > TOP_N:
        lines.append(f"...+{n - TOP_N} more")
    lines.append(f"Reply Y <id> to execute. Details: {DASHBOARD_URL}")
    return "\n".join(lines)


def main(send: bool) -> None:
    con = connect(read_only=True)
    text = build_summary(con)
    con.close()

    if text is None:
        print("No open opportunities today — nothing to send.")
        return
    print(text)

    if not send:
        print("\n(dry-run; re-run with --send to text it)")
        return
    from sms import send_text, twilio_creds
    creds = twilio_creds()
    if not creds:
        print("\n(not sent: Twilio env not fully set)")
        return
    send_text(*creds, text)
    print("\n(sent via SMS)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true",
                    help="actually send the digest via Twilio SMS")
    main(ap.parse_args().send)
