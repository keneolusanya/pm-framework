"""Day 3: LLM verification of candidate pairs with Claude (Sonnet 5).

This is the trust-critical stage — deciding whether two markets resolve on the
same real-world event, on the same criteria, source, and deadline. We use
`messages.parse()` with a Pydantic schema so the model returns guaranteed-valid,
already-parsed JSON (no free text to parse, no schema drift).

Model: claude-sonnet-5 by default (override with VERIFY_MODEL). The Anthropic SDK
auto-retries 429 / 5xx with exponential backoff, so callers don't hand-roll it.
Auth resolves from ANTHROPIC_API_KEY in .env (or any credential the SDK finds).
"""
import hashlib
import os

from anthropic import Anthropic, AuthenticationError, PermissionDeniedError
from dotenv import load_dotenv
from pydantic import BaseModel

load_dotenv()

MODEL = os.environ.get("VERIFY_MODEL", "claude-sonnet-5")

_client: Anthropic | None = None


class CredentialError(Exception):
    """The Anthropic key is missing, invalid, revoked, or expired. Distinct from
    transient errors so callers can alert loudly on it rather than silently skip."""


class VerifyResult(BaseModel):
    same_event: bool
    confidence: float
    resolution_mismatch: str | None


def get_client() -> Anthropic:
    global _client
    if _client is None:
        # max_retries lets the SDK ride out rate limits / transient 5xx for us.
        _client = Anthropic(max_retries=5)
    return _client


def rules_hash(text: str) -> str:
    """Stable hash of resolution text, for Day 4's staleness check."""
    return hashlib.sha256((text or "").strip().encode()).hexdigest()


def verify_pair(a: dict, b: dict) -> dict:
    """Ask Claude whether two markets are the same event. Returns a dict with
    same_event (bool), confidence (0-1), resolution_mismatch (str | None)."""
    prompt = (
        f"Market A ({a['platform']}): {a['question']}\nRules A: {a['rules_text']}\n\n"
        f"Market B ({b['platform']}): {b['question']}\nRules B: {b['rules_text']}\n\n"
        "Do these describe the same real-world event, resolving on the same "
        "criteria, source, and deadline? Watch for differences in year, "
        "geographic or jurisdictional scope, the settlement source, or the "
        "resolution deadline. Set resolution_mismatch to a short description of "
        "any such difference, or null if there is none."
    )
    resp = get_client().messages.parse(
        model=MODEL,
        max_tokens=1024,
        thinking={"type": "disabled"},
        messages=[{"role": "user", "content": prompt}],
        output_format=VerifyResult,
    )
    result = resp.parsed_output
    if result is None:  # e.g. a safety refusal — surface it to the batch runner
        raise ValueError(f"no parsed output (stop_reason={resp.stop_reason})")
    return {
        "same_event": result.same_event,
        "confidence": result.confidence,
        "resolution_mismatch": result.resolution_mismatch,
    }


def classify(result: dict) -> str:
    """Map an LLM result to a verified_pairs status."""
    if (result.get("same_event")
            and result.get("confidence", 0) > 0.9
            and not result.get("resolution_mismatch")):
        return "auto_accept"
    return "needs_review"


def health_check() -> None:
    """Confirm the Anthropic key is usable — auth + model access — at zero token
    cost (a metadata GET, nothing generated). Raises CredentialError if the key
    is missing, invalid, revoked, or expired. Other errors (network, wrong model
    id) propagate as themselves so callers can treat them as transient/soft."""
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        raise CredentialError("ANTHROPIC_API_KEY is not set in the environment")
    try:
        get_client().models.retrieve(MODEL)
    except (AuthenticationError, PermissionDeniedError) as e:
        raise CredentialError(f"Anthropic key rejected ({type(e).__name__}): {e}") from e


if __name__ == "__main__":
    # Manual key check: `python match/verify.py`
    try:
        health_check()
        print(f"OK: Anthropic key valid, model '{MODEL}' reachable.")
    except CredentialError as e:
        print(f"FAIL: {e}")
        raise SystemExit(1)
