-- ===========================================================================
-- StatsDeck — ready-made usage analytics queries.
--
-- Run any of these directly with psql:
--     psql "$DATABASE_URL" -f scripts/analytics_queries.sql        (runs all)
--     psql "$DATABASE_URL" -c "<paste one query>"
--
-- Or via the helper (prettier output, named queries):
--     python scripts/run_analytics.py dau
--     python scripts/run_analytics.py user-history --user user_2abc...
--
-- "Activity" below = real user actions only (tool_call + prompt_used). Discovery
-- requests are never recorded, so these numbers reflect what users actually did.
-- ===========================================================================


-- name: dau ------------------------------------------------------------------
-- Daily Active Users (distinct users per day), last 30 days.
SELECT date_trunc('day', created_at)::date AS day,
       COUNT(DISTINCT user_id)             AS active_users,
       COUNT(*)                            AS events
FROM usage_events
WHERE created_at >= now() - INTERVAL '30 days'
GROUP BY day
ORDER BY day DESC;


-- name: wau ------------------------------------------------------------------
-- Weekly Active Users (distinct users per ISO week), last 12 weeks.
SELECT date_trunc('week', created_at)::date AS week_start,
       COUNT(DISTINCT user_id)              AS active_users,
       COUNT(*)                             AS events
FROM usage_events
WHERE created_at >= now() - INTERVAL '12 weeks'
GROUP BY week_start
ORDER BY week_start DESC;


-- name: calls-per-user -------------------------------------------------------
-- Total tool calls per user, plus prompt usage and last-seen time.
SELECT user_id,
       COUNT(*) FILTER (WHERE event_type = 'tool_call')   AS tool_calls,
       COUNT(*) FILTER (WHERE event_type = 'prompt_used') AS prompts_used,
       COUNT(*)                                           AS total_events,
       MAX(created_at)                                    AS last_seen
FROM usage_events
GROUP BY user_id
ORDER BY tool_calls DESC;


-- name: tool-popularity ------------------------------------------------------
-- Most / least used tools (order DESC = most used; reverse for least used).
SELECT tool_name,
       COUNT(*)                AS calls,
       COUNT(DISTINCT user_id) AS distinct_users,
       ROUND(AVG(latency_ms))  AS avg_latency_ms,
       ROUND(100.0 * AVG((success)::int), 1) AS success_pct
FROM usage_events
WHERE event_type = 'tool_call' AND tool_name IS NOT NULL
GROUP BY tool_name
ORDER BY calls DESC;


-- name: error-rate -----------------------------------------------------------
-- Overall + per-tool error rate (tool calls only).
SELECT COALESCE(tool_name, '(all tools)')                 AS tool_name,
       COUNT(*)                                           AS calls,
       COUNT(*) FILTER (WHERE success IS FALSE)           AS errors,
       ROUND(100.0 * COUNT(*) FILTER (WHERE success IS FALSE) / NULLIF(COUNT(*), 0), 2) AS error_pct
FROM usage_events
WHERE event_type = 'tool_call'
GROUP BY ROLLUP (tool_name)
ORDER BY calls DESC;


-- name: error-types ----------------------------------------------------------
-- Breakdown of what's failing.
SELECT tool_name, error_type, COUNT(*) AS occurrences
FROM usage_events
WHERE success IS FALSE
GROUP BY tool_name, error_type
ORDER BY occurrences DESC;


-- name: cache-hit-rate -------------------------------------------------------
-- Cache effectiveness per tool (only calls that did a cache lookup).
SELECT tool_name,
       COUNT(*) FILTER (WHERE cache_hit IS NOT NULL)      AS calls_with_lookup,
       COUNT(*) FILTER (WHERE cache_hit IS TRUE)          AS hits,
       ROUND(100.0 * COUNT(*) FILTER (WHERE cache_hit IS TRUE)
             / NULLIF(COUNT(*) FILTER (WHERE cache_hit IS NOT NULL), 0), 1) AS hit_pct
FROM usage_events
WHERE event_type = 'tool_call'
GROUP BY tool_name
ORDER BY calls_with_lookup DESC;


-- name: user-history ---------------------------------------------------------
-- Full per-user activity history. Set :user before running, e.g.:
--   psql "$DATABASE_URL" -v user="'user_2abc...'" -f scripts/analytics_queries.sql
-- (The run_analytics.py helper passes --user for you.)
SELECT created_at, event_type, tool_name, prompt_name, sport,
       success, error_type, latency_ms, cache_hit, params
FROM usage_events
WHERE user_id = :user
ORDER BY created_at DESC
LIMIT 500;
