"""Personal SMS alerts via Twilio.

Personal-only channel: a Twilio trial account can text your own verified number
with no A2P 10DLC registration (capped ~50/day, prefixed "Sent from a Twilio
Trial account"). Credentials come from the environment, never hardcoded:
TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER, TWILIO_TO_NUMBER.

Opportunity texts carry the opportunity id; reply "Y <id>" to confirm that
exact trade (execute/webhook.py).
"""
import os

CRED_KEYS = ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER", "TWILIO_TO_NUMBER")


def twilio_creds() -> tuple | None:
    """(sid, token, from, to) if all four are set, else None."""
    creds = tuple(os.environ.get(k) for k in CRED_KEYS)
    return creds if all(creds) else None


def format_body(opportunity: dict) -> str:
    basis = opportunity.get("basis")
    caveat = " [mid prices: check the book]" if basis == "mid" else ""
    return (
        f"Arb #{opportunity['id']}: {opportunity['question'][:60]}\n"
        f"{opportunity['platform_a']} {opportunity['price_a']:.3f} vs "
        f"{opportunity['platform_b']} {opportunity['price_b']:.3f}, "
        f"net edge {opportunity['net_edge'] * 100:.1f}%{caveat}\n"
        f"Reply Y {opportunity['id']} to execute"
    )


def send_text(account_sid, auth_token, from_number, to_number, text):
    """Send an arbitrary SMS body (opportunities, health alerts, digests)."""
    from twilio.rest import Client

    Client(account_sid, auth_token).messages.create(
        body=text, from_=from_number, to=to_number)


def notify_sms(account_sid, auth_token, from_number, to_number, opportunity):
    send_text(account_sid, auth_token, from_number, to_number, format_body(opportunity))
