# Native harness diagnostics

`OMNIGENT_HARNESS_STDERR_ENABLED=1` enables native diagnostic text in Omnigent
logs. The flag defaults off. `true`, `yes`, and `on` also enable capture; unset,
`0`, or other values disable it. Set it in the environment that launches the
host or CLI; the host forwards it to runners. Existing processes retain their
launch environment, so start a fresh host and session after changing it.

| Harness | Diagnostic source | When exported |
| --- | --- | --- |
| Codex native | In-memory app-server stderr | First-thread discovery failure, before process cleanup |
| Claude native | An Omnigent-owned Claude `--debug-file` | Continuously while the transcript forwarder runs, plus a bounded final drain on shutdown or launch failure |

The flag is shared across harnesses and lifecycle phases. Codex runtime-failure
and continuous stderr export are not implemented. Claude's debug channel does
not capture arbitrary child-process stderr, terminal screen contents, or errors
that occur before Claude initializes its logger. The terminal's file descriptors
remain unchanged.

## Content and destinations

Enabled capture retains diagnostic text, including tracebacks and request or
response context. It applies known credential-pattern redaction, including
whitespace-delimited values after explicit credential labels, normalizes carriage
returns to newlines, and removes other terminal control codes. It does not filter
prompts or payloads by content; configure capture only where this text is
appropriate for the deployment's log storage and readers.

Redaction is pattern-based, not a general detector of secrets in prose. For
example, `password hunter2` is redacted, but `The password is hunter2` becomes
`The password [REDACTED] hunter2`: the actual value remains. Treat exported
diagnostics as potentially sensitive even after redaction.

The captured text appears in the owning process's ordinary local logs, including
runner logs under `~/.omnigent/logs/runner/`, and in structured event attributes.
It also reaches any configured debug-log or OpenTelemetry exporter. This flag
does not enable an exporter or select a destination. It also does not control
existing DEBUG stderr logging or earlier readiness-error reporting.

Shared credential-pattern improvements also affect ordinary logs with capture
disabled: assignment labels can contain spaces, and assigned values can include
a `Bearer` prefix. For example, `api key: is missing` now becomes
`api key: [REDACTED] missing` because the colon indicates an assignment. Only
whitespace-delimited label/value matching without `:` or `=` is confined to
diagnostic sanitization.

## Codex startup failure snapshot

When a fresh native Codex session times out waiting for its first thread, or
the event stream ends before that thread arrives, the runner emits the existing
error with `event_name=codex_thread_start_failed`. The record uses the actual
session ID, including for a child sharing its parent's runner.

The snapshot is taken before cleanup closes the app-server. It is available at
ERROR level without enabling DEBUG logging. Process and reader status are always
included; with the flag disabled, the event omits stderr text and tail metadata.

### Attributes

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

## Claude continuous diagnostics

Both CLI and host-managed native launches add `--debug-file` pointing to a fresh
owner-only file inside the session bridge directory. An explicit user-supplied
`--debug-file` is preserved and its file is not collected. When disabled, this
collector creates no file and does not read diagnostics or marker metadata.

A separate task follows the owned file even while transcript discovery or HTTP
forwarding is stalled. File reads, redaction, and final draining run in worker
threads so diagnostic work does not block the forwarding event loop. Completed
records emit INFO events with
`event_name=harness_diagnostic_output`, `harness=claude-native`, and
`source_kind=claude_debug_log`. Each event uses the current active session ID,
including after a session switch. The diagnostic text can include prompt-hook
prompts, scripts, request/response context, and response fragments. The source
file contains Claude's original output; redaction applies to exported records.

| Attribute | Meaning |
| --- | --- |
| `launch_id` | Unique identifier for the owned diagnostic file |
| `offset` | Byte offset consumed from the current file |
| `text` | Recent redacted records, at most 65,536 UTF-8 bytes per event |
| `truncated` | Whether records or bytes were omitted |
| `lines_omitted`, `bytes_omitted` | Known omitted records and bytes for this event; unread backlog has a byte count without a complete line count |
| `tail_byte_limit` | Maximum exported text size, 65,536 bytes; metadata and the formatted log message add to the overall event size |

Each poll reads at most 64 KiB. Partial records are buffered until a newline;
records over 1 MiB are omitted with counts, then collection resumes at the next
newline. Known credential redaction runs on assembled records before export
clipping. Rotation drains the old inode before following the replacement;
Claude normally rotates by renaming the file. Truncation is detected only when
the observed file size falls below the read offset, which resets the offset and
discards any buffered partial record. If the same inode is truncated and regrows
to the offset or beyond between polls, the truncation is not detected: new bytes
before the offset can be skipped, and a buffered old partial record can be joined
with new output.

Reattaching a forwarder starts reading the owned file from the beginning and can
repeat earlier diagnostics. If a rotated predecessor already exists on
attachment, its size is reported as omitted bytes without replaying it. Counts
cannot reconstruct files already removed by Claude.

Shutdown, cancellation, and terminal-launch failure drain at most 256 KiB.
Shutdown lets an in-flight poll finish before closing and draining the file.
Shutdown prioritizes an already-rotated replacement and reports skipped backlog.
A final partial record is exported only at the observed end of the file;
remaining unread bytes are counted as omitted. File or logger failures do not
replace the original session error.

Terminal launch failures are logged before their final diagnostic drain. If the
caller cancels during that drain, cancellation still propagates and the worker
can finish draining, but the original launch failure is already in the logs.

Claude owns disk rotation. In Claude 2.1.277, the custom debug file rotates near
10 MiB to `<filename>.1`, retaining one predecessor (roughly 20 MiB total, with
possible append overshoot). This is CLI-version-dependent, not an Omnigent disk
quota. The next enabled launch in the same bridge directory removes the prior
owned file and its rotated predecessor. Other user debug files are never read
or deleted.

## Verification

```sh
uv run --no-sync pytest -q tests/test_codex_native_diagnostics.py tests/runner/test_codex_startup_telemetry.py tests/host/test_connect.py -k 'codex or harness_stderr'
uv run --no-sync pytest -q tests/test_claude_native_diagnostics.py tests/test_claude_native_diagnostics_integration.py
```

These tests inject a startup timeout and an ended event stream, inspect the
serialized debug-log row, and verify the process snapshot precedes teardown.
They also check disabled capture, host-to-runner environment forwarding, local
log output, child attribution, credential redaction, UTF-8 byte limits, and
unchanged success/cancellation behavior.

Claude tests exercise both launch paths, explicit debug-file preservation,
opt-out without file access, rotation, partial records, bounded shutdown,
session attribution, and continued collection while forwarding is blocked.

After installing the updated runtime, start a fresh host and Claude native
session with `OMNIGENT_HARNESS_STDERR_ENABLED=1`. Run a normal prompt and filter
local logs or the configured debug-log sink by the exact session ID and
`event_name = 'harness_diagnostic_output'`. Confirm `harness = 'claude-native'`,
`source_kind = 'claude_debug_log'`, and diagnostic text. A fresh session with the
flag set to `0` should receive no injected debug-file argument or these events.

After deploying the runner, filter the debug-log table by the incident time
window, exact session ID, and `event_name = 'codex_thread_start_failed'`.
Confirm `stderr_capture_enabled = 'True'` for an opted-in runner and compare
the process and reader states with its tail. With the flag unset or `0`, confirm
`stderr_capture_enabled = 'False'` and no `stderr_tail` attribute. A row with
`stderr_reader_state = 'failed'` and `stderr_reader_error_type = 'ValueError'`
distinguishes a failed drain from a live reader with an otherwise stalled
startup. Use the return code and adjacent lifecycle events to interpret it.
