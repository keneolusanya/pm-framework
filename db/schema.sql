-- ============================================================================
-- pm-framework data model (DuckDB)
--
-- Applied idempotently by db/migrate.py on every writer connection, so a
-- deployed database picks up new tables/columns on the next ingest or scan.
-- All TIMESTAMP columns hold naive UTC (connections run with TimeZone='UTC').
-- ============================================================================

-- ---------------------------------------------------------------------------
-- Market data, split in two so 5-minute snapshots stay small:
--   market_info   one row per market, upserted each ingest (text, rules, fees)
--   market_prices one row per market per ingest cycle (quotes + volume)
--   markets       a VIEW joining the two, for readable ad-hoc queries
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS market_info (
    platform    VARCHAR   NOT NULL,   -- 'polymarket' | 'kalshi'
    market_id   VARCHAR   NOT NULL,   -- Polymarket conditionId | Kalshi ticker
    question    VARCHAR,
    rules_text  VARCHAR,              -- current resolution rules (staleness check hashes this)
    category    VARCHAR,              -- platform-native category, if any
    close_time  TIMESTAMP,
    outcomes    JSON,                 -- e.g. ["Yes","No"]
    token_ids   JSON,                 -- Polymarket CLOB token ids aligned to outcomes (NULL on Kalshi)
    fee_rate    DOUBLE,               -- taker fee coefficient: fee = rate * contracts * p * (1-p)
    tick_size   DOUBLE,               -- minimum price increment (Polymarket)
    min_size    DOUBLE,               -- minimum order size in shares (Polymarket)
    first_seen  TIMESTAMP,
    last_seen   TIMESTAMP,
    PRIMARY KEY (platform, market_id)
);

CREATE TABLE IF NOT EXISTS market_prices (
    platform    VARCHAR   NOT NULL,
    market_id   VARCHAR   NOT NULL,
    prices      JSON,                 -- mid/last YES,NO probabilities aligned to outcomes
    yes_bid     DOUBLE,               -- best YES bid (NULL if no book)
    yes_ask     DOUBLE,               -- best YES ask (NULL if no book)
    volume      DOUBLE,               -- traded volume (Polymarket USD, Kalshi contracts)
    fetched_at  TIMESTAMP NOT NULL
);

CREATE OR REPLACE VIEW markets AS
SELECT p.platform, p.market_id, i.question, i.rules_text, i.category, i.close_time,
       i.outcomes, p.prices, p.yes_bid, p.yes_ask, p.volume, p.fetched_at,
       i.token_ids, i.fee_rate
FROM market_prices p
JOIN market_info i ON i.platform = p.platform AND i.market_id = p.market_id;

-- One row per ingest cycle. Survives pruning of market_prices, so throughput
-- metrics (snapshots/day) stay accurate long after the raw rows are gone.
CREATE TABLE IF NOT EXISTS ingest_runs (
    fetched_at       TIMESTAMP NOT NULL,
    polymarket_rows  BIGINT,
    kalshi_rows      BIGINT,
    fetch_seconds    DOUBLE,
    write_seconds    DOUBLE
);

-- ---------------------------------------------------------------------------
-- Matching pipeline
-- ---------------------------------------------------------------------------

-- Candidate matches surviving the structural filter + embedding similarity.
-- market_a is always the Polymarket id, market_b the Kalshi id.
CREATE TABLE IF NOT EXISTS candidate_pairs (
    market_a    VARCHAR   NOT NULL,
    market_b    VARCHAR   NOT NULL,
    category    VARCHAR,              -- canonical category the pair matched under
    similarity  DOUBLE,               -- cosine similarity of question+rules embeddings
    created_at  TIMESTAMP NOT NULL,
    PRIMARY KEY (market_a, market_b)
);

-- Funnel counts for each build_candidates run (the "N pairs -> M candidates" numbers).
CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_at            TIMESTAMP NOT NULL,
    poly_markets      BIGINT,
    kalshi_markets    BIGINT,
    poly_mapped       BIGINT,
    kalshi_mapped     BIGINT,
    cross_product     BIGINT,         -- poly_markets * kalshi_markets
    structural_pairs  BIGINT,         -- same category AND close_time within window
    candidates        BIGINT,         -- top-k above the similarity threshold
    min_sim           DOUBLE,
    k                 INTEGER,
    window_hours      INTEGER
);

-- LLM-verified pairs. rules_hash_* snapshot the resolution text at
-- verification time so the scanner's staleness check can detect edits.
CREATE TABLE IF NOT EXISTS verified_pairs (
    market_a            VARCHAR   NOT NULL,
    market_b            VARCHAR   NOT NULL,
    same_event          BOOLEAN,
    confidence          DOUBLE,
    resolution_mismatch VARCHAR,              -- LLM's stated rules difference, or NULL
    status              VARCHAR,              -- auto_accept | auto_reject | needs_review | confirmed | rejected
    rules_hash_a        VARCHAR,
    rules_hash_b        VARCHAR,
    verified_at         TIMESTAMP NOT NULL,
    PRIMARY KEY (market_a, market_b)
);

-- Hand labels on a random sample of accepted pairs (match/label_sample.py).
-- This is what the precision figure is computed from.
CREATE TABLE IF NOT EXISTS precision_labels (
    market_a         VARCHAR   NOT NULL,
    market_b         VARCHAR   NOT NULL,
    status_at_label  VARCHAR,             -- auto_accept | confirmed, when sampled
    is_match         BOOLEAN   NOT NULL,  -- your judgment: same event, same rules?
    labeled_at       TIMESTAMP NOT NULL,
    PRIMARY KEY (market_a, market_b)
);

-- ---------------------------------------------------------------------------
-- Scanner
-- ---------------------------------------------------------------------------

-- Flagged mispricings. `status` is the concurrency guard shared by the
-- terminal loop and the SMS webhook: pending -> actioned flips atomically.
-- price_a / price_b are the YES prices the legs transact against: for a
-- book-based opportunity, the ask on the side you buy YES and the bid on the
-- side you buy NO (NO costs 1 - YES bid).
CREATE SEQUENCE IF NOT EXISTS seq_opportunity_id START 1;
CREATE TABLE IF NOT EXISTS opportunities (
    id          BIGINT    DEFAULT nextval('seq_opportunity_id') PRIMARY KEY,
    market_a    VARCHAR   NOT NULL,   -- polymarket market_id
    market_b    VARCHAR   NOT NULL,   -- kalshi market_id
    question    VARCHAR,
    price_a     DOUBLE,
    price_b     DOUBLE,
    spread      DOUBLE,               -- abs(price_a - price_b): gross edge per $1 payoff
    net_edge    DOUBLE,               -- spread after both taker fees + safety margin
    status      VARCHAR   NOT NULL,   -- pending | actioned | expired
    created_at  TIMESTAMP NOT NULL
);

-- One row per scan: how many raw signals there were and which safeguard
-- stopped each. This is the data behind "safeguards blocked X% of signals".
CREATE TABLE IF NOT EXISTS scan_runs (
    run_at               TIMESTAMP NOT NULL,
    pairs_scanned        INTEGER,     -- trusted verified pairs present in the latest snapshot
    raw_signals          INTEGER,     -- pairs with positive net edge before any safeguard
    blocked_stale        INTEGER,     -- ...of which rules text changed since verification
    blocked_illiquid     INTEGER,     -- ...of which a leg was below the volume floor
    blocked_implausible  INTEGER,     -- ...of which the edge exceeded the sanity cap
    already_open         INTEGER,     -- passed, but a pending opportunity already exists
    new_opportunities    INTEGER,
    expired              INTEGER,     -- pending opportunities retired this scan
    audit_checked        INTEGER,     -- pairs re-verified by the random audit
    audit_flagged        INTEGER      -- ...of which failed re-verification
);

-- Every time a safeguard pulls a verified pair back to needs_review.
CREATE SEQUENCE IF NOT EXISTS seq_safeguard_event_id START 1;
CREATE TABLE IF NOT EXISTS safeguard_events (
    id         BIGINT    DEFAULT nextval('seq_safeguard_event_id') PRIMARY KEY,
    event_at   TIMESTAMP NOT NULL,
    market_a   VARCHAR   NOT NULL,
    market_b   VARCHAR   NOT NULL,
    kind       VARCHAR   NOT NULL,    -- stale_rules | implausible_edge | audit_flag
    detail     VARCHAR
);

-- ---------------------------------------------------------------------------
-- Execution
-- ---------------------------------------------------------------------------

-- Executed (or dry-run / failed) trade legs. Two legs per opportunity.
CREATE SEQUENCE IF NOT EXISTS seq_trade_id START 1;
CREATE TABLE IF NOT EXISTS trades (
    id             BIGINT    DEFAULT nextval('seq_trade_id') PRIMARY KEY,
    opportunity_id BIGINT,
    platform       VARCHAR   NOT NULL,
    market_id      VARCHAR   NOT NULL,
    side           VARCHAR,              -- 'YES' | 'NO'
    price          DOUBLE,
    size           DOUBLE,               -- contracts / shares
    order_id       VARCHAR,              -- platform order id, 'DRY_RUN', or NULL on failure
    created_at     TIMESTAMP NOT NULL
);

-- ---------------------------------------------------------------------------
-- Columns added after the first release (no-ops on fresh databases).
-- ---------------------------------------------------------------------------
ALTER TABLE opportunities ADD COLUMN IF NOT EXISTS closed_at TIMESTAMP;    -- when it left pending
ALTER TABLE opportunities ADD COLUMN IF NOT EXISTS quote_basis VARCHAR;    -- 'book' (bid/ask) | 'mid'
ALTER TABLE trades ADD COLUMN IF NOT EXISTS status VARCHAR;                -- placed | dry_run | failed
ALTER TABLE trades ADD COLUMN IF NOT EXISTS error VARCHAR;                 -- failure detail
