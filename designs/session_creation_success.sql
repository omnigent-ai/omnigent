-- One creation metric from the optional debug sink (Databricks SQL).
-- Parameters: debug_logs_table (qualified table name), workspace_id,
-- cohort_start, cohort_end, rollout_at, as_of (timestamps).
-- Use rollout_at after both server and runner instrumentation are deployed.
-- Scope the input table/view to the intended deployment, not mixed dev/prod logs.
-- Five-minute readiness deadline plus an illustrative two-minute ingestion grace.
-- Unknown/rejected requests remain in the denominator; known children do not.
-- Explicit measurement gaps suppress the rate instead of inflating failures.
WITH logs AS (
  SELECT
    client_time AS event_at,
    source,
    event_name,
    NULLIF(session_id, '') AS session_id,
    NULLIF(attributes['request_id'], '') AS request_id,
    NULLIF(attributes['runner_id'], '') AS runner_id,
    attributes['creation_kind'] AS creation_kind
  FROM IDENTIFIER(:debug_logs_table)
  WHERE workspace_id = CAST(:workspace_id AS STRING)
    AND client_time >= GREATEST(
      CAST(:cohort_start AS TIMESTAMP), CAST(:rollout_at AS TIMESTAMP)
    )
    AND client_time <= CAST(:as_of AS TIMESTAMP)
    AND source = 'server'
    AND event_name IN (
      'session_creation_started', 'session_created',
      'session_creation_accepted', 'session_creation_failed',
      'session_runner_bound', 'session_runner_ready',
      'session_readiness_unavailable', 'session_readiness_observation_failed'
    )
),
requests AS (
  SELECT
    request_id,
    MIN(CASE WHEN event_name = 'session_creation_started' THEN event_at END) AS started_at,
    MAX(session_id) AS session_id,
    MAX(CASE WHEN creation_kind = 'child' THEN 1 ELSE 0 END) AS is_child
  FROM logs
  WHERE request_id IS NOT NULL
    AND event_name IN (
      'session_creation_started', 'session_created',
      'session_creation_accepted', 'session_creation_failed'
    )
  GROUP BY request_id
),
cohort AS (
  SELECT request_id, session_id, started_at
  FROM requests
  WHERE is_child = 0  -- Includes unknown/rejected requests, even without a session.
    AND started_at < CAST(:cohort_end AS TIMESTAMP)
    AND started_at <= CAST(:as_of AS TIMESTAMP) - INTERVAL 7 MINUTES
),
binding_events AS (
  SELECT DISTINCT session_id, runner_id, event_at AS bound_at
  FROM logs
  WHERE event_name IN ('session_created', 'session_runner_bound')
    AND session_id IS NOT NULL
    AND runner_id IS NOT NULL
),
bindings AS (
  SELECT
    session_id,
    runner_id,
    bound_at,
    LEAD(bound_at) OVER (
      PARTITION BY session_id ORDER BY bound_at, runner_id
    ) AS next_bound_at
  FROM binding_events
),
observations AS (
  SELECT b.session_id, b.runner_id, l.event_at AS observed_at, l.event_name
  FROM logs l
  JOIN bindings b
    ON l.session_id = b.session_id
   AND l.runner_id = b.runner_id
   AND l.event_at >= b.bound_at
   AND (b.next_bound_at IS NULL OR l.event_at < b.next_bound_at)
  WHERE l.event_name IN (
    'session_runner_ready', 'session_readiness_unavailable',
    'session_readiness_observation_failed'
  )
),
per_creation AS (
  SELECT
    c.request_id,
    c.session_id,
    c.started_at,
    MIN(CASE WHEN r.event_name = 'session_runner_ready' THEN r.observed_at END) AS ready_at,
    COUNT_IF(r.event_name = 'session_runner_ready') > 0 AS succeeded,
    COUNT_IF(r.event_name IN (
      'session_readiness_unavailable', 'session_readiness_observation_failed'
    )) > 0 AS observation_unavailable
  FROM cohort c
  LEFT JOIN observations r
    ON r.session_id = c.session_id
   AND r.observed_at >= c.started_at
   AND r.observed_at <= c.started_at + INTERVAL 5 MINUTES
  GROUP BY c.request_id, c.session_id, c.started_at
)
SELECT
  COUNT(*) AS creation_count,
  COUNT_IF(succeeded) AS successful_creations,
  COUNT_IF(NOT succeeded AND NOT observation_unavailable) AS failed_creations,
  COUNT_IF(NOT succeeded AND observation_unavailable) AS unmeasurable_creations,
  CASE WHEN COUNT_IF(NOT succeeded AND observation_unavailable) = 0 THEN
    ROUND(100.0 * COUNT_IF(succeeded) / NULLIF(COUNT(*), 0), 2)
  END AS creation_success_rate_pct
FROM per_creation;

-- For per-request drilldown, replace the final SELECT with:
-- SELECT * FROM per_creation ORDER BY started_at DESC;
--
-- Diagnose unsuccessful rows using the existing lifecycle events, restricted to
-- [started_at, started_at + 5 minutes]. Match create errors by request_id, and
-- runner events by BOTH session_id and runner_id within that binding's lifetime.
-- Show explicit failures and last-observed stage separately. No ready event
-- means readiness_timeout; absence of a log is not proof of the root cause.
