# Codex startup diagnostics

When a fresh native Codex session times out waiting for its first thread, or
the event stream ends before that thread arrives, the runner emits the existing
error with `event_name=codex_thread_start_failed`. The record uses the actual
session ID, including for a child sharing its parent's runner.

The snapshot is taken before cleanup closes the app-server. It is available at
ERROR level without enabling DEBUG logging. Deploy the updated runner to collect
these fields for subsequent attempts.

## Attributes

| Field | Meaning |
| --- | --- |
| `harness`, `phase` | `codex-native`, `thread_discovery` |
| `reason` | `timeout` or `event_stream_ended` |
| `timeout_s`, `elapsed_ms` | Configured wait budget and observed discovery duration; the budget is absent for an unbounded sign-in wait |
| `login_required` | Whether startup was waiting for interactive sign-in |
| `app_server_state` | `unavailable`, `not_started`, `running`, or `exited` |
| `app_server_pid`, `app_server_returncode` | Process identity and observed exit status, when known |
| `codex_version` | Previously probed app-server CLI version, when known |
| `stderr_reader_state` | `unavailable`, `not_started`, `running`, `cancelled`, `failed`, or `completed` |
| `stderr_reader_error_type`, `stderr_reader_cause_type` | Exception and immediate cause/context classes when the reader failed; no exception payload |
| `stderr_tail_available` | Whether an in-memory stderr buffer exists |
| `stderr_tail` | At most 4,096 characters of retained, sanitized startup diagnostics |
| `stderr_tail_truncated`, `stderr_lines_omitted` | Whether text was shortened and how many entries were withheld |
| `diagnostics_error_type` | Snapshot collection failed; the original startup failure and cleanup still proceed |

The debug-log sink serializes non-null attribute values as strings. Booleans
appear as `True` or `False`. A missing exit status is unknown; it does not mean
the process exited successfully. A failed stderr reader can explain a blocked
app-server, but the reader exception alone does not prove pipe backpressure.

The collector uses completed stderr lines already retained in memory. An empty
tail does not prove that the process wrote no stderr: an unterminated line may
still be in the reader, and payload-like or oversized entries are withheld.
Credentials and URL authentication material are redacted before output is
bounded. Request/response payloads, prompts, and header dumps are withheld.
The collector performs no filesystem reads, subprocess probes, or network calls.

This event covers fresh-thread discovery. Earlier process launch failures,
resume failures, and errors after a thread starts keep their existing logging.
Successful discovery and cancellation do not emit this failure event.

## Verification

```sh
uv run --no-sync pytest -q tests/test_codex_native_diagnostics.py tests/runner/test_codex_startup_telemetry.py
```

These tests inject a startup timeout and an ended event stream, inspect the
serialized debug-log row, and verify the process snapshot precedes teardown.
They also check child attribution, redaction, output limits, and unchanged
success/cancellation behavior.

After deploying the runner, filter the debug-log table by the incident time
window, exact session ID, and `event_name = 'codex_thread_start_failed'`.
Compare the process and reader states with the sanitized tail. A row with
`stderr_reader_state = 'failed'` and `stderr_reader_error_type = 'ValueError'`
distinguishes a failed drain from a live reader with an otherwise stalled
startup. Use the return code and adjacent lifecycle events to interpret it.
