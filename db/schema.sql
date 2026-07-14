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
