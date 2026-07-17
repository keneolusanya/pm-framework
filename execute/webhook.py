"""Day 5 Block 5: SMS-confirm webhook (the way you'll actually use it).

A Twilio "A Message Comes In" webhook. When you reply Y to an alert, this
validates the request and executes the same execute_trade() the terminal loop
uses — so confirming from your phone and from the terminal share one guarded
path and can't double-fire.

    uvicorn execute.webhook:app --port 8000
    # expose with: ngrok http 8000  (Day 5) -> permanent /sms-reply (Day 6)

Three checks before anything executes:
  1. Twilio signature validation (proves the request is really from Twilio),
  2. sender check (the From number must be your own TWILIO_TO_NUMBER),
  3. the opportunity must still be status='pending' (the concurrency guard).
"""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response

sys.path.insert(0, str(Path(__file__).resolve().parent))
from data import connect  # noqa: E402
from trade import execute_trade  # noqa: E402

load_dotenv()
app = FastAPI()


def _twiml(message: str) -> Response:
    body = (f'<?xml version="1.0" encoding="UTF-8"?><Response>'
            f'<Message>{message}</Message></Response>')
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
    if not _valid_signature(request, form, str(request.url)):
        return _twiml("Rejected.")

    # 2. Sender: only your own phone may confirm trades.
    owner = os.environ.get("TWILIO_TO_NUMBER")
    if not owner or form.get("From") != owner:
        return _twiml("Unauthorized sender.")

    body = (form.get("Body") or "").strip().upper()
    if not body.startswith("Y"):
        return _twiml("Ignored (reply Y to confirm).")

    con = connect()
    try:
        # An explicit id ("Y 42") or the newest pending opportunity.
        parts = body.split()
        opp = None
        if len(parts) > 1 and parts[1].isdigit():
            opp = con.execute(
                "SELECT id FROM opportunities WHERE id=? AND status='pending'",
                [int(parts[1])]).fetchone()
        else:
            opp = con.execute(
                "SELECT id FROM opportunities WHERE status='pending' "
                "ORDER BY created_at DESC LIMIT 1").fetchone()
        if not opp:
            return _twiml("No pending opportunity to confirm.")
        result = execute_trade(con, opp[0], confirmed_by="sms")
        return _twiml(result[:150])
    finally:
        con.close()


@app.get("/health")
def health():
    return {"ok": True}
