# pm-framework

Prediction-market research infrastructure: a 3-stage cross-platform matching
pipeline (Polymarket + Kalshi) — structural filter → embedding similarity →
LLM verification. Arbitrage scanner is the first application built
on top, more later.

## Commands
- Activate env: `source .venv/bin/activate`
- Run ingestion: `python ingest/run.py`
- Run the scanner: `python scan/scanner.py`
- Run the dashboard: `streamlit run dashboard/app.py`
- Verify a file is actually gitignored before committing: `git check-ignore -v <file>`

## Architecture
- `ingest/` — Polymarket (Gamma API) + Kalshi (RSA-PSS signed REST) market pulls
- `match/` — candidate generation (embeddings) + LLM verification (Gemini structured output)
- `scan/` — mispricing scanner, plus the staleness check and random audit on verified pairs
- `execute/` — risk caps, order placement, trade confirmation (terminal + SMS)
- `alerts/` — Twilio SMS alerts/replies + the RSS feed
- `dashboard/` — Streamlit app: public spreads page + password-gated trades page
- `db/` — DuckDB schema and the local database file live here

## Critical rules — this handles real money and real credentials

- **Never hardcode secrets.** API keys, the Kalshi private key path, Twilio
  credentials — always read from `.env` via `os.environ`.
- **Never commit `.env`, `*.pem`, `*.key`, or `kalshi_private*.txt`.** Run
  `git check-ignore -v <file>` on any new credential-shaped file before
  committing. Don't rely on `.gitignore`.
- **Never add auto-execution.** Trades are never automatic. Do not try to build
  any level of automatic trading
- **Preserve the concurrency guard in `execute/`.** An opportunity's
  `status` must flip to `actioned` the instant either the terminal or the
  SMS webhook confirms it, and both paths must check that status before
  placing an order. This is what stops the same trade firing twice.
- Kalshi calls need RSA-PSS request signing; Polymarket order placement
  goes through `py-clob-client`, not raw HTTP calls.