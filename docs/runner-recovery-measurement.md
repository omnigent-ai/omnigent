# Runner Recovery Measurement Guide

This is a read-only operator guide for the relay recovery contract. It does
not replace the fixed KPI3 query and does not treat a recovered stream as a
completed turn.

## Contract and parameters

Every query takes these named parameters:

| Parameter | Meaning |
| --- | --- |
| `:debug_logs_table` | Approved table identifier, used only as `IDENTIFIER(:debug_logs_table)`. |
| `:workspace_id` | Exact workspace cohort to measure. |
| `:start_time` / `:end_time` | Inclusive/exclusive UTC `TIMESTAMP` window; `report_end = :end_time`. |

Stored `client_time` is a Databricks `TIMESTAMP`. Epoch microseconds apply only
to the ZeroBus JSON wire representation. Stable columns are `log_id`,
`client_time`, `event_name`, `session_id`, `turn_id`, `source`, `app_version`,
`hostname`, `workspace_id`, and `attributes MAP<STRING,STRING>`. The sink
serializes map values as strings, including booleans as `True`/`False`.

The rollout marker is `attributes['telemetry_schema'] =
'runner_stream_recovery.v1'`.

Deploy the server change for relay recovery telemetry and update the host
daemon for host-frame diagnostics. They are separate rollout populations.
Connected/ready markers prove server adoption; absence of host failures does
not prove that a host has the new instrumentation.

| Event | Contract |
| --- | --- |
| `runner_stream_connected` | HTTP stream accepted. Marker only; not readiness or recovery. |
| `runner_stream_ready` | This attempt consumed its first `session.heartbeat`. Initial readiness is not recovery. |
| `runner_stream_transport_lost` | Opens an outage. Attributes include `outage_id`, loss-time `runner_id`, `stream_ready`, `intentional_stop`, `grace_s`, and the marker. |
| `runner_stream_recovered` | At most one row per outage, only after a retry consumes its first heartbeat. Attributes include the same `outage_id`/`runner_id`, `recovery_attempt`, `outage_s`, `recovery_evidence = 'stream_heartbeat'`, and the marker. |
| `runner_stream_disconnected` | Give-up row for the same outage, with `decision`, `grace_s`, `outage_s`, `retries`, and the marker. |

The exact chain key is:

```text
workspace_id + source + session_id + attributes['runner_id'] + attributes['outage_id']
```

Keep `turn_id`, `hostname`, and the loss `app_version` as attribution fields,
but do not join on them alone. Do not infer a `connection_id`, join by time
proximity, or treat absent rows as successful. A retry that never reaches a
heartbeat does not recover its previous outage; a later loss gets a new ID.

## 1. Adoption and identity coverage

This query applies the explicit workspace filter to the measured cohort. It
also reports missing/other workspace rows from the same time window, so the
filter does not hide identity problems. Raw rows are intentionally counted
without silently dropping blank `log_id`; the recovery query reports the same
gap for complete outage keys.

```sql
WITH raw_window AS (
  SELECT
    log_id,
    client_time,
    event_name,
    session_id,
    turn_id,
    source,
    app_version,
    hostname,
    workspace_id,
    attributes,
    COALESCE(NULLIF(TRIM(attributes['frame_kind']), ''), '<none>') AS frame_kind
  FROM IDENTIFIER(:debug_logs_table)
  WHERE client_time >= CAST(:start_time AS TIMESTAMP)
    AND client_time < CAST(:end_time AS TIMESTAMP)
    AND event_name IN (
      'runner_stream_connected', 'runner_stream_ready',
      'runner_stream_transport_lost', 'runner_stream_recovered',
      'runner_stream_disconnected', 'host_frame_handler_failed'
    )
), scoped AS (
  SELECT *
  FROM raw_window
  WHERE TRIM(CAST(workspace_id AS STRING)) = TRIM(CAST(:workspace_id AS STRING))
), coverage AS (
  SELECT
    source, app_version, event_name, frame_kind,
    COUNT(*) AS raw_rows,
    COUNT(DISTINCT NULLIF(TRIM(CAST(log_id AS STRING)), '')) AS distinct_log_ids,
    SUM(CASE WHEN NULLIF(TRIM(CAST(log_id AS STRING)), '') IS NULL THEN 1 ELSE 0 END)
      AS missing_log_id_rows,
    SUM(CASE WHEN NULLIF(TRIM(session_id), '') IS NULL THEN 1 ELSE 0 END)
      AS missing_session_id_rows,
    SUM(CASE WHEN NULLIF(TRIM(source), '') IS NULL THEN 1 ELSE 0 END)
      AS missing_source_rows,
    SUM(CASE WHEN attributes['telemetry_schema'] = 'runner_stream_recovery.v1'
             THEN 1 ELSE 0 END) AS schema_v1_rows,
    SUM(CASE WHEN event_name IN (
               'runner_stream_transport_lost', 'runner_stream_recovered',
               'runner_stream_disconnected'
             ) AND NULLIF(TRIM(attributes['outage_id']), '') IS NULL
             THEN 1 ELSE 0 END) AS missing_outage_id_rows,
    SUM(CASE WHEN NULLIF(TRIM(attributes['runner_id']), '') IS NULL
             THEN 1 ELSE 0 END) AS missing_runner_id_rows,
    SUM(CASE WHEN NULLIF(TRIM(attributes['request_id']), '') IS NOT NULL
             THEN 1 ELSE 0 END) AS request_id_rows,
    SUM(CASE WHEN NULLIF(TRIM(attributes['host_id']), '') IS NOT NULL
             THEN 1 ELSE 0 END) AS host_id_rows,
    SUM(CASE WHEN NULLIF(TRIM(attributes['error_type']), '') IS NOT NULL
             THEN 1 ELSE 0 END) AS error_type_rows,
    SUM(CASE WHEN TRY_CAST(attributes['elapsed_ms'] AS DOUBLE) IS NOT NULL
             THEN 1 ELSE 0 END) AS elapsed_ms_rows
  FROM scoped
  GROUP BY source, app_version, event_name, frame_kind
), identity_gaps AS (
  SELECT
    SUM(CASE WHEN workspace_id IS NULL
                  OR TRIM(CAST(workspace_id AS STRING)) = ''
             THEN 1 ELSE 0 END) AS missing_workspace_rows,
    SUM(CASE WHEN workspace_id IS NOT NULL
                  AND TRIM(CAST(workspace_id AS STRING)) <> TRIM(CAST(:workspace_id AS STRING))
             THEN 1 ELSE 0 END) AS other_workspace_rows
  FROM raw_window
)
SELECT coverage.*, identity_gaps.missing_workspace_rows,
       identity_gaps.other_workspace_rows
FROM identity_gaps LEFT JOIN coverage ON TRUE
ORDER BY source, app_version, event_name, frame_kind;
```

For `host_frame_handler_failed`, group by `frame_kind`: host-wide requests
naturally may have no session or turn ID. The adopted attribution keys are
`request_id`, `frame_kind`, `host_id`, optional `runner_id`/`session_id`,
`error_type`, and `elapsed_ms`; do not substitute speculative `handler` or
`error_code` keys.

## 2. Aggregate observed outage recovery

This query uses a windowed loss timestamp and grouped `MAX`/`MIN(CASE ...)` per
exact outage key. It preserves the loss build and reports identity gaps. A
recovery is valid only with heartbeat evidence, nonnegative `outage_s`, and a
timestamp at or after the loss. Recovery plus give-up for one outage is a
conflict/unknown outcome, never automatic success. Rows with another or
missing schema marker remain visible as `unknown_schema_rows` but are excluded
from the v1 outage classification.

```sql
WITH raw_window AS (
  SELECT
    log_id, client_time, event_name, session_id, turn_id, source, app_version,
    hostname, workspace_id, attributes,
    NULLIF(TRIM(attributes['runner_id']), '') AS runner_id,
    NULLIF(TRIM(attributes['outage_id']), '') AS outage_id
  FROM IDENTIFIER(:debug_logs_table)
  WHERE client_time >= CAST(:start_time AS TIMESTAMP)
    AND client_time < CAST(:end_time AS TIMESTAMP)
    AND TRIM(CAST(workspace_id AS STRING)) = TRIM(CAST(:workspace_id AS STRING))
    AND event_name IN (
      'runner_stream_transport_lost', 'runner_stream_recovered',
      'runner_stream_disconnected'
    )
), identity_gaps AS (
  SELECT
    SUM(CASE WHEN NULLIF(TRIM(source), '') IS NULL
                  OR NULLIF(TRIM(session_id), '') IS NULL
                  OR runner_id IS NULL OR outage_id IS NULL
             THEN 1 ELSE 0 END) AS incomplete_identity_rows,
    SUM(CASE WHEN NULLIF(TRIM(CAST(log_id AS STRING)), '') IS NULL
             THEN 1 ELSE 0 END) AS missing_log_id_rows
  FROM raw_window
), schema_gaps AS (
  SELECT
    SUM(CASE WHEN attributes['telemetry_schema'] = 'runner_stream_recovery.v1'
             THEN 0 ELSE 1 END) AS unknown_schema_rows
  FROM raw_window
), ranked AS (
  SELECT
    raw_window.*,
    NULLIF(TRIM(CAST(log_id AS STRING)), '') AS log_id_key,
    ROW_NUMBER() OVER (
      PARTITION BY NULLIF(TRIM(CAST(log_id AS STRING)), '')
      ORDER BY CASE WHEN attributes['telemetry_schema'] = 'runner_stream_recovery.v1'
                    THEN 0 ELSE 1 END,
               client_time DESC
    ) AS log_rn
  FROM raw_window
), deduped AS (
  SELECT *
  FROM ranked
  WHERE log_id_key IS NULL OR log_rn = 1
), keyed AS (
  SELECT
    deduped.*,
    MIN(CASE WHEN event_name = 'runner_stream_transport_lost'
             THEN client_time END) OVER (
      PARTITION BY workspace_id, source, session_id, runner_id, outage_id
    ) AS loss_time
  FROM deduped
  WHERE NULLIF(TRIM(source), '') IS NOT NULL
    AND NULLIF(TRIM(session_id), '') IS NOT NULL
    AND runner_id IS NOT NULL
    AND outage_id IS NOT NULL
    AND attributes['telemetry_schema'] = 'runner_stream_recovery.v1'
), per_outage AS (
  SELECT
    workspace_id, source, session_id, runner_id, outage_id,
    MAX(CASE WHEN event_name = 'runner_stream_transport_lost' THEN turn_id END)
      AS loss_turn_id,
    MAX(CASE WHEN event_name = 'runner_stream_transport_lost'
             THEN NULLIF(TRIM(app_version), '') END) AS loss_app_version_value,
    COUNT(DISTINCT CASE WHEN event_name = 'runner_stream_transport_lost'
                        THEN NULLIF(TRIM(app_version), '') END) AS loss_build_count,
    MAX(CASE WHEN event_name = 'runner_stream_transport_lost'
             THEN NULLIF(TRIM(hostname), '') END) AS loss_hostname_value,
    COUNT(DISTINCT CASE WHEN event_name = 'runner_stream_transport_lost'
                        THEN NULLIF(TRIM(hostname), '') END) AS loss_hostname_count,
    MIN(loss_time) AS loss_time,
    MIN(CASE WHEN event_name = 'runner_stream_recovered'
                  AND COALESCE(attributes['recovery_evidence'] = 'stream_heartbeat', FALSE)
                  AND COALESCE(TRY_CAST(attributes['outage_s'] AS DOUBLE) >= 0, FALSE)
                  AND client_time >= loss_time
             THEN client_time END) AS recovery_time,
    MIN(CASE WHEN event_name = 'runner_stream_recovered'
                  AND COALESCE(attributes['recovery_evidence'] = 'stream_heartbeat', FALSE)
                  AND COALESCE(TRY_CAST(attributes['outage_s'] AS DOUBLE) >= 0, FALSE)
                  AND client_time >= loss_time
             THEN TRY_CAST(attributes['outage_s'] AS DOUBLE) END)
      AS recovery_outage_s,
    SUM(CASE WHEN event_name = 'runner_stream_recovered' THEN 1 ELSE 0 END)
      AS recovery_candidate_rows,
    SUM(CASE WHEN event_name = 'runner_stream_recovered'
                  AND COALESCE(attributes['recovery_evidence'] = 'stream_heartbeat', FALSE)
                  AND COALESCE(TRY_CAST(attributes['outage_s'] AS DOUBLE) >= 0, FALSE)
                  AND client_time >= loss_time
             THEN 1 ELSE 0 END) AS valid_recovery_rows,
    SUM(CASE WHEN event_name = 'runner_stream_recovered'
                  AND NOT (
                    COALESCE(attributes['recovery_evidence'] = 'stream_heartbeat', FALSE)
                    AND COALESCE(TRY_CAST(attributes['outage_s'] AS DOUBLE) >= 0, FALSE)
                    AND client_time >= loss_time
                  )
             THEN 1 ELSE 0 END) AS invalid_recovery_rows,
    MIN(CASE WHEN event_name = 'runner_stream_disconnected'
                  AND client_time >= loss_time
             THEN client_time END) AS giveup_time,
    MAX(CASE WHEN event_name = 'runner_stream_disconnected'
                  AND client_time >= loss_time
             THEN attributes['decision'] END) AS giveup_decision,
    COUNT(DISTINCT CASE WHEN event_name = 'runner_stream_disconnected'
                             AND client_time >= loss_time
                        THEN attributes['decision'] END) AS giveup_decision_count,
    SUM(CASE WHEN log_id_key IS NULL
             THEN 1 ELSE 0 END) AS missing_log_id_rows
  FROM keyed
  GROUP BY workspace_id, source, session_id, runner_id, outage_id
), classified AS (
  SELECT per_outage.*,
    CASE WHEN loss_build_count = 1 THEN loss_app_version_value
         ELSE '<unknown_or_conflict>' END AS loss_app_version,
    CASE
      WHEN loss_build_count <> 1 OR loss_hostname_count <> 1
        OR (giveup_time IS NOT NULL AND giveup_decision_count <> 1)
        OR valid_recovery_rows > 1
        THEN 'conflict_identity_or_event'
      WHEN recovery_time IS NOT NULL AND giveup_time IS NOT NULL
        THEN 'conflict_recovery_and_giveup'
      WHEN recovery_time IS NOT NULL THEN 'recovered_stream'
      WHEN giveup_time IS NOT NULL THEN 'terminal_no_recovery'
      ELSE 'unresolved_observed_loss'
    END AS outcome
  FROM per_outage
  WHERE loss_time IS NOT NULL
), summary AS (
  SELECT
    source, workspace_id, loss_app_version,
    COUNT(*) AS observed_loss_keys,
    SUM(CASE WHEN loss_build_count <> 1 THEN 1 ELSE 0 END)
      AS loss_build_unknown_or_conflict_keys,
    SUM(CASE WHEN loss_hostname_count <> 1 THEN 1 ELSE 0 END)
      AS loss_hostname_unknown_or_conflict_keys,
    SUM(missing_log_id_rows) AS missing_log_id_rows_in_complete_keys,
    SUM(invalid_recovery_rows) AS invalid_recovery_rows,
    SUM(CASE WHEN valid_recovery_rows > 1 THEN 1 ELSE 0 END)
      AS multiple_valid_recovery_keys,
    SUM(CASE WHEN outcome = 'recovered_stream' THEN 1 ELSE 0 END)
      AS clean_recovery_keys,
    SUM(CASE WHEN outcome IN (
                    'conflict_identity_or_event', 'conflict_recovery_and_giveup'
                  ) THEN 1 ELSE 0 END)
      AS conflict_keys,
    SUM(CASE WHEN outcome = 'terminal_no_recovery' THEN 1 ELSE 0 END)
      AS terminal_no_recovery_keys,
    SUM(CASE WHEN outcome = 'unresolved_observed_loss' THEN 1 ELSE 0 END)
      AS unresolved_observed_loss_keys,
    CAST(SUM(CASE WHEN outcome = 'recovered_stream' THEN 1 ELSE 0 END) AS DOUBLE)
      / NULLIF(COUNT(*), 0) AS recovery_lower_bound,
    CAST(SUM(CASE WHEN outcome IN (
                    'recovered_stream', 'conflict_identity_or_event',
                    'conflict_recovery_and_giveup', 'unresolved_observed_loss'
                  ) THEN 1 ELSE 0 END) AS DOUBLE)
      / NULLIF(COUNT(*), 0) AS recovery_upper_bound,
    CAST(SUM(CASE WHEN outcome IN (
                    'conflict_identity_or_event', 'conflict_recovery_and_giveup',
                    'unresolved_observed_loss'
                  ) THEN 1 ELSE 0 END) AS DOUBLE)
      / NULLIF(COUNT(*), 0) AS unresolved_or_conflict_share,
    percentile_approx(CASE WHEN outcome = 'recovered_stream'
                           THEN recovery_outage_s END, 0.50)
      AS recovery_latency_p50_s,
    percentile_approx(CASE WHEN outcome = 'recovered_stream'
                           THEN recovery_outage_s END, 0.95)
      AS recovery_latency_p95_s
  FROM classified
  GROUP BY source, workspace_id, loss_app_version
)
SELECT summary.*, identity_gaps.incomplete_identity_rows,
       identity_gaps.missing_log_id_rows AS missing_log_id_rows_all_events,
       schema_gaps.unknown_schema_rows
FROM identity_gaps CROSS JOIN schema_gaps LEFT JOIN summary ON TRUE
ORDER BY source, workspace_id, loss_app_version;
```

The bounds apply only to the **observed loss cohort** in the selected
workspace/window. `unresolved_observed_loss` is not necessarily right
censoring: cancellation, process death, and exporter loss can all omit the
later row. Missing telemetry also means the query cannot estimate all
production outages. Conflicts and unresolved rows are included in the upper
bound only as unknown possibilities; they are never labeled successful.

## Reading the result

- **Adoption:** require marker and identity coverage by build/source before a
  pre/post claim. Keep `report_end`, filters, and the exact query revision.
- **Recovery:** use only clean `runner_stream_recovered` rows and
  `recovery_evidence = 'stream_heartbeat'`; `outage_s` is transport-recovery
  latency, not turn latency.
- **KPI3:** calculate the unchanged fixed KPI3 definition separately, with its
  active-session denominator, exclusions, harness filters, and absolute
  `report_end`. Recovery rows are diagnostics, not a KPI numerator.
- **Attribution:** an outage ID proves the loss/retry/give-up chain, not host
  termination, proxy expiry, server stall, or initiating cause. Do not infer
  cause from time proximity. Match source/workspace/build cohorts exactly.
- **Host frames:** group `host_frame_handler_failed` by `frame_kind`; host-wide
  requests may legitimately have no session or turn ID. Use the adopted keys
  `request_id`, `frame_kind`, `host_id`, optional `runner_id`/`session_id`,
  `error_type`, and `elapsed_ms`.

No live warehouse execution was performed. Validate both standalone queries in
an approved Databricks SQL worksheet with the named parameters before using
production results.
