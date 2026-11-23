# pm-framework

**Prediction-market research infrastructure.** The core is a 3-stage pipeline that decides when
two contracts on Polymarket and Kalshi resolve on the *same real-world event*.
A mispricing/arbitrage scanner is the first application built on top of the matched dataset.

## The matching pipeline

```
  Polymarket (Gamma API)        Kalshi (events + nested markets, RSA-PSS signed)
            \                                   /
             v                                 v
        +--------------------------------------------+
        |  ingest/  ->  market_info + market_prices   |   every 5 min: quotes, fees, token ids
        +--------------------------------------------+
                              |  ~48k markets -> ~96M possible pairs
              STAGE 1  structural filter (match/filters.py)
              same canonical category + close_time within 48h
                              |  ~96M -> ~340k pairs
              STAGE 2  embedding similarity (match/embed.py)
              MiniLM cosine on question + rules, top-1 per market >= 0.80
                              |  ~340k -> ~130 candidates
              STAGE 3  LLM verification (match/verify.py)
              Claude (Sonnet 5) structured output: same event? same rules?
                              |  auto-accept confident matches, human-review the rest
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
filters shrink the set before the LLM ever runs. For example, embeddings
surface "Oregon Senate 2026" ~ "Oregon Senate 2028"; the LLM rejects it for
the year mismatch. Funnel figures above are from the 2026-11-20 build;
`python scripts/metrics.py` prints the current ones.

## The scanner

For every verified pair, each scan computes the **executable** two-leg price —
the ask on the side where you buy YES, the bid on the side where you buy NO —
then subtracts both platforms' taker fees (`rate × p × (1−p)` per leg, with the
per-market Polymarket rate from its fee schedule and 0.07 on Kalshi) and a
safety margin. Markets without a live book fall back to mid prices and are
labeled `mid`. Before anything becomes an opportunity it must pass:

| Safeguard | What it catches | Effect |
|---|---|---|
| Staleness check | a platform edited the rules text since verification | pair → `needs_review` |
| Liquidity floor | a leg trades too thinly for its quote to be real | skipped |
| Plausibility cap | an edge too large to be a real arb (bad match or price) | pair → `needs_review` |
| Random audit | a verification that was wrong to begin with (~1% re-checked per scan) | pair → `needs_review` |

Every scan records how many raw signals each safeguard blocked (`scan_runs`),
and every pair pulled back to review is logged (`safeguard_events`).

## Layout

| Dir | Description |
|---|---|
| `ingest/` | Polymarket (Gamma) + Kalshi (event-driven, RSA-PSS signed) pulls |
| `match/` | category map, structural filter, embeddings, LLM verification, review + labeling CLIs |
| `scan/` | fee + order-book pricing, spread scanner, staleness check, random audit |
| `execute/` | risk rails, order placement, human-confirm trade loop, SMS webhook |
| `alerts/` | Twilio SMS alerts + daily digest + RSS feed |
| `dashboard/` | Streamlit: public spreads + metrics, password-gated trades |
| `db/` | DuckDB schema, migrations, retention, local database |
| `scripts/` | metrics report, dataset export |
| `deploy/` | systemd units, Caddyfile, install script |
| `tests/` | pricing, execution guard, scanner safeguards, storage |

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                   # then fill in your keys
python db/init.py                      # create (or upgrade) db/markets.duckdb
python -m pytest -q tests              # sanity check
```

## Usage

### First run — bootstrap the pipeline (in order)

```bash
python ingest/run.py                   # 1. pull both platforms (~10 s)
python match/build_candidates.py       # 2. structural filter + embeddings -> candidate_pairs
python match/run_verify.py --limit 3   # 3. smoke-test verification (~2 cents)
python match/run_verify.py             # 4. verify the rest (~0.7 cents per new pair, resumable)
python match/review.py                 # 5. clear the needs_review queue (interactive y/n/s)
python match/label_sample.py           # 6. hand-label a random sample -> precision
python scan/scanner.py                 # 7. flag mispricings -> opportunities
python scripts/metrics.py              # 8. the measured numbers
streamlit run dashboard/app.py         # 9. view spreads, metrics, history
python scripts/export_dataset.py       # (optional) export verified_pairs.csv
```

Steps 3–4 (and the scanner's audit sample) are the only steps that cost money.

### Ongoing operation

Ingestion and scanning run every 5 minutes on the server. Re-run steps 2 + 4
periodically (weekly) to pick up new markets — verification only pays for
*new* pairs. Run step 5 whenever the scanner pulls pairs back to review, and
add to step 6 over time to tighten the precision estimate.

### What runs on its own vs. what you run

Once deployed (`sudo deploy/install.sh` on the server):

| Automated | Cadence | Notes |
|---|---|---|
| `ingest/run.py` | every 5 min | quotes, fees, token ids for ~48k markets |
| `scan/scanner.py --notify` | every 5 min | spread scan + safeguards + audit sample; fires RSS/SMS |
| `alerts/daily_summary.py --send` | daily 14:00 UTC | SMS digest of open opportunities |
| `db/prune.py` | daily | drops price rows older than `DB_KEEP_DAYS`, keeps matched markets |
| `execute/webhook.py` | always-on | receives your `Y <id>` SMS confirmation |
| `dashboard/app.py` | always-on | serves the public + gated pages |

These are always **manual** (by design):

| Manual | Why |
|---|---|
| `match/build_candidates.py` | you decide when to re-scan for new markets |
| `match/run_verify.py` | paid LLM calls — kept manual so you control spend |
| `match/review.py`, `match/label_sample.py` | inherently interactive (you type y/n) |
| `execute/trade.py` | **money** — requires your `y`; never fires alone |
| `scripts/export_dataset.py` | one-off publish |

### Deploying

You need an Ubuntu 24.04 server (2 vCPU / 4 GB RAM / 40 GB disk is plenty)
that accepts your SSH key as root. Then, from your Mac:

```bash
deploy/push.sh root@SERVER_IP                    # free hostname: <ip>.sslip.io
deploy/push.sh root@SERVER_IP pm.yourdomain.com  # or your own domain (A record -> SERVER_IP)
```

`push.sh` uploads the project and runs `deploy/bootstrap.sh` on the server, which:
- installs Caddy, uv and Python 3.14,
- creates a dedicated `pm` user that every service runs as,
- builds the venv (CPU-only torch) and runs the tests,
- fills in `PM_DOMAIN`, `WEBHOOK_PUBLIC_URL`, `DASHBOARD_URL` and a generated
  `DASHBOARD_PASSWORD` in the server's `.env`,
- configures HTTPS, opens only ports 22/80/443, starts every unit above,
- and points your Twilio number's incoming-SMS webhook at the server.

Re-run the same command to ship code updates. After the first deploy the
server's `.env`, keys and database are the live copies and are never
overwritten, so run the interactive tools (`review.py`, `label_sample.py`,
`trade.py`) on the server: `ssh root@SERVER_IP`, then
`cd /opt/pm-framework && sudo -u pm .venv/bin/python match/review.py`.

Disk: each snapshot adds roughly 1–2 MB, ~0.5 GB/day before pruning.

## Execution safety

Nothing trades automatically. Every trade requires a human to confirm — `y` at
the terminal or `Y <id>` by SMS reply (a bare `Y` only works when exactly one
opportunity is open). Before any order:

- the latest prices must be fresh (≤ 15 min) and the edge still open,
- a concurrency guard flips the opportunity `pending → actioned` atomically,
  so the terminal and phone can't double-fire,
- risk rails (per-trade cap, daily notional cap, kill switch) are checked.

Orders dry-run unless `EXECUTE_LIVE=1`; Kalshi uses its demo endpoint unless
`KALSHI_DEMO=0`. If the second leg fails after the first was placed, you get an
SMS telling you to hedge the open leg by hand.

Manual execution directly on Kalshi's or Polymarket's own UI always remains
available as a fallback if SMS, the terminal, and the whole system are down
(or you just want to :) )

## Potential Future Roadmap (a real multi-user version)

1. **Self-serve category filtering** on the RSS/alert side (per-category feed
   URLs), so other people can get alerts without auth.
2. Much further out: **per-user credential storage and execution** — the real
   work to let others trade through it.
3. A real UI, as opposed to a simple Streamlit version.
4. Heavy amount of autonomous trading. Definitely the riskiest, but could be
   achieved with months of verified results along with other improvements.
5. Order-book depth (not just top of book) in the edge calculation.
