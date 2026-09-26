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
  `delay_s`, `retry_in_s`.
- `runner_session_initialized`: `recovery_turn` (`history_resume`,
  `recovery_prompt` or `none`) with its inputs `recovery_id`,
  `resume_interrupted_turn`, `suppress_recovery_turn`, `execution_seen`,
  `history_len`, `last_item_type`, and the resulting `status`.

## Server events (`source = 'server'`)

- `runner_tunnel` with `phase` `connected`, `closed`, `disconnected` or
  `error`: `connection_id` from the runner's hello, `connection_age_s`,
  `last_frame_age_s`, `ended_by` (the helper tasks that had finished when the
  end was observed, comma-separated: `tunnel-receive` for a peer close,
  `tunnel-ping` for a server-declared timeout, both when the peer's close
  reply had already arrived), plus the close `code` and `reason` on
  `disconnected`. `closed` covers a server-initiated end the peer has not yet
  acknowledged, such as a ping timeout, which previously left no row.
- `runner_ping_timeout`: `runner_id`, `connection_id`, `connection_age_s`,
  `silent_s`.
- `runner_stream_transport_lost`: one row per outage when the relay starts
  holding a session's turn, with `grace_s`.
- `runner_stream_disconnected`: the relay's give-up row, with `decision`
  (`intentional_stop`, `server_shutdown`, `idle_no_failure` or
  `failed_mid_turn`), `grace_s`, `outage_s`, `retries`.
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
(`0.16.0.post1790000000+g1a2b3c4`, with `.dirty` when deploying a modified
tree under `--allow-dirty`). The stamp is written to the pyprojects and to
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
