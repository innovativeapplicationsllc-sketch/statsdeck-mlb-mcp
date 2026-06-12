-- StatsDeck usage analytics schema.
-- Idempotent: safe to run repeatedly (CREATE ... IF NOT EXISTS everywhere).
-- Designed for usage analytics now AND to extend toward billing/entitlements later.

CREATE TABLE IF NOT EXISTS usage_events (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Who: the Clerk user ID (token subject). 'default' for stdio / unauthenticated.
    user_id     TEXT        NOT NULL,

    -- What kind of activity: 'tool_call' | 'prompt_used'. (Discovery requests are
    -- never recorded, so this table only reflects real user activity.)
    event_type  TEXT        NOT NULL,

    -- Exactly one of these is set depending on event_type.
    tool_name   TEXT,
    prompt_name TEXT,

    -- Nullable now; lets the same schema serve NFL etc. later without migration.
    sport       TEXT,

    -- Non-sensitive query params only (player queried, timeframe, days, ...).
    -- No personal data — just what's useful for analytics.
    params      JSONB,

    -- Outcome + performance.
    success     BOOLEAN,
    error_type  TEXT,
    latency_ms  INTEGER,        -- how long the underlying data fetch took
    cache_hit   BOOLEAN         -- nullable: NULL when the call did no cache lookup
);

-- Indexes for the queries we actually run: per-user history, time-window
-- rollups (DAU/WAU), and tool popularity.
CREATE INDEX IF NOT EXISTS idx_usage_events_user_id      ON usage_events (user_id);
CREATE INDEX IF NOT EXISTS idx_usage_events_created_at   ON usage_events (created_at);
CREATE INDEX IF NOT EXISTS idx_usage_events_user_created ON usage_events (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_usage_events_tool_name    ON usage_events (tool_name);
CREATE INDEX IF NOT EXISTS idx_usage_events_event_type   ON usage_events (event_type);
