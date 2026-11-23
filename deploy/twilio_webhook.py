"""Point your Twilio number's incoming-SMS webhook at this server.

    python deploy/twilio_webhook.py      # uses TWILIO_* and WEBHOOK_PUBLIC_URL from .env

Equivalent to Twilio Console -> Phone Numbers -> your number -> Messaging ->
"A message comes in" -> Webhook, HTTP POST. bootstrap.sh runs it on deploy.
"""
import os
import sys

from dotenv import load_dotenv

load_dotenv()


def main() -> int:
    sid, token = os.environ.get("TWILIO_ACCOUNT_SID"), os.environ.get("TWILIO_AUTH_TOKEN")
    number, url = os.environ.get("TWILIO_FROM_NUMBER"), os.environ.get("WEBHOOK_PUBLIC_URL")
    missing = [k for k, v in (("TWILIO_ACCOUNT_SID", sid), ("TWILIO_AUTH_TOKEN", token),
                              ("TWILIO_FROM_NUMBER", number), ("WEBHOOK_PUBLIC_URL", url)) if not v]
    if missing:
        print(f"Twilio webhook not set: missing {', '.join(missing)} in .env")
        return 1

    from twilio.rest import Client
    client = Client(sid, token)
    matches = client.incoming_phone_numbers.list(phone_number=number, limit=1)
    if not matches:
        print(f"Twilio webhook not set: {number} is not a number on this Twilio account")
        return 1
    client.incoming_phone_numbers(matches[0].sid).update(sms_url=url, sms_method="POST")
    print(f"Twilio: {number} now posts incoming SMS to {url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
