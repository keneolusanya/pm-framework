-- ============================================================================
-- pm-framework data model (DuckDB)
-- ============================================================================

-- Cross-platform prediction market data.
-- One row per market pull; (platform, market_id, fetched_at) identifies a snapshot.
CREATE TABLE IF NOT EXISTS markets (
    platform    VARCHAR   NOT NULL,   -- 'polymarket' | 'kalshi'
    market_id   VARCHAR   NOT NULL,   -- platform-native market identifier
    question    VARCHAR,              -- human-readable market question
    rules_text  VARCHAR,              -- full resolution rules / description
    category    VARCHAR,              -- platform-assigned category/tag
    close_time  TIMESTAMP,            -- when the market closes/resolves
    outcomes    JSON,                 -- list of outcome labels, e.g. ["Yes","No"]
    prices      JSON,                 -- outcome -> price map, aligned to outcomes
    volume      DOUBLE,               -- traded volume (platform units)
    fetched_at  TIMESTAMP NOT NULL    -- when this snapshot was pulled
);

-- candidate cross-platform matches surviving structural + embedding filters.
-- market_a is always the Polymarket id, market_b the Kalshi id.
CREATE TABLE IF NOT EXISTS candidate_pairs (
    market_a    VARCHAR   NOT NULL,   -- polymarket market_id
    market_b    VARCHAR   NOT NULL,   -- kalshi market_id
    category    VARCHAR,              -- canonical category the pair matched under
    similarity  DOUBLE,               -- cosine similarity of question embeddings
    created_at  TIMESTAMP NOT NULL,
    PRIMARY KEY (market_a, market_b)
);

-- LLM-verified pairs. rules_hash_* snapshot the resolution text at
-- verification time so Day 4's staleness check can detect edits.
CREATE TABLE IF NOT EXISTS verified_pairs (
    market_a            VARCHAR   NOT NULL,   -- polymarket market_id
    market_b            VARCHAR   NOT NULL,   -- kalshi market_id
    same_event          BOOLEAN,
    confidence          DOUBLE,
    resolution_mismatch VARCHAR,              -- LLM's stated rules difference, or NULL
    status              VARCHAR,              -- auto_accept | needs_review | confirmed | rejected
    rules_hash_a        VARCHAR,              -- sha256 of market_a rules_text at verify time
    rules_hash_b        VARCHAR,              -- sha256 of market_b rules_text at verify time
    verified_at         TIMESTAMP NOT NULL,
    PRIMARY KEY (market_a, market_b)
);

-- flagged mispricing opportunities. `status` is the concurrency guard
-- shared by the terminal loop and the SMS webhook (Day 5).
CREATE SEQUENCE IF NOT EXISTS seq_opportunity_id START 1;
CREATE TABLE IF NOT EXISTS opportunities (
    id          BIGINT    DEFAULT nextval('seq_opportunity_id') PRIMARY KEY,
    market_a    VARCHAR   NOT NULL,   -- polymarket market_id
    market_b    VARCHAR   NOT NULL,   -- kalshi market_id
    question    VARCHAR,
    price_a     DOUBLE,               -- polymarket YES probability
    price_b     DOUBLE,               -- kalshi YES probability
    spread      DOUBLE,               -- abs(price_a - price_b)
    net_edge    DOUBLE,               -- spread after fees + safety margin
    status      VARCHAR   NOT NULL,   -- pending | actioned | expired
    created_at  TIMESTAMP NOT NULL
);

-- executed trades. Two legs per opportunity (one row each).
CREATE SEQUENCE IF NOT EXISTS seq_trade_id START 1;
CREATE TABLE IF NOT EXISTS trades (
    id             BIGINT    DEFAULT nextval('seq_trade_id') PRIMARY KEY,
    opportunity_id BIGINT,
    platform       VARCHAR   NOT NULL,   -- 'polymarket' | 'kalshi'
    market_id      VARCHAR   NOT NULL,
    side           VARCHAR,              -- 'YES' | 'NO'
    price          DOUBLE,
    size           DOUBLE,               -- contracts / shares
    order_id       VARCHAR,              -- platform order id, if returned
    created_at     TIMESTAMP NOT NULL
);
