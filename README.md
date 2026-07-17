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
              Gemini structured output: same event? same resolution rules?
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

## Run

```bash
python ingest/run.py                   # pull both platforms (--loop for every 5m)
python match/build_candidates.py       # markets -> candidate_pairs
python match/run_verify.py             # candidate_pairs -> verified_pairs (Gemini)
python match/review.py                 # manual review queue (y/n/s)
python scan/scanner.py --notify        # flag spreads, fire RSS/SMS
streamlit run dashboard/app.py         # dashboard
python scripts/export_dataset.py       # export verified_pairs.csv
```

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
and tested. Stage-3 LLM verification is code-complete but its batch run is gated
by Gemini free-tier quota — `run_verify.py` is resumable and finishes once quota
allows (or on a paid tier). See `plan.md` for the day-by-day build log and
findings.
