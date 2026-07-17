# Prediction Markets Research Framework — 6-Day Build Plan

~20 hours total. Each day is scoped to be doable in one sitting, with a concrete
"done when" checkpoint so you know if you're on track before moving on.

---

## Day 0 — Before you start (do this the night before, ~30 min)

- [ ] Create a Polymarket account (no KYC needed to read data; you'll need a
      Polygon wallet + USDC before Day 5 execution)
- [ ] Create a Kalshi account and start KYC verification now — this can take
      a day or two to clear, and you need it done before Day 5
- [ ] In Kalshi's Profile Settings → API Keys, click "Create New API Key."
      Kalshi generates the pair for you and shows the private key exactly
      once — download the file immediately and store it somewhere durable
      (not just Downloads). Note the Key ID it gives you alongside it.
- [ ] `mkdir pm-framework && cd pm-framework && git init`
- [ ] Create `.gitignore` *before* your first commit — not after, since
      anything committed once stays in history even if you delete it later:
```
# Secrets — never committed
.env
*.pem
*.key
kalshi_private*.txt

# Python
__pycache__/
*.py[cod]
.venv/
venv/
build/
dist/
*.egg-info/

# Local data
*.duckdb
*.db

# macOS
.DS_Store

# Editor
.vscode/
.idea/

# Tunnels/logs
ngrok.log
*.log

# Uncomment if you add these later
# .pytest_cache/
# .coverage
# .ipynb_checkpoints/
```
- [ ] `python -m venv .venv && source .venv/bin/activate`
- [ ] `pip install httpx pandas duckdb sentence-transformers google-genai streamlit cryptography py-clob-client`
- [ ] Get a Gemini API key at aistudio.google.com — sign in with a Google
      account, no credit card, no phone verification. Free tier covers
      Day 3's verification volume many times over.

---

## Secrets tracker (running list — check this all week)

Everything below lives in `.env` only, never hardcoded, never committed.
Add each row as you reach that day.

| Day | Variable(s) | Purpose |
|---|---|---|
| 0 | `GEMINI_API_KEY` | Day 3 LLM verification |
| 1 | `KALSHI_KEY_ID`, `KALSHI_PRIVATE_KEY_PATH` | Kalshi request signing |
| 4 | `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`, `TWILIO_TO_NUMBER` | Personal SMS alerts |
| 5 | Polygon wallet private key (via `py-clob-client` config, not raw `.env`) | Polymarket order signing |
| 5 | *(reuses Twilio creds above)* | Validating inbound SMS webhook signatures |
| 6 | VPS SSH key, domain DNS credentials | Deployment — lives on your machine/registrar, never referenced in the repo at all |

If you add anything not on this list, add the row here too — the goal is
one place to check "did I gitignore this" rather than relying on memory.

---

**Goal:** both platforms' open markets land in one normalized local database.

### Block 1 (60 min) — Schema + repo skeleton
- [ ] Create `db/schema.sql` with one `markets` table:
  `platform, market_id, question, rules_text, category, close_time, outcomes (json), prices (json), volume, fetched_at`
- [ ] Create `db/init.py` that creates a DuckDB file from this schema
- [ ] Folder structure: `ingest/`, `match/`, `scan/`, `execute/`, `dashboard/`, `db/`

### Block 2 (60 min) — Polymarket ingestion
- [x] Write `ingest/polymarket.py` hitting the public Gamma API:
```python
import httpx, json

GAMMA = "https://gamma-api.polymarket.com"

def fetch_polymarket_markets():
    r = httpx.get(f"{GAMMA}/markets",
                   params={"active": "true", "closed": "false", "limit": 500})
    r.raise_for_status()
    rows = []
    for m in r.json():
        rows.append({
            "platform": "polymarket",
            "market_id": m["conditionId"],
            "question": m["question"],
            "rules_text": m.get("description", ""),
            "close_time": m.get("endDate"),
            # these three fields come back as JSON-encoded strings, not arrays
            "outcomes": json.loads(m["outcomes"]),
            "prices": [float(p) for p in json.loads(m["outcomePrices"])],
            "token_ids": json.loads(m["clobTokenIds"]),
            "volume": float(m.get("volume", 0)),
        })
    return rows
```
- [x] Run it standalone, confirm you get >100 rows with sane-looking prices (0–1 range)

> **Finding (2026-07-17):** Gamma silently caps each page at ~100 rows no matter
> what `limit` you pass. The snippet above returns only 100. The implemented
> version pages with an `offset` param (stepping by page size until an empty
> page or a 2000-market cap) to get the full open-market set. `token_ids` is
> kept in the row dict for Day 5 order placement even though there's no DB
> column for it yet.

### Block 3 (60 min) — Kalshi ingestion
- [x] Write `ingest/kalshi.py`. Public `/markets` doesn't require auth, but build
      the signer now since Day 5 execution needs it anyway:
```python
import time, base64
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

def kalshi_auth_headers(method, path, key_id, private_key):
    ts = str(int(time.time() * 1000))
    msg = f"{ts}{method}{path}".encode()
    sig = private_key.sign(
        msg,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=hashes.SHA256().digest_size),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
    }
```
- [x] Fetch `GET /trade-api/v2/markets?status=open`, normalize into the same
      schema as Polymarket
- [x] Run it standalone, confirm rows land

> **Finding (2026-07-17):** The live API differs from what this block assumed.
> Host is `https://api.elections.kalshi.com`. Notable drift:
> - **Prices are NOT in cents.** They arrive as *strings* in `*_dollars` fields
>   (`yes_bid_dollars`, `yes_ask_dollars`, `last_price_dollars`), already in 0–1
>   probability. `float()`-parse them; do **no** `/100` conversion. Watch out:
>   `"0.0000"` is a non-empty string and therefore truthy — test `> 0`, not
>   truthiness, or every dead market looks "priced."
> - **Most open markets are illiquid** (~95% are all-zero, untraded). We keep
>   only markets with a real price (bid/ask midpoint, else last price) and
>   **bound the loop by markets *scanned*, not priced rows kept** — otherwise it
>   paginates through tens of thousands of dead markets forever. Scan cap: 10k.
> - **No `title` or `category` on the market object.** Question text is built
>   from `yes_sub_title` (rough — e.g. `"yes New York M,..."`). A clean question
>   needs the event title from `/trade-api/v2/events`; deferred to Day 2 as a
>   matching-quality task. `volume` is `volume_fp` (also a string).
> - The signer is built and wired in (optional — only signs when `KALSHI_KEY_ID`
>   is set). `KALSHI_KEY_ID` is not yet in `.env`; not needed until Day 5.

### Block 4 (30 min) — Land it, schedule it
- [x] `ingest/run.py` calls both fetchers, writes into the DuckDB `markets` table
- [x] Add a simple loop (every 5 min) with exponential backoff on HTTP 429s
- [x] **✅ Done when:** `python ingest/run.py` populates the `markets` table for
      both platforms and you can `SELECT COUNT(*) FROM markets GROUP BY platform`
      and see two non-trivial numbers

> **Done 2026-07-17:** `SELECT COUNT(*) FROM markets GROUP BY platform` →
> `kalshi=1057, polymarket=2000`, prices in 0–1, JSON + `close_time` stored
> cleanly. Design choices: bare `run.py` does one cycle and exits (testable);
> `--loop` runs the 5-min scheduler. Each cycle *appends* a snapshot keyed by
> `fetched_at` (history retained; scanner reads latest) rather than replacing.
> Backoff lives in a `fetch_with_backoff` wrapper around each fetcher.

---

## Day 2 — Candidate generation (4h)

**Goal:** for every open market, a short list of plausible cross-platform matches.

### Block 1 (45 min) — Category seed mapping
- [ ] Manually build `match/category_map.csv`: Polymarket tag ↔ Kalshi series
      prefix, for 4–6 categories you care about (e.g. Fed decisions, elections,
      major crypto price thresholds). Don't try to cover everything — a small
      accurate map beats a big guessy one.

### Block 2 (45 min) — Structural filters
- [ ] `match/filters.py`: given two market lists, return only pairs where
      (a) category maps per Block 1, and (b) `close_time` within 48h of each other
- [ ] Print the count of surviving pairs — this should already be a small
      fraction of the full cross product

### Block 3 (90 min) — Embedding similarity
- [ ] `match/embed.py`:
```python
from sentence_transformers import SentenceTransformer
import numpy as np

model = SentenceTransformer("all-MiniLM-L6-v2")

def top_k_candidates(poly, kalshi, k=3, min_sim=0.55):
    p_txt = [f"{m['question']} {m['rules_text']}" for m in poly]
    k_txt = [f"{m['question']} {m['rules_text']}" for m in kalshi]
    p_emb = model.encode(p_txt, normalize_embeddings=True)
    k_emb = model.encode(k_txt, normalize_embeddings=True)
    sims = p_emb @ k_emb.T
    out = []
    for i, row in enumerate(sims):
        for j in np.argsort(row)[::-1][:k]:
            if row[j] >= min_sim:
                out.append((poly[i]["market_id"], kalshi[j]["market_id"], float(row[j])))
    return out
```
- [ ] Run only on pairs that survived Block 2's structural filter
- [ ] Write results to a `candidate_pairs` table (`market_a, market_b, similarity`)

### Block 4 (30 min) — Sanity check
- [ ] Pull 10 random candidate pairs and eyeball them yourself
- [ ] Tune `min_sim` up/down until the top few candidates per market look
      plausible more often than not (you don't need it perfect — Day 3 catches errors)
- [x] **✅ Done when:** `candidate_pairs` has a manageable number of rows
      (dozens to low hundreds, not thousands) and manual spot-checking shows
      mostly sensible pairings

> **Done 2026-07-17:** 145 candidate pairs (121 politics, 14 economics, 10
> companies/tech). Key findings:
> - **Neither platform gives a usable category as assumed.** Polymarket's
>   Gamma market object has `category: null` and `tags: null` — only nested
>   `events[].title`. Kalshi markets carry no category either; the *event*
>   does. So Kalshi ingestion was reworked to drive from `/events?...&
>   with_nested_markets=true`, which yields real categories ("Elections",
>   "Economics", "Crypto", ...) and titles (Day 1's `/markets`-first scan
>   returned only sports-combo markets whose events never intersect the
>   interesting ones). `match/category_map.csv` therefore maps Polymarket
>   *keywords* ↔ Kalshi *native category names*, not tag ↔ series prefix.
> - **k=1, not k=3.** Multi-outcome events (e.g. "2028 nominee: <person>")
>   exist as one binary market per outcome on *both* platforms, so k=3 pairs
>   each Polymarket market with 3 near-duplicate Kalshi legs. k=1 gives the
>   correct 1:1 mapping and dedupes; min_sim tuned to 0.80.
> - **Embedding stage is bucketed by category** (embed each market once,
>   matrix-multiply within category, top-k) to avoid the 84M-pair cross
>   product; the 48h close_time window is applied to the small survivor set.
> - The near-misses that survive (Senate "2026" vs "2028", "Senate" vs "State
>   Senate", Trump visits "Wyoming" vs "Montana") are exactly the resolution
>   mismatches Day 3's LLM verification is built to reject — the pipeline is
>   working as designed.

---

## Day 3 — LLM verification (4h) — the core piece

**Goal:** a `verified_pairs` table you'd trust with real money.

### Block 1 (45 min) — Define the verification schema
- [ ] `match/verify.py`, using Gemini's structured-output mode so you get
      guaranteed-valid JSON back, not free text you have to parse:
```python
from google import genai
import json

client = genai.Client()  # reads GEMINI_API_KEY from env

VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "same_event": {"type": "boolean"},
        "confidence": {"type": "number"},
        "resolution_mismatch": {"type": "string", "nullable": True},
    },
    "required": ["same_event", "confidence", "resolution_mismatch"],
}

def verify_pair(a, b):
    prompt = (
        f"Market A ({a['platform']}): {a['question']}\nRules A: {a['rules_text']}\n\n"
        f"Market B ({b['platform']}): {b['question']}\nRules B: {b['rules_text']}\n\n"
        "Do these describe the same real-world event, resolving on the same "
        "criteria, source, and deadline? Flag any difference in resolution rules."
    )
    resp = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config={
            "response_mime_type": "application/json",
            "response_schema": VERIFY_SCHEMA,
        },
    )
    return json.loads(resp.text)
```

### Block 2 (60 min) — Run it over all candidates
- [ ] Loop `verify_pair` over every row in `candidate_pairs`
- [ ] Store results in `verified_pairs`: `market_a, market_b, same_event, confidence, resolution_mismatch, status`
- [ ] Set `status = auto_accept` when `same_event=True`, `confidence > 0.9`,
      and `resolution_mismatch is None`; otherwise `status = needs_review`

### Block 3 (60 min) — Manual review queue
- [ ] A short CLI (`match/review.py`) that prints each `needs_review` pair
      side by side with the LLM's stated reason for uncertainty, and lets you
      type `y`/`n`/`s` (skip)
- [ ] Work through the queue — this is genuinely the highest-leverage hour
      of the week, since it's what makes the dataset trustworthy

### Block 4 (75 min) — Close the loop
- [ ] Persist your review decisions back into `verified_pairs`
- [ ] Re-run Day 1's ingestion once more and confirm `verified_pairs` still
      references valid, still-open markets (drop any that closed)
- [~] **✅ Done when:** you have a `verified_pairs` table you're personally
      confident in, with every row either auto-accepted at high confidence or
      manually confirmed by you

> **Code complete, run blocked on quota (2026-07-17):**
> - `gemini-2.5-flash` from the plan is **retired for new API keys** (404).
>   Switched to the `gemini-flash-latest` alias (resolves to gemini-3.5-flash)
>   so it survives future retirements; override with `GEMINI_MODEL`.
> - The structured-output call shape is validated and works, but this key's
>   **free tier is capped at ~20 `generate_content_free_tier_requests`** and
>   stays saturated (persistent 429 with a ~40-55s retry hint that doesn't
>   actually clear), so the 145-pair batch could not complete — 0 verified.
> - `match/run_verify.py` is **resumable** (skips already-verified pairs) and
>   honors the server's retry hint, so this finishes on its own once quota
>   allows. NEEDS YOU: enable billing on the Gemini key (paid tier lifts the
>   cap), supply a different key, or let the resumable job run across days.
> - `match/review.py` (the y/n/s manual queue) is built but needs a human — it
>   is the one genuinely interactive step and awaits you.

---

## Day 4 — Fair value, scanner & alerts (~4.5h)

**Goal:** a live, refreshing view of price gaps on verified-equivalent
contracts, plus a way to actually get pinged when one opens up.

### Block 1 (45 min) — Normalize to probability
- [ ] `scan/pricing.py`: Kalshi YES/NO prices are reciprocal (sum to $1) —
      convert cents to 0–1 probability; Polymarket's `outcomePrices` are
      already 0–1
- [ ] Note down each platform's fee schedule as constants (check current fee
      pages for both — these change) so your spread threshold isn't fictional

### Block 2 (75 min) — Spread scanner + staleness check
- [ ] `scan/scanner.py`: for every row in `verified_pairs`, pull current
      prices, compute `abs(p_a - p_b)`, flag when the gap clears fees + a
      safety margin
- [ ] Log every flagged opportunity with timestamp to an `opportunities` table
- [ ] **Staleness check (cheap, runs every poll):** store a hash of each
      market's `rules_text` on `verified_pairs` at verification time. On
      every ingestion pull, recompute the hash for markets with a verified
      pair and compare. Match → do nothing, free. Mismatch → a platform
      edited its resolution criteria after you verified it; drop that pair
      back to `needs_review` and re-run Day 3's `verify_pair` on it alone
- [ ] **Random audit (cheap insurance):** once a day, re-verify a small
      random sample (~1%) of `verified_pairs` regardless of hash match —
      catches a wrong verification, not just a changed one. Both of these
      add only single-digit LLM calls a day, nowhere near the free tier limit

### Block 3 (45 min) — Personal SMS alert
- [ ] Sign up for a Twilio trial account, verify your own phone number under
      Verified Caller IDs. Trial accounts can text a verified number with
      **no A2P 10DLC registration and no toll-free verification** — capped
      at 50 msgs/day, messages prefixed "Sent from a Twilio Trial account"
- [ ] `alerts/sms.py`:
```python
from twilio.rest import Client

def notify_sms(account_sid, auth_token, from_number, to_number, opportunity):
    client = Client(account_sid, auth_token)
    body = (
        f"Arb: {opportunity['question'][:60]}\n"
        f"{opportunity['platform_a']} {opportunity['price_a']:.2f} vs "
        f"{opportunity['platform_b']} {opportunity['price_b']:.2f} "
        f"(spread {opportunity['spread']:.3f})"
    )
    client.messages.create(body=body, from_=from_number, to=to_number)
```
- [ ] Call it from `scan/scanner.py` right where you write to `opportunities`
- [ ] This channel is personal-only. Alerting *other* people by SMS later
      means upgrading to a paid Twilio account and completing A2P 10DLC
      brand/campaign registration — currently a 10-15 day carrier review.
      Not part of this week; start it in parallel only if you want it queued up.

### Block 4 (30 min) — RSS feed (the zero-friction path for anyone else)
- [ ] `alerts/rss.py`: append each flagged opportunity to a static XML file
      served alongside the dashboard. No account or registration on either
      end — anyone can subscribe with any RSS reader, or route it into
      whatever they already use (Discord, Slack, email digests, etc.)
- [ ] This is what the README points to for "how would someone else get
      these alerts" — it sidesteps the SMS registration problem entirely

### Block 5 (75 min) — Dashboard
- [ ] `dashboard/app.py` in Streamlit: a **public** table of verified pairs
      with live spread, sorted by size, plus a spread-history chart
- [ ] Keep trades/P&L (once Day 5 exists) on a **separate, password-gated
      page** — no reason to broadcast position sizing or realized
      performance next to the public opportunity feed
- [ ] `streamlit run dashboard/app.py`, confirm it updates on refresh
- [ ] **✅ Done when:** the dashboard shows real updating spread data,
      you've received one real test SMS on your own phone, and the RSS
      feed validates in a feed reader

---

## Day 5 — Execution layer (~4h) — go slow here

**Goal:** one real (or demo) trade placed end-to-end, safely.

### Block 1 (30 min) — Risk rails first
- [ ] `execute/risk.py`: fixed max position size per trade (start at $10–25),
      a hard daily loss cap, and a kill switch flag you can flip

### Block 2 (60 min) — Kalshi order placement, test in demo first
- [ ] Use Kalshi's demo environment to place and cancel a test order before
      touching the production endpoint
- [ ] `execute/kalshi_order.py`, reusing your Day 1 signer for authenticated
      `/portfolio/orders` calls

### Block 3 (60 min) — Polymarket order placement
- [ ] Fund a small amount of USDC on Polygon
- [ ] Use `py-clob-client` for order placement — there's no sandbox here, so
      start with a trivially small order size to confirm the flow works at all
      before relying on it

### Block 4 (45 min) — Manual-confirm trade loop (terminal, for building/testing)
- [ ] `execute/trade.py`: when the scanner flags an opportunity, print the
      proposed two-leg trade and wait for you to type `y` before sending
      either order
- [ ] Log every executed trade (both legs, prices, size) to a `trades` table
- [ ] This is the fastest way to validate risk caps and order placement work
      correctly before adding the async complexity of Block 5 on top
- [ ] **Concurrency guard:** add a `status` column to the pending-opportunity
      table (`pending` / `actioned` / `expired`). Both the terminal loop and
      the SMS webhook must check `status == pending` before executing, and
      flip it to `actioned` immediately on confirm, before the order goes
      out. Whichever channel confirms first wins; the other is a no-op.
      Needed because both paths stay live simultaneously (Block 5) — without
      this, confirming from your phone after already typing `y` at the
      terminal would fire two trades instead of one

### Block 5 (45 min) — SMS confirm loop (the way you'll actually use it)
- [ ] Since you're realistically on your phone, not the terminal, when an
      opportunity fires: build the FastAPI webhook (signature validation +
      sender check + pending-opportunity lookup) and wire it to call the
      same `execute_trade()` function Block 4 already tested
- [ ] Expose it for now with `ngrok http 8000` (or a Cloudflare Tunnel) and
      paste that temporary URL into your Twilio number's "A Message Comes
      In" webhook setting — gets you a real end-to-end test today without
      waiting on Day 6's permanent deployment
- [ ] Test it for real: let an opportunity fire, reply `Y` from your phone,
      confirm the trade executes and logs correctly
- [ ] In Day 6, once the VPS + Caddy setup is live, swap the Twilio webhook
      URL from the ngrok tunnel to the permanent `/sms-reply` route — same
      code, just a stable home for it
- [~] **✅ Done when:** you've placed one real, small trade confirmed by
      replying to a text on your phone — not the terminal — and it's logged

> **Code complete, tested in dry-run (2026-07-17):** all execute/ modules
> built and smoke-tested end-to-end against demo data:
> - **Concurrency guard verified**: two confirms on the same opportunity →
>   the second returns "already actioned", no double-fire. status flips
>   pending→actioned atomically (UPDATE ... WHERE status='pending' RETURNING)
>   before any order.
> - **Risk rails verified**: per-trade cap, daily notional cap, and kill
>   switch (execute/KILL_SWITCH file or RISK_KILL_SWITCH env) all block.
> - **Nothing auto-executes**: terminal `y` / SMS `Y` are the only triggers;
>   execute_trade dry-runs unless EXECUTE_LIVE=1 with creds present.
> - NEEDS YOU to place a *real* trade (the done-when): KALSHI_KEY_ID (for demo
>   + prod order signing), a funded Polygon wallet key (POLYMARKET_PRIVATE_KEY,
>   no sandbox), and ngrok to expose the webhook to Twilio. One caveat found:
>   Polymarket order legs need the market's clob token_id, which Day 1 fetches
>   but the schema doesn't store — resolve/store it before going live.
> - DuckDB is single-writer: run the webhook as the one writer process (the
>   terminal loop is for testing), or move to a client/server DB if both must
>   be live at once.

---

## Day 6 — Deploy & write it up (2h)

**Goal:** it's live, and a stranger on GitHub understands why it's interesting in 30 seconds.

### Block 1 (45 min) — Deploy
- [ ] Spin up a small VPS (Hetzner or DigitalOcean, ~$5–6/mo) — one Ubuntu
      box running everything, no need to split across multiple platforms
- [ ] `ingest/run.py` and `scan/scanner.py` as systemd timers
- [ ] Install Caddy as a reverse proxy in front of Streamlit — it handles
      HTTPS automatically, no manual certs
- [ ] Point a domain (or free subdomain) at the box
- [ ] Update the Twilio SMS webhook URL from the Day 5 ngrok tunnel to the
      permanent `https://yourdomain/sms-reply` route — same webhook code,
      just its stable home now
- [ ] Confirm the public opportunities page loads over HTTPS, and the
      password-gated trades page actually requires the password

### Block 2 (45 min) — README
- [ ] Lead with the framing, not the trade: this is prediction-market
      research infrastructure — the 3-stage matching pipeline (structural
      filter → embedding candidates → LLM verification) is the actual
      contribution nobody else has built for this asset class. The
      arbitrage scanner is the first proof-of-concept built on top of it,
      not the point of the project — say so explicitly rather than leading
      with "arbitrage bot"
- [ ] Include the capital non-fungibility caveat honestly (Polymarket USDC/Polygon
      vs Kalshi USD) — it reads as more credible than an unqualified "arbitrage bot" claim
- [ ] Architecture diagram (even a simple one)
- [ ] Add a short "Current scope" note: this runs as a single-operator system —
      your credentials, your risk caps, one local database. It's not multi-tenant.
      State plainly what *is* usable by others today (the public dashboard,
      the RSS feed, the verified-pairs dataset) versus what isn't (nobody
      else can trade through it)
- [ ] Add a one-paragraph "Roadmap" note naming the two things a real
      multi-user version would need next — self-serve category filtering on
      the RSS/alert side, and, much further out, per-user credential storage
      and execution — so it reads as a deliberate scope decision, not a gap
      you didn't notice
- [ ] One-line note that manual execution directly on Kalshi/Polymarket's
      own UI always remains available as a fallback if SMS, terminal, and
      the whole system are unreachable — needs no build, just worth stating

### Block 3 (30 min) — Publish the dataset
- [ ] Export `verified_pairs` (anonymized of any personal info) as a CSV —
      this is a standalone artifact almost nobody else has published
- [ ] **✅ Done when:** repo is public, dashboard URL works, README leads with
      the matching pipeline

---

## Stretch goals (if you finish early)
- Calibration study: do each platform's stated probabilities actually match
  real-world outcome frequencies? Independent research contribution using
  the verified-pairs dataset, nothing to do with trading
- Cross-platform lead-lag study: does one platform systematically move
  before the other on the same event? Directly echoes the "fast predictors"
  language from the original JD, applied to a slower-moving asset class
- Self-serve category filtering on the RSS feed (e.g. per-category feed URLs) —
  the cheapest possible step toward "other people get alerts tailored to them"
  without touching auth or credentials
- Auto-execution with tighter risk caps, after a few days of manual-confirm data
- Expand the category map to more event types
- A simple backtest: how would the scanner's flagged spreads have performed
  historically, using each platform's price-history endpoint