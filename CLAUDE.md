# pm-framework

Prediction-market research infrastructure: a 3-stage cross-platform matching
pipeline (Polymarket + Kalshi) — structural filter → embedding similarity →
LLM verification. Arbitrage scanner is the first application built
on top, more later.

## Commands
- Activate env: `source .venv/bin/activate`
- Run tests: `python -m pytest -q tests`
- Run ingestion: `python ingest/run.py`
- Run the scanner: `python scan/scanner.py` (`--no-audit` skips the paid LLM sample)
- Print measured metrics: `python scripts/metrics.py`
- Run the dashboard: `streamlit run dashboard/app.py`
- Point any command at another database: `PM_DB_PATH=/path/to/file.duckdb`
- Verify a file is actually gitignored before committing: `git check-ignore -v <file>`

## Architecture
- `ingest/` — Polymarket (Gamma API) + Kalshi (RSA-PSS signed REST) market pulls,
  including best bid/ask, CLOB token ids and per-market taker fee rates
- `match/` — category map, structural filter, embeddings, LLM verification
  (Claude Sonnet 5 structured output), review + precision-labeling CLIs
- `scan/` — fee/order-book pricing, mispricing scanner, staleness check, random audit
- `execute/` — risk caps, order placement, trade confirmation (terminal + SMS)
- `alerts/` — Twilio SMS alerts + daily digest + the RSS feed
- `dashboard/` — Streamlit app: public spreads + metrics pages, password-gated trades page
- `db/` — DuckDB schema, migrations, retention, and the local database file
- `scripts/` — metrics report, dataset export
- `deploy/` — systemd units + Caddyfile + install script

## Database conventions
- Open the database only through `match/data.py: connect()`. It sets the session
  to UTC, waits out other processes' locks, and applies `db/schema.sql` + migrations.
- DuckDB allows one writer process *or* many readers. Never hold a connection
  open across network calls, LLM calls, or `input()` — open, write, close.
- All TIMESTAMP columns are naive UTC. Write `datetime.now(timezone.utc).replace(tzinfo=None)`.
- Schema changes go in `db/schema.sql` (idempotent `CREATE ... IF NOT EXISTS` /
  `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`); one-time data migrations in `db/migrate.py`.

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
  goes through `py-clob-client`, not raw HTTP calls, and is placed per outcome
  token id (never the market's conditionId).
