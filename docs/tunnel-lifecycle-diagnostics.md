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
  (`intentional_stop`, `server_shutdown`, `idle_no_failure` or
  `failed_mid_turn`), `grace_s`, `outage_s`, `retries`. `outage_s` is the
  time since the current grace window opened; a reconnect that dropped again
  within the window does not reset it, so it includes that brief connected
  stretch and is not cumulative disconnected time.
- `runner_session_init_started`: `resume_interrupted_turn`,
  `suppress_recovery_turn`, `recovery_id`. Neither flag set is the tunnel
  reconnect hook; resume set is a sub-agent restore; suppress set is a
  message forward.

## Correlation

Join the runner's and server's rows for one socket on
`attributes['connection_id']`. A `runner_connected` row with `reconnect =
False` after earlier rows for the same `runner_id` is a new process; `pid`
confirms it. A repeating `connection_age_s` across drops points at an
intermediary timeout rather than either endpoint.

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
  tests/server/test_runner_session_init.py \
  tests/runner/test_suppress_recovery_turn.py \
  tests/deploy/test_databricks_deploy_version.py
```

Against a live server and runner with the debug sink configured: drop the
runner's socket, hold a reconnect past `RUNNER_DISCONNECT_GRACE_S`, kill the
runner process, and crash a harness mid-turn. One query on the session over
the events above, ordered by `client_time`, must tell the four apart and show
whether the original turn survived.
