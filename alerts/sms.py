"""Day 4 Block 3: personal SMS alerts via Twilio.

Personal-only channel: a Twilio trial account can text your own verified number
with no A2P 10DLC registration (capped ~50/day, prefixed "Sent from a Twilio
Trial account"). Credentials come from the environment, never hardcoded:
TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER, TWILIO_TO_NUMBER.
"""


def format_body(opportunity: dict) -> str:
    return (
        f"Arb: {opportunity['question'][:60]}\n"
        f"{opportunity['platform_a']} {opportunity['price_a']:.2f} vs "
        f"{opportunity['platform_b']} {opportunity['price_b']:.2f} "
        f"(spread {opportunity['spread']:.3f})"
    )


def notify_sms(account_sid, auth_token, from_number, to_number, opportunity):
    from twilio.rest import Client

    client = Client(account_sid, auth_token)
    client.messages.create(
        body=format_body(opportunity),
        from_=from_number,
        to=to_number,
    )
