# Tunnel lifecycle diagnostics

Structured fields that explain a runner tunnel drop end to end: what closed
the socket, how long the runner was gone, whether the server failed the turn,
and whether the reconnect restarted a turn. They ride the existing debug-log
sink as `event_name` plus string `attributes`; booleans appear as `True` or
`False`. Deploy both the server and the runner before expecting the fields on
both ends.

## Runner events (`source = 'runner'`)

- `runner_connected`: `connection_id`, `reconnect` (an earlier connection on
  this process was accepted), `attempt` (ordinal within the reconnect
  streak), `downtime_s` (gap since the previous connection ended), `pid`.
- `runner_tunnel_disconnected`: one row per attempt the runner retries,
  replacing the plain retry line. A fatal exit (persistent auth or protocol
  rejection, cancellation) raises out of the reconnect loop and is logged by
  the caller instead. `disconnect_reason` (the bounded classifier shared with
  the OTel counter; `local_shutdown` when the process is stopping, even if the
  close handshake broke), `close_code`, `close_reason`, `close_rcvd_code` and
  `close_sent_code` (which side sent a close frame; a 1006 has neither),
  `error_type`, `connected`, `connection_age_s`, `recycle`, `backoff_reset`,
  `delay_s`, `retry_in_s`. A clean 1000/1001 close ends the read loop without
  an exception, so its codes and reason come from the connection's own close
  frames.
- `runner_session_initialized`: `recovery_turn` (`history_resume`,
  `recovery_prompt` or `none`) with its inputs `recovery_id`,
  `resume_interrupted_turn`, `suppress_recovery_turn`, `execution_seen`,
  `history_len`, `last_item_type`, and the resulting `status`.

## Server events (`source = 'server'`)

- `runner_tunnel` with `phase` `connected`, `disconnected` or
  `error`: `connection_id` from the runner's hello, `connection_age_s`,
  `last_frame_age_s`, `ended_by` (the helper tasks that had finished when the
  end was observed, comma-separated: `tunnel-receive`, `tunnel-ping`, or
  `tunnel-sender`), plus the close `code` and `reason` on `disconnected`.
  When a helper reports a peer disconnect, the event preserves its observed
  code and reason. Otherwise it records the first server-requested close,
  including retirement, replacement, or ping timeout, without implying that
  the peer acknowledged it. Concurrent close requests can make the recorded
  code and reason differ from those sent on the socket. A stale receive or
  ping helper can end before the sender, so `ended_by` alone does not identify
  the close cause.

  During a rollout, queries should accept both `closed` (older servers) and
  `disconnected`, and allow missing close details on older `closed` rows.
  Update queries that select only `closed` to use `disconnected` after the
  server upgrade is complete.
- `runner_ping_timeout`: `runner_id`, `connection_id`, `connection_age_s`,
  `silent_s`.
- `runner_stream_transport_lost`: one row per outage when the relay first
  observes the loss, with `intentional_stop` and `grace_s`. An unintentional
  loss is then held for `grace_s`; an intentional stop goes straight to the
  give-up row.
- `runner_stream_disconnected`: the relay's give-up row, with `decision`
  (`intentional_stop`, `server_shutdown`, `live_elsewhere`, `idle_no_failure` or
  `failed_mid_turn`), `grace_s`, `outage_s`, `retries`. `outage_s` is the
  time since the current grace window opened; a reconnect that dropped again
  within the window does not reset it, so it includes that brief connected
  stretch and is not cumulative disconnected time.
- `runner_disconnect_decision`: a warning explaining the status check in the
  relay (`origin = runner_disconnected_mid_turn`) or offline sweep
  (`origin = runner_offline_sweep`). `decision` is `idle_no_failure`,
  `failed_mid_turn`, `failed_before_start`, or `intentional_stop`.
  Idle subsessions keep their status and emit no `session_turn_failed` event
  or error labels. Running and waiting sessions still fail on disconnect;
  `fail_idle_top_level` applies only to top-level startup failures.

  `status_source` is `cache`, `persisted`, `snapshot`, `relay_snapshot`, or
  `unknown`, alongside `cached_session_status`, `persisted_session_status`,
  `snapshot_session_status`, and `status_lookup` (`not_needed`, `found`,
  `missing`, or `error`). Both paths read a fresh row on a cache miss and
  recheck the cache after the read. A missing or failed read falls back to the
  sweep's snapshot (`snapshot`) or the known status retained when the relay
  adopted its runner binding (`relay_snapshot`). Without any known state,
  the disconnect still reports a failure.

  Adoption snapshots stay outside the live cache: an old saved status must
  not override a newer row written by another server. They belong to one
  relay binding and are discarded when it ends or is replaced. A quiet
  Claude subsession can emit only heartbeats after handoff, so retaining its
  saved idle state avoids a false failure if the later status lookup fails.
  A readable running/waiting row still takes precedence, including when
  that persisted state is stale; this fallback does not repair stale writes.

  The row includes the active `turn_id` and, when available, `session_kind`,
  `parent_session_id`, `runner_id`, `host_id`, and `conversation_updated_at`.
  The latter measures content activity, not the time of a status transition.
  A relay cache hit does not load conversation metadata solely for logging.
- `runner_session_init_started`: `resume_interrupted_turn`,
  `suppress_recovery_turn`, `recovery_id`. Neither flag set is the tunnel
  reconnect hook; resume set is a sub-agent restore; suppress set is a
  message forward.

## Correlation

The disconnect grace task rechecks the local tunnel after loading bound
sessions. A reconnect during that read logs `reconnected during offline
lookup; skipping offline-marking`; an older database snapshot must not turn
the live runner's sessions into disconnect failures.

Join the runner's and server's rows for one socket on
`attributes['connection_id']`. A `runner_connected` row with `reconnect =
False` after earlier rows for the same `runner_id` is a new process; `pid`
confirms it. A repeating `connection_age_s` across drops points at an
intermediary timeout rather than either endpoint.

## Credential recovery

`auth token refresh failed; falling back to previous token` describes a
failed renewal attempt, not the cause of the preceding socket close. Check
the exception type and subsequent handshake result: a rejected old bearer
can keep the runner disconnected even after network connectivity returns.

Delegated runner credentials and stored or refreshed OIDC logins do not
require the Databricks executor to import. The SDK path loads only when
those providers do not supply a token; an import failure there still permits
the existing managed-mint fallback. This does not repair an inconsistent
installation or provide a credential when every configured provider fails.

## Build identity

Databricks App deploys append the checked-out commit to the stamped version
(`0.16.0.post1790000000+g1a2b3c4`, with `.dirty` when the tree has
uncommitted or untracked non-ignored files, which only `--allow-dirty`
permits). The stamp is written to the pyprojects and to
`omnigent/version.py`, the constant the runtime imports, so `app_version` on
every row and `version` on the server's `runner_tunnel` connected row name
the build. The generated version is itself valid for
`--skip-build --version <version>`.

## Verification

```sh
uv run --no-sync pytest -q tests/runner/transports/ws_tunnel/test_serve.py \
  tests/runner/transports/ws_tunnel/test_frames.py \
  tests/server/integration/test_runner_tunnel_route.py \
  tests/server/routes/test_sessions_runner_relay.py \
  tests/server/integration/test_sessions_tunnel_three_layer.py \
  tests/server/routes/test_subagent_status.py \
  tests/server/test_runner_session_init.py \
  tests/runner/test_suppress_recovery_turn.py \
  tests/deploy/test_databricks_deploy_version.py
```

Against a live server and runner with the debug sink configured: drop the
runner's socket, hold a reconnect past `RUNNER_DISCONNECT_GRACE_S`, kill the
runner process, and crash a harness mid-turn. One query on the session over
the events above, ordered by `client_time`, must tell the four apart and show
whether the original turn survived.

For idle-child handling, let a Claude subsession become idle, then stop its
host without using the session's Stop action. After the disconnect grace,
the child should remain idle with a warning whose decision is
`idle_no_failure`, no disconnect error in its transcript, and no Failed
activity in its parent's transcript. Repeat with a running child to confirm
that interrupted work still produces `runner_disconnected`.

For handoff handling, reconnect an idle child's runner to a fresh server,
then drop the runner after its heartbeat-only relay is ready. In a test
environment, make the disconnect-time conversation lookup fail or return no
row. The warning should report `status_source = relay_snapshot` and
`decision = idle_no_failure`. Repeat after persisting a new running status:
the fresh row must win and the interruption must still fail.
