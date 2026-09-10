# Host connection diagnostics

Structured fields annotate the existing reconnect warnings and accepted-but-silent
connection errors. Retry policy, severity, and dashboard exclusions are unchanged.
Deploy the updated host before expecting these fields; historical logs are unchanged.

The debug-log sink stores `event_name` separately and serializes non-null
`attributes` values as strings. Booleans appear as `True` or `False`.

## Events and fields

- `host_tunnel_unresponsive`: the existing accepted-but-silent connection error.
- `host_tunnel_reconnecting`: the existing reconnect warning.
- `host_tunnel_responsive`: one informational event when an application frame
  arrives after a silent-connection streak. This proves a response, not that the
  session or backend is otherwise healthy.

Use `host_id`, `host_process_id`, `connection_attempt`, `connection_phase`, and
`connection_elapsed_ms` to distinguish upgrade, owner lookup, hello transmission,
runner-exit reporting, and receive-loop failures. Attempt numbers are local to
one host process; include timestamps and process identity across restarts.

`upgrade_accepted`, `hello_sent`, `frame_received`, `exception_type`,
`disconnect_reason`, `websocket_close_code`, and `http_status` describe the last
attempt. `consecutive_silent_connections` and `reconnect_delay_s` explain retry
cadence. `tracked_runner_count` is not a count of failed or healthy sessions.

The new attributes contain no URLs, headers, credentials, or peer-provided text.
Existing human-readable error messages remain unchanged.

## Verification

```sh
uv run --no-sync pytest -q tests/host/test_connect.py -k 'silent_connect or silent_connection_diagnostics or inbound_frame_resets or suspend'
```

For a naturally occurring reconnect, inspect its `event_name` and `attributes`
in the debug-log table. Group by host/process and attempt; check whether the
connection got through upgrade and hello transmission, its close/status code,
and the selected retry delay. Look for `host_tunnel_responsive` to confirm a
subsequent response. Do not interrupt a shared endpoint to test these logs;
the unit tests exercise the failure paths with simulated connections.
