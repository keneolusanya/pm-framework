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

# Confidence gates. auto_accept and auto_reject each require the model to be at
# least this sure of its same_event call; anything less goes to human review.
# The reject gate is deliberately strict — a wrong reject silently drops a real
# match (no downstream catch), so raise VERIFY_REJECT_CONF if you want to be
# even more cautious.
ACCEPT_CONFIDENCE = float(os.environ.get("VERIFY_ACCEPT_CONF", "0.9"))
REJECT_CONFIDENCE = float(os.environ.get("VERIFY_REJECT_CONF", "0.9"))

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
        "criteria, source, and deadline?\n"
        "Treat as DIFFERENT events (same_event=false) any pair that differs in a "
        "numeric strike, threshold, count, level, or bucket of the same underlying "
        "series — e.g. '11 rate cuts' vs '1 rate cut', or 'BTC above $70k' vs "
        "'above $80k'. Also flag differences in year, geographic/jurisdictional "
        "scope, settlement source, or resolution deadline.\n"
        "Set resolution_mismatch to a short description of any such difference, or "
        "null if there is none. Set confidence to how certain you are of the "
        "same_event judgment itself: 1.0 = certain (either way), 0.5 = a coin flip."
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
    """Map an LLM result to a verified_pairs status.

    auto_accept  — confidently the SAME event, no rule mismatch → scanned/traded
    auto_reject  — confidently a DIFFERENT event → excluded, skips human review
    needs_review — the model isn't confident either way → you decide

    auto_reject only fires when the model is confident the events differ, so the
    "11 rate cuts vs 1" pairs get dropped, while genuinely ambiguous ones still
    reach you.
    """
    same = result.get("same_event")
    conf = result.get("confidence", 0) or 0
    mismatch = result.get("resolution_mismatch")
    if same and conf >= ACCEPT_CONFIDENCE and not mismatch:
        return "auto_accept"
    if (not same) and conf >= REJECT_CONFIDENCE:
        return "auto_reject"
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
