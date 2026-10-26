# Delayed runner disconnect attribution

A rise in the generic `runner_disconnected` category can be a change in
attribution rather than an increase in lost connections. Compare all failures
with that error code, including their more specific categories, before
interpreting the generic category's trend.

The server's `_hold_for_offline_host` in `omnigent/server/app.py` waits when
a runner and its user-run host disappear together. After the ordinary
disconnect grace, the turn can remain held for up to `HOST_OFFLINE_HOLD_S`
(24 hours). If the host returns first, its original runner gets another
disconnect grace to reconnect. The sweep publishes `runner_disconnected`
when the hold expires or the host returns without that runner.

```text
tunnel closes → 90-second grace → host-offline hold → hold ends → failure
                                 seconds to 24 hours
```

A classifier that looks for a tunnel close only in the 120 seconds before
the failure misses these closes. They fall back to `runner_disconnected`
even when the earlier close has diagnostic detail. The error timestamp is
the time the server gave up waiting, not the time the connection dropped.

## Correlate the hold before interpreting the failure

The server emits `runner_disconnect_held_host_offline` at the start and
`runner_disconnect_hold_ended` at the end. The end carries `runner_id`,
`outcome`, and `held_s`. Match it to the canonical `session_turn_failed`
event using workspace, session, server hostname, and request ID. A short
time window then bounds the match; time proximity alone is insufficient.

The query below uses the same named parameters as
[the recovery measurement guide](runner-recovery-measurement.md#contract-and-parameters).
It returns one row per identified canonical failure. It deduplicates log IDs,
keeps unmatched and ambiguous failures visible, and accepts only the two
hold outcomes that can lead to a failure. Missing request IDs or hostname
leave the failure unattributed. The five-second end-to-failure window is
conservative: a slower reconciliation can remain unmatched.

```sql
WITH raw_events AS (
  SELECT log_id, workspace_id, session_id, hostname, client_time, event_name,
    attributes
  FROM IDENTIFIER(:debug_logs_table)
  WHERE workspace_id = :workspace_id AND source = 'server'
    AND client_time >= CAST(:start_time AS TIMESTAMP) - INTERVAL 5 SECONDS
    AND client_time < CAST(:end_time AS TIMESTAMP)
    AND event_name IN ('session_turn_failed', 'runner_disconnect_hold_ended')
    AND NULLIF(TRIM(log_id), '') IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, log_id
    ORDER BY client_time) = 1
), failures AS (
  SELECT *, NULLIF(TRIM(attributes['request_id']), '') AS request_id
  FROM raw_events
  WHERE event_name = 'session_turn_failed'
    AND attributes['code'] = 'runner_disconnected'
    AND client_time >= CAST(:start_time AS TIMESTAMP)
), hold_ends AS (
  SELECT *, NULLIF(TRIM(attributes['request_id']), '') AS request_id,
    NULLIF(TRIM(attributes['runner_id']), '') AS runner_id,
    attributes['outcome'] AS outcome,
    TRY_CAST(attributes['held_s'] AS DOUBLE) AS held_s
  FROM raw_events
  WHERE event_name = 'runner_disconnect_hold_ended'
), matched AS (
  SELECT f.log_id AS failure_log_id, f.workspace_id, f.session_id, f.hostname,
    f.request_id, f.client_time AS failure_time,
    COUNT(h.log_id) AS hold_end_matches,
    MAX(h.runner_id) AS candidate_runner_id,
    MAX(h.outcome) AS candidate_outcome,
    MAX(h.held_s) AS candidate_held_s
  FROM failures f LEFT JOIN hold_ends h
    ON h.workspace_id = f.workspace_id AND h.session_id = f.session_id
    AND h.hostname = f.hostname AND NULLIF(TRIM(f.hostname), '') IS NOT NULL
    AND h.request_id = f.request_id
    AND h.client_time BETWEEN f.client_time - INTERVAL 5 SECONDS AND f.client_time
  GROUP BY ALL
)
SELECT failure_log_id, workspace_id, session_id, hostname, request_id, failure_time,
  hold_end_matches,
  CASE WHEN hold_end_matches = 1 THEN candidate_runner_id END AS runner_id,
  CASE WHEN hold_end_matches = 1 THEN candidate_outcome END AS hold_outcome,
  CASE WHEN hold_end_matches = 1 THEN candidate_held_s END AS held_s,
  CASE
    WHEN hold_end_matches > 1 THEN 'ambiguous_hold'
    WHEN hold_end_matches = 0 OR candidate_runner_id IS NULL
      OR candidate_held_s IS NULL OR NOT (candidate_held_s > 0)
      THEN 'unattributed'
    WHEN candidate_outcome = 'expired' THEN 'host_offline_hold_expired'
    WHEN candidate_outcome = 'host_back_without_runner' THEN 'host_returned_without_runner'
    ELSE 'unexpected_hold_outcome'
  END AS classification
FROM matched
ORDER BY failure_time, failure_log_id;
```

This counts canonical failure events, not distinct sessions or the KPI's
numerator. Apply the dashboard's harness and session eligibility rules
separately. Track rows missing log IDs using the recovery guide's coverage
queries. Keep an absolute report end so later data does not change the cohort
between comparisons.

To recover the earlier close detail, find the hold-start event for the same
workspace, session, hostname, request ID, and runner. Check that the duration
to the matched end agrees with `held_s`, then inspect that runner's tunnel
close before the hold started. Use connection identity where available and
retain ambiguity when multiple connections match. Widening every failure's
close window to 24 hours can incorrectly select a different outage.

## What the evidence establishes

| Observation | Interpretation |
| --- | --- |
| Hold ends with `runner_reconnected` | The hold avoided a failure; reconnection alone does not prove turn completion. |
| Hold ends with `expired` and a matching failure | The host remained unavailable through the hold deadline. |
| Hold ends with `host_back_without_runner` and a matching failure | The host returned, but the original runner did not return within its grace. |
| Earlier close code 1000 | The WebSocket closed cleanly; this does not establish that the user's work completed or that shutdown was deliberate. |
| No matching hold evidence | Leave the failure unattributed; older builds and incomplete logs can omit evidence. |

Sleep, network loss, host restart, and intentional shutdown need their own
evidence. A hold outcome alone cannot distinguish them. Keep these failures
eligible for investigation until an exact turn outcome or deliberate-stop
record establishes that no work was lost. A category correction should not
silently become a KPI exclusion.

## Verification

Run the query in an approved SQL worksheet with an absolute UTC interval and
the debug-log table and workspace parameters. For a matched failure, inspect
its `failure_log_id` and the hold-end row: their session, request, and hostname
must agree. An expired hold should show approximately 86,400 seconds; a host
return can end it sooner. Compare the generic category with all disconnect
failures for the same cohort and window. This guide does not modify the live
dashboard or runtime behavior.
