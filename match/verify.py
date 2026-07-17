"""Day 3: LLM verification of candidate pairs with Gemini structured output.

Structured-output mode guarantees valid JSON against VERIFY_SCHEMA, so there's
no free text to parse. Two markets are auto-accepted only when the model says
they're the same event, at high confidence, with no resolution mismatch.

Model note: the plan's `gemini-2.5-flash` is retired for new API keys (404).
We default to the `gemini-flash-latest` alias so this keeps working across
model retirements; override with GEMINI_MODEL. Free-tier quota is tight and
the flash models are frequently under load, so calls retry on 429/503.
"""
import hashlib
import json
import os
import re
import time

from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors

load_dotenv()

MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")

VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "same_event": {"type": "boolean"},
        "confidence": {"type": "number"},
        "resolution_mismatch": {"type": "string", "nullable": True},
    },
    "required": ["same_event", "confidence", "resolution_mismatch"],
}

_client = None


def get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client()  # reads GEMINI_API_KEY / GOOGLE_API_KEY
    return _client


def rules_hash(text: str) -> str:
    """Stable hash of resolution text, for Day 4's staleness check."""
    return hashlib.sha256((text or "").strip().encode()).hexdigest()


def _suggested_delay(err, fallback: float) -> float:
    """Honor the 'Please retry in Xs' hint the free-tier 429 includes."""
    m = re.search(r"retry in ([\d.]+)s", str(err))
    return float(m.group(1)) + 1.0 if m else fallback


def verify_pair(a: dict, b: dict, max_retries: int = 8) -> dict:
    """Ask the LLM whether two markets are the same event. Retries transient
    429/503 (honoring the server's suggested delay); raises on persistent failure."""
    prompt = (
        f"Market A ({a['platform']}): {a['question']}\nRules A: {a['rules_text']}\n\n"
        f"Market B ({b['platform']}): {b['question']}\nRules B: {b['rules_text']}\n\n"
        "Do these describe the same real-world event, resolving on the same "
        "criteria, source, and deadline? Flag any difference in resolution rules."
    )
    delay = 4.0
    for attempt in range(1, max_retries + 1):
        try:
            resp = get_client().models.generate_content(
                model=MODEL,
                contents=prompt,
                config={
                    "response_mime_type": "application/json",
                    "response_schema": VERIFY_SCHEMA,
                },
            )
            return json.loads(resp.text)
        except (genai_errors.ServerError, genai_errors.ClientError) as e:
            code = getattr(e, "code", None)
            if code in (429, 503) and attempt < max_retries:
                time.sleep(_suggested_delay(e, min(delay, 60)))
                delay = min(delay * 2, 60)
                continue
            raise


def classify(result: dict) -> str:
    """Map an LLM result to a verified_pairs status."""
    if (result.get("same_event")
            and result.get("confidence", 0) > 0.9
            and not result.get("resolution_mismatch")):
        return "auto_accept"
    return "needs_review"
