"""SMS-confirm webhook: reply to an alert from your phone to execute it.

A Twilio "A Message Comes In" webhook. When you reply "Y <id>" to an alert,
this validates the request and runs the same execute_trade() the terminal loop
uses — so confirming from your phone and from the terminal share one guarded
path and can't double-fire.

    uvicorn execute.webhook:app --host 127.0.0.1 --port 8000
    # local testing: expose with `ngrok http 8000`; in production Caddy proxies /sms-reply

Checks before anything executes:
  1. Twilio signature validation (proves the request is really from Twilio),
  2. sender check (the From number must be your own TWILIO_TO_NUMBER),
  3. an explicit opportunity id, unless exactly one opportunity is pending,
  4. inside execute_trade: fresh prices, edge still open, the pending -> actioned
     claim (the concurrency guard), and the risk rails.
"""
import os
import sys
from html import escape
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "execute"))
sys.path.insert(0, str(ROOT / "match"))
from data import connect  # noqa: E402
from trade import execute_trade  # noqa: E402

load_dotenv()
app = FastAPI()

# Twilio gives up on a webhook after ~15s; wait at most this long for the DB lock.
DB_WAIT_SEC = 8.0
# Behind a TLS-terminating proxy the URL uvicorn sees differs from the one Twilio
# signed; set this to the public URL (e.g. https://your.domain/sms-reply).
PUBLIC_URL = os.environ.get("WEBHOOK_PUBLIC_URL")


def _twiml(message: str) -> Response:
    body = (f'<?xml version="1.0" encoding="UTF-8"?><Response>'
            f'<Message>{escape(message)}</Message></Response>')
    return Response(content=body, media_type="application/xml")


def _valid_signature(request: Request, form: dict, url: str) -> bool:
    from twilio.request_validator import RequestValidator

    token = os.environ.get("TWILIO_AUTH_TOKEN")
    if not token:
        return False
    signature = request.headers.get("X-Twilio-Signature", "")
    return RequestValidator(token).validate(url, form, signature)


@app.post("/sms-reply")
async def sms_reply(request: Request):
    form = dict(await request.form())

    # 1. Authenticity: the request must be signed by Twilio.
    if not _valid_signature(request, form, PUBLIC_URL or str(request.url)):
        return _twiml("Rejected.")

    # 2. Sender: only your own phone may confirm trades.
    owner = os.environ.get("TWILIO_TO_NUMBER")
    if not owner or form.get("From") != owner:
        return _twiml("Unauthorized sender.")

    parts = (form.get("Body") or "").strip().upper().split()
    if not parts or not parts[0].startswith("Y"):
        return _twiml("Ignored (reply Y <id> to confirm).")

    try:
        con = connect(wait=DB_WAIT_SEC)
    except Exception:
        return _twiml("Database busy — reply again in a minute.")
    try:
        # 3. Which opportunity: an explicit id, or the only pending one.
        if len(parts) > 1 and parts[1].lstrip("#").isdigit():
            opp_id = int(parts[1].lstrip("#"))
        else:
            pending = con.execute(
                "SELECT id FROM opportunities WHERE status='pending' LIMIT 2").fetchall()
            if not pending:
                return _twiml("No pending opportunity to confirm.")
            if len(pending) > 1:
                return _twiml("Several opportunities are open — reply Y <id>.")
            opp_id = pending[0][0]
        return _twiml(execute_trade(con, opp_id, confirmed_by="sms")[:300])
    finally:
        con.close()


@app.get("/health")
def health():
    return {"ok": True}
