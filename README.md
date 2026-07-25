# pm-framework

**Prediction-market research infrastructure.** The core is a 3-stage pipeline that decides when 
two contracts on Polymarket and Kalshi platforms resolve on the *same real-world event*. 
A mispricing/arbitrage scanner is the  first application built on top of the matched dataset.

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
filters shrink the set before the LLM ever runs. For example, 
embeddings surface "Oregon Senate 2026" ~ "Oregon
Senate 2028"; the LLM rejects it for the year mismatch.

## Potential Future Roadmap (a real multi-user version)

1. **Self-serve category filtering** on the RSS/alert side (per-category feed
  URLs), other people can get alerts without auth. 
2. Much further out: **per-user credential storage and execution** — the real
  work to let others trade through it.
3. A real UI, as opposed to a simple Streamlit version.
4. Heavy amount of autonomous trading. Definitely the riskiest, but could be
achieved with months of verified results along with other improvements

## Layout

| Dir | Description |
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
*Fill in Keys

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
data. Step 4 is the only step that costs money through the Claude call.

### Ongoing operation

Re-run step 1 to refresh prices, and steps 2 + 4 periodically (weekly/monthly) to
pick up new markets. The human verification aspect is **resumable** in the sense that a refresh only pays for
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

## Execution safety

Nothing trades automatically. Every trade requires a human to confirm — `y` at
the terminal or `Y` by SMS reply. A concurrency guard flips an opportunity to
`actioned` before any order is sent, so the terminal and phone can't double-fire.
Risk rails (per-trade cap, daily notional cap, kill switch) are checked on every
order. `execute_trade` dry-runs unless `EXECUTE_LIVE=1`.

Manual execution directly on Kalshi's or Polymarket's own UI always remains
available as a fallback if SMS, the terminal, and the whole system are down
(or you just want to :) )

