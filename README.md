# pm-framework

**Prediction-market research infrastructure.** The core is a 3-stage pipeline
that decides when two contracts on *different* platforms (Polymarket and Kalshi)
resolve on the *same real-world event*. That cross-platform matching problem —
not the trading — is the contribution here; nobody else has built it for this
asset class.

A mispricing/arbitrage scanner is the first application built on top of the
matched dataset. It's a proof of concept that the matches are good enough to act
on, **not** the point of the project.

## The matching pipeline

```
  Polymarket (Gamma API)        Kalshi (events + nested markets, RSA-PSS signed)
            \                                   /
             \                                 /
              v                               v
        +--------------------------------------------+
        |  ingest/  ->  normalized `markets` table    |   one snapshot per pull
        +--------------------------------------------+
                              |
              STAGE 1  structural filter (match/filters.py)
              same canonical category + close_time within 48h
                              |  ~84M pairs -> thousands
                              v
              STAGE 2  embedding similarity (match/embed.py)
              MiniLM cosine, bucketed by category, top-1 per market
                              |  thousands -> ~145 candidates
                              v
              STAGE 3  LLM verification (match/verify.py)
              Claude (Sonnet 5) structured output: same event? same rules?
                              |  auto-accept high-confidence, human-review rest
                              v
                    +-------------------+
                    |  verified_pairs   |   the trustworthy dataset
                    +-------------------+
                       |             |
                 scan/ (spreads)   dashboard/ + alerts/ (RSS, SMS)
                       |
                 execute/ (human-confirmed, risk-capped)
```

Each stage is progressively more expensive and more accurate, so the cheap
filters shrink the set before the costly LLM ever runs. The stages catch what
the previous one can't: embeddings surface "Oregon Senate 2026" ~ "Oregon
Senate 2028"; the LLM rejects it for the year mismatch.

## Important caveat: capital is not fungible across platforms

This is **not** a frictionless arbitrage. Polymarket settles in USDC on Polygon;
Kalshi settles in USD via a US brokerage. You can't instantly move capital
between them, and both legs of a "locked" spread tie up separate pools of money.
The scanner finds genuine price divergences on equivalent contracts; realizing
them is subject to that non-fungibility, withdrawal times, and fees. Treat the
spreads as research signal first, trade signal second.

## What's usable by others today

This runs as a **single-operator system**: your credentials, your risk caps, one
local DuckDB file. It is not multi-tenant.

- **Public dashboard** — verified pairs and live spreads (read-only).
- **RSS feed** (`/feed.xml`) — subscribe from any reader; route into Discord,
  Slack, email. No account on either end.
- **The verified-pairs dataset** (`scripts/export_dataset.py`) — a CSV of
  cross-platform equivalent contracts, a standalone artifact.
- **Not trading** — nobody else can execute through this. It signs with one
  operator's keys and enforces one operator's risk limits.

## Roadmap (a real multi-user version)

1. **Self-serve category filtering** on the RSS/alert side (per-category feed
   URLs) — the cheapest step toward "other people get alerts tailored to them,"
   no auth required.
2. Much further out: **per-user credential storage and execution** — the real
   work to let others trade through it, deliberately out of scope for now.

## Layout

| Dir | What |
|---|---|
| `ingest/` | Polymarket (Gamma) + Kalshi (event-driven, RSA-PSS signed) pulls |
| `match/` | category map, structural filter, embeddings, LLM verification, review CLI |
| `scan/` | fee model, spread scanner, staleness check, random audit |
| `execute/` | risk rails, order placement, human-confirm trade loop, SMS webhook |
| `alerts/` | Twilio SMS + RSS feed |
| `dashboard/` | Streamlit: public spreads + password-gated trades |
| `db/` | DuckDB schema + local database |
| `deploy/` | systemd timers + Caddyfile |

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python db/init.py                      # create db/markets.duckdb
cp .env.example .env                   # then fill in your keys
```

Secrets live in `.env` only (never committed). See the secrets table in
`plan.md` for what each day needs.

## Usage

### First run — bootstrap the pipeline (in order)

```bash
python ingest/run.py                   # 1. pull both platforms -> markets
python match/build_candidates.py       # 2. markets -> candidate_pairs (structural + embeddings)
python match/run_verify.py --limit 3   # 3. smoke-test verification (~2 cents)
python match/run_verify.py             # 4. full verification -> verified_pairs (~$1, resumable)
python match/review.py                 # 5. clear the needs_review queue (interactive y/n/s)
python scan/scanner.py                 # 6. flag mispricings -> opportunities
streamlit run dashboard/app.py         # 7. view spreads + history
python scripts/export_dataset.py       # (optional) export verified_pairs.csv
```

Steps 1–4 populate the database; after that the scanner and dashboard have real
data to work with. Step 4 is the only step that costs money (Claude calls).

### Ongoing operation

Re-run step 1 to refresh prices, and steps 2 + 4 periodically (weekly/monthly) to
pick up new markets — verification is **resumable**, so a refresh only pays for
*new* pairs, not the whole set again. In production these are wired to run on a
schedule (see `deploy/`).

### What runs on its own vs. what you run

Once deployed (systemd timers in `deploy/`), these run automatically:

| Automated | Cadence | Notes |
|---|---|---|
| `ingest/run.py` | every 5 min | price refresh |
| `scan/scanner.py --notify` | every 5 min | spread scan + staleness check + ~1% daily audit; fires RSS/SMS |
| `execute/webhook.py` | always-on | receives your `Y` SMS confirmation |
| `dashboard/app.py` | always-on | serves the public + gated pages |

These are always **manual** (by design):

| Manual | Why |
|---|---|
| `match/build_candidates.py` | you decide when to re-scan for new markets |
| `match/run_verify.py` | the ~$1 full run — kept manual so you control spend |
| `match/review.py` | inherently interactive (you type y/n/s) |
| `execute/trade.py` | **money** — requires your `y`; never fires alone |
| `scripts/export_dataset.py` | one-off publish |

### Cost & cadence

The automated Claude usage is only the scanner's staleness + audit checks —
single-digit calls/day, roughly **$1–3/month**. The expensive **full verification
(~$1) is manual** and only runs when you invoke it. Set a low monthly spend cap
on your Anthropic key as a guardrail; the key needs no expiry for the system to
keep running (an expired key silently disables the audit safety net — see
Execution safety).

## Execution safety

Nothing trades automatically. Every trade requires a human to confirm — `y` at
the terminal or `Y` by SMS reply. A concurrency guard flips an opportunity to
`actioned` before any order is sent, so the terminal and phone can't double-fire.
Risk rails (per-trade cap, daily notional cap, kill switch) are checked on every
order. `execute_trade` dry-runs unless `EXECUTE_LIVE=1`.

Manual execution directly on Kalshi's or Polymarket's own UI always remains
available as a fallback if SMS, the terminal, and the whole system are down.

## Status

The matching pipeline, scanner, alerts, dashboard, and execution layer are built
and tested. Stage-3 LLM verification runs on Claude Sonnet 5 (`match/verify.py`);
add `ANTHROPIC_API_KEY` to `.env` and run `python match/run_verify.py` to populate
`verified_pairs` (resumable; a full run is ~$1). See `plan.md` for the day-by-day
build log and findings.
