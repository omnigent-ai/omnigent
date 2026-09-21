-- One creation metric from the optional debug sink (Databricks SQL).
-- Parameters: debug_logs_table, workspace_id, cohort_start, cohort_end,
-- rollout_at, as_of. Scope the table/view to one deployment, not mixed dev/prod.
-- rollout_at must follow deployment of BOTH server and runner instrumentation.
-- Retain history since rollout: a shared runner can connect before cohort_start.
-- Five-minute deadline plus an illustrative two-minute ingestion grace.
WITH logs AS (
  SELECT DISTINCT
    client_time AS event_at, source, event_name,
    NULLIF(session_id, '') AS session_id,
    NULLIF(attributes['request_id'], '') AS request_id,
    NULLIF(attributes['runner_id'], '') AS runner_id,
    attributes['creation_kind'] AS creation_kind,
    attributes['harness'] AS harness,
    attributes['terminal_name'] AS terminal_name,
    attributes['superseded'] AS superseded
  FROM IDENTIFIER(:debug_logs_table)
  WHERE workspace_id = CAST(:workspace_id AS STRING)
    AND client_time >= CAST(:rollout_at AS TIMESTAMP)
    AND client_time <= CAST(:as_of AS TIMESTAMP)
    AND source IN ('server', 'runner')
    AND event_name IN (
      'session_creation_started', 'session_created',
      'session_creation_accepted', 'session_creation_failed', 'session_runner_bound',
      'session_runner_unbound',
      'runner_connected', 'runner_disconnected', 'runner_session_initialized',
      'runner_stream_connected', 'runner_stream_ready', 'runner_stream_closed',
      'native_input_starting', 'native_input_ready', 'native_input_stopped',
      'terminal_exit_observed', 'terminal_close_requested'
    )
),
requests AS (
  SELECT request_id,
    MIN(CASE WHEN event_name = 'session_creation_started' THEN event_at END) AS started_at,
    MAX(session_id) AS session_id,
    MAX(CASE WHEN creation_kind = 'child' THEN 1 ELSE 0 END) AS is_child
  FROM logs
  WHERE source = 'server' AND request_id IS NOT NULL
    AND event_name IN ('session_creation_started', 'session_created',
      'session_creation_accepted', 'session_creation_failed')
  GROUP BY request_id
),
cohort AS (
  SELECT request_id, session_id, started_at FROM requests
  WHERE is_child = 0 -- Includes unknown/rejected requests without a session.
    AND started_at >= CAST(:cohort_start AS TIMESTAMP)
    AND started_at < CAST(:cohort_end AS TIMESTAMP)
    AND started_at <= CAST(:as_of AS TIMESTAMP) - INTERVAL 7 MINUTES
),
binding_events AS (
  SELECT DISTINCT session_id,
    CASE WHEN event_name = 'session_runner_unbound' THEN NULL ELSE runner_id END AS runner_id,
    event_at AS bound_at
  FROM logs WHERE source = 'server'
    AND event_name IN ('session_created', 'session_runner_bound', 'session_runner_unbound')
    AND session_id IS NOT NULL
),
binding_changes AS (
  SELECT *, LAG(runner_id) OVER (
    PARTITION BY session_id ORDER BY bound_at, runner_id
  ) AS previous_runner_id FROM binding_events
),
bindings AS (
  SELECT session_id, runner_id, bound_at,
    LEAD(bound_at) OVER (PARTITION BY session_id ORDER BY bound_at, runner_id) AS bound_until
  FROM binding_changes
  WHERE COALESCE(runner_id, '') != COALESCE(previous_runner_id, '')
),
connections AS (
  SELECT runner_id, event_name, event_at,
    LEAD(event_at) OVER (PARTITION BY runner_id ORDER BY event_at, event_name) AS ended_at
  FROM logs WHERE source = 'server'
    AND event_name IN ('runner_connected', 'runner_disconnected')
),
relays AS (
  SELECT session_id, runner_id, event_name, event_at,
    LEAD(event_at) OVER (
      PARTITION BY session_id, runner_id ORDER BY event_at, event_name
    ) AS ended_at
  FROM logs WHERE source = 'server'
    AND event_name IN ('runner_stream_connected', 'runner_stream_ready', 'runner_stream_closed')
),
native_states AS (
  SELECT session_id, runner_id, event_name, event_at, harness,
    LEAD(event_at) OVER (
      PARTITION BY session_id, runner_id ORDER BY event_at, event_name
    ) AS ended_at
  FROM logs WHERE source = 'runner' AND (
    event_name IN ('native_input_starting', 'native_input_ready', 'native_input_stopped')
    OR (event_name IN ('terminal_exit_observed', 'terminal_close_requested')
        AND terminal_name IN ('claude', 'codex')
        AND COALESCE(LOWER(superseded), 'false') != 'true')
  )
),
readiness_candidates AS (
  SELECT b.session_id, b.runner_id,
    GREATEST(b.bound_at, c.event_at, i.event_at, r.event_at, n.event_at) AS ready_at,
    b.bound_until, c.ended_at AS connection_until,
    r.ended_at AS relay_until, n.ended_at AS native_until
  FROM bindings b
  JOIN connections c ON c.runner_id = b.runner_id AND c.event_name = 'runner_connected'
  JOIN logs i ON i.session_id = b.session_id AND i.runner_id = b.runner_id
    AND i.source = 'runner' AND i.event_name = 'runner_session_initialized'
    AND i.harness IN ('claude-native', 'codex-native')
    AND i.event_at >= b.bound_at AND i.event_at >= c.event_at
    AND (c.ended_at IS NULL OR i.event_at < c.ended_at)
  JOIN relays r ON r.session_id = b.session_id AND r.runner_id = b.runner_id
    AND r.event_name = 'runner_stream_ready' AND r.event_at >= c.event_at
    AND r.event_at >= b.bound_at
  JOIN native_states n ON n.session_id = b.session_id AND n.runner_id = b.runner_id
    AND n.event_name = 'native_input_ready' AND n.harness = i.harness
    AND n.event_at >= b.bound_at
    -- Native input can remain ready across a tunnel reconnect; init/relay cannot.
),
ready AS (
  SELECT session_id, runner_id, ready_at FROM readiness_candidates
  WHERE (bound_until IS NULL OR ready_at < bound_until)
    AND (connection_until IS NULL OR ready_at < connection_until)
    AND (relay_until IS NULL OR ready_at < relay_until)
    AND (native_until IS NULL OR ready_at < native_until)
),
coverage_gaps AS (
  SELECT DISTINCT b.session_id, l.event_at
  FROM logs l JOIN bindings b
    ON l.session_id = b.session_id AND l.runner_id = b.runner_id
    AND l.event_at >= b.bound_at
    AND (b.bound_until IS NULL OR l.event_at < b.bound_until)
  WHERE l.source = 'runner'
    AND l.event_name IN ('runner_session_initialized', 'native_input_starting')
    AND COALESCE(l.harness, '') NOT IN ('claude-native', 'codex-native')
),
per_creation AS (
  SELECT c.request_id, c.session_id, c.started_at, MIN(r.ready_at) AS ready_at,
    COUNT(r.ready_at) > 0 AS succeeded,
    COUNT(g.event_at) > 0 AS observation_unavailable
  FROM cohort c
  LEFT JOIN ready r ON r.session_id = c.session_id
    AND r.ready_at >= c.started_at AND r.ready_at <= c.started_at + INTERVAL 5 MINUTES
  LEFT JOIN coverage_gaps g ON g.session_id = c.session_id
    AND g.event_at >= c.started_at AND g.event_at <= c.started_at + INTERVAL 5 MINUTES
  GROUP BY c.request_id, c.session_id, c.started_at
)
SELECT COUNT(*) AS creation_count,
  COALESCE(COUNT_IF(succeeded), 0) AS successful_creations,
  COALESCE(COUNT_IF(NOT succeeded AND NOT observation_unavailable), 0) AS failed_creations,
  COALESCE(COUNT_IF(NOT succeeded AND observation_unavailable), 0) AS unmeasurable_creations,
  CASE WHEN COALESCE(COUNT_IF(NOT succeeded AND observation_unavailable), 0) = 0 THEN
    ROUND(100.0 * COUNT_IF(succeeded) / NULLIF(COUNT(*), 0), 2)
  END AS creation_success_rate_pct
FROM per_creation;

-- Drilldown: replace the final SELECT with SELECT * FROM per_creation.
-- Match request failures by request_id, then pipeline logs by BOTH session_id
-- and runner_id within binding and deadline bounds. Report explicit failure
-- stages separately from the last observed milestone. Missing readiness is a
-- deadline miss, not proof of a particular root cause. Do not require all logs.
