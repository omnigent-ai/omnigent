# Codex startup diagnostics

When a fresh native Codex session times out waiting for its first thread, or
the event stream ends before that thread arrives, the runner emits the existing
error with `event_name=codex_thread_start_failed`. The record uses the actual
session ID, including for a child sharing its parent's runner.

The snapshot is taken before cleanup closes the app-server. It is available at
ERROR level without enabling DEBUG logging. Deploy the updated runner to collect
these fields for subsequent attempts.

## Opt-in stderr capture

Process and reader status are always included. Stderr text is disabled by
default. Set `OMNIGENT_STARTUP_STDERR_ENABLED=1` in the environment that
launches the host or runner to include its completed stderr buffer in failure
logs. `true`, `yes`, and `on` also enable capture; unset, `0`, or other values
disable it. The host forwards this setting to its runners. Existing hosts and
runners retain their launch environment, so restart them for a setting change
to take effect.

The flag is shared across harnesses; native Codex is currently the first
consumer. Other harnesses can use the shared setting when they add startup
stderr diagnostics.

Enabled capture retains diagnostic text, including tracebacks and request or
response context. It applies the same known credential-pattern redaction as
other Omnigent process logs and removes terminal control codes. It does not
filter prompts or payloads by content; configure capture only where this text
is appropriate for the deployment's log storage and readers.

The captured text appears in ordinary local runner logs (by default under
`~/.omnigent/logs/runner/`) and the structured event's `stderr_tail` attribute.
It also reaches any configured debug-log or OpenTelemetry exporter. This flag
does not enable an exporter or select a destination. With the flag disabled,
this failure event omits stderr text and tail metadata. Existing DEBUG stderr
logging and earlier readiness-error reporting retain their existing behavior.

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
| `stderr_capture_enabled` | Whether stderr text capture was explicitly enabled |
| `stderr_tail_available` | With capture enabled, whether an in-memory stderr buffer exists |
| `stderr_tail` | With capture enabled, at most 65,536 UTF-8 bytes of recent stderr |
| `stderr_tail_truncated` | Whether the size limit shortened the captured text |
| `stderr_lines_omitted`, `stderr_bytes_omitted` | Captured entries omitted whole and bytes omitted from the redacted buffer by the size limit |
| `diagnostics_error_type` | Snapshot collection failed; the original startup failure and cleanup still proceed |

The debug-log sink serializes non-null attribute values as strings. Booleans
appear as `True` or `False`. A missing exit status is unknown; it does not mean
the process exited successfully. A failed stderr reader can explain a blocked
app-server, but the reader exception alone does not prove pipe backpressure.

The collector uses completed stderr lines already retained in memory. An empty
tail does not prove that the process wrote no stderr: an unterminated line may
still be in the reader. It retains a contiguous tail of complete entries within
64 KiB, including newline separators. If the newest entry alone exceeds that
budget, it retains the end of that entry at a valid UTF-8 boundary. Redaction
precedes any clipping. Omission counters cover this snapshot only, excluding
earlier buffer eviction or clipping by the stderr reader.
The collector performs no filesystem reads, subprocess probes, or network calls.

This event covers fresh-thread discovery. Earlier process launch failures,
resume failures, and errors after a thread starts keep their existing logging.
Successful discovery and cancellation do not emit this failure event.

## Verification

```sh
uv run --no-sync pytest -q tests/test_codex_native_diagnostics.py tests/runner/test_codex_startup_telemetry.py tests/host/test_connect.py -k 'codex or startup_stderr'
```

These tests inject a startup timeout and an ended event stream, inspect the
serialized debug-log row, and verify the process snapshot precedes teardown.
They also check disabled capture, host-to-runner environment forwarding, local
log output, child attribution, credential redaction, UTF-8 byte limits, and
unchanged success/cancellation behavior.

After deploying the runner, filter the debug-log table by the incident time
window, exact session ID, and `event_name = 'codex_thread_start_failed'`.
Confirm `stderr_capture_enabled = 'True'` for an opted-in runner and compare
the process and reader states with its tail. With the flag unset or `0`, confirm
`stderr_capture_enabled = 'False'` and no `stderr_tail` attribute. A row with
`stderr_reader_state = 'failed'` and `stderr_reader_error_type = 'ValueError'`
distinguishes a failed drain from a live reader with an otherwise stalled
startup. Use the return code and adjacent lifecycle events to interpret it.
