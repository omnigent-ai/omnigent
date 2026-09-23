# Native harness diagnostics

`OMNIGENT_HARNESS_STDERR_ENABLED=1` enables native diagnostic text in Omnigent
logs. The flag defaults off. `true`, `yes`, and `on` also enable capture; unset,
`0`, or other values disable it. Set it in the environment that launches the
host or CLI; the host forwards it to runners. Existing processes retain their
launch environment, so start a fresh host and session after changing it.

| Harness | Diagnostic source | When exported |
| --- | --- | --- |
| Codex native | App-server stderr pipe; bounded terminal output on startup exit | Stderr continuously throughout the app-server lifetime; terminal evidence before startup cleanup |
| Claude native | An Omnigent-owned Claude `--debug-file` | Continuously while the transcript forwarder runs, plus a bounded final drain on shutdown or launch failure |

The flag is shared across harnesses and lifecycle phases. Codex captures the
app-server's stderr, including startup and runtime diagnostics. A separate
startup-exit path captures the TUI's recent terminal output, because argument
parsing can fail before native logging initializes. Neither path reads Codex's
private log files. Claude's debug channel does
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
does not enable an exporter or select a destination. When Codex capture is
enabled, batched INFO diagnostics replace the per-line DEBUG stderr path so
logging handlers cannot block the pipe reader. With capture disabled, that
DEBUG path is unchanged. Earlier readiness-error reporting is also unchanged.

Shared credential-pattern improvements also affect ordinary logs with capture
disabled: assignment labels can contain spaces, and assigned values can include
a `Bearer` prefix. For example, `api key: is missing` now becomes
`api key: [REDACTED] missing` because the colon indicates an assignment. Only
whitespace-delimited label/value matching without `:` or `=` is confined to
diagnostic sanitization.

## Codex continuous diagnostics

Enabled capture also configures the app-server's `RUST_LOG` filter. Without an
explicit filter, Codex can retain useful diagnostics in its native local store
while its stderr contains only the listener banner. Omnigent selects warnings
plus targeted HTTP, engine-client, MCP, and tool-timing diagnostics:

```text
warn,codex_core::client=info,codex_core::tools::parallel=debug,codex_core::mcp=info,codex_http_client=debug,codex_client::default_client=debug,codex_mcp_client=info,codex_code_mode::timing=debug
```

An explicit subprocess `RUST_LOG` takes precedence, then a runner/CLI environment
value, including an empty filter or `off`. For a host-managed session, add
`RUST_LOG` to `OMNIGENT_RUNNER_ENV_PASSTHROUGH` if you want to forward a custom
host filter to the runner. Disabled capture does not inject or change the filter.
Start a fresh host/session after changing its launch environment.

The default avoids broad core/protocol DEBUG filters, which can include prompts,
tool payloads, and large amounts of span activity. It is not a content-security
boundary: warnings and request context can still contain sensitive data, and
redaction remains pattern-based. Module names and available detail depend on the
installed Codex version. User-selected filters can increase volume or suppress
otherwise useful diagnostics.

The HTTP targets cover both older `codex_client::default_client` and newer
`codex_http_client` implementations; they do not enable transport-level body
tracing. The native-binary regression is exercised against Codex 0.139.0 and
0.152.1.

This uses [Codex's supported `RUST_LOG` control](https://learn.chatgpt.com/docs/config-file/environment-variables#diagnostics)
for native stderr logging, not a reader for Codex's private
`logs_2.sqlite` schema. No `log_dir` override or plaintext TUI file is needed, and
the remote TUI's logging configuration is left unchanged.

Both CLI and host-managed launches drain app-server stderr for the entire
process lifetime. With capture enabled, completed records are handed to a
background thread that sanitizes and emits them at INFO about every 250 ms,
independently of thread discovery, prompt delivery, or transcript forwarding.
Events use `event_name=harness_diagnostic_output`, `harness=codex-native`, and
`source_kind=codex_app_server_stderr`.

Each event uses the current session ID from the bridge state, falling back to
the owning launch session before bridge state exists. Child startup diagnostics
therefore retain the child's identity when it shares its parent's runner.
Identity is resolved when a batch is emitted: output buffered immediately before
a session switch such as `/clear` can be attributed to the replacement session.
Use `launch_id`, PID, and byte offset to follow the same process across a switch.

| Attribute | Meaning |
| --- | --- |
| `launch_id`, `app_server_pid` | Unique capture identifier and app-server PID |
| `offset` | Raw stderr bytes consumed through the submitted records, including omitted bytes |
| `text` | Recent redacted records, at most 65,536 UTF-8 bytes per event |
| `truncated` | Whether records or bytes were omitted |
| `lines_omitted`, `bytes_omitted` | Whole source records dropped by input/queue limits, plus omissions from the redacted export buffer |
| `tail_byte_limit` | Maximum exported text size, 65,536 bytes; metadata and the formatted message add to event size |

The pipe reader performs no export I/O or sanitization. Its handoff queue holds
at most 1 MiB and 256 complete records, dropping the oldest records under
pressure. The worker may also hold one batch while delivering it. A source
record over 1 MiB, including its newline when present, is omitted whole, then
capture resumes at the next newline. Known credential redaction runs on complete
assembled records before export clipping. Partial records remain buffered until
a newline or reader termination; EOF or cancellation submits the final fragment.

Shutdown allows up to one second for buffered pipe output to reach EOF before
cancelling the reader, then up to one second for the worker's final batch. These
waits yield the event loop. A blocked handler cannot hold up the subprocess pipe
or force app-server teardown to wait indefinitely. Capture is best effort:
logger failures discard the affected batch, and an exporter that remains stuck
through shutdown can lose the final diagnostics. With capture disabled, no
collector thread or queue is created. The existing in-memory startup snapshot remains available in either
mode, with its text export controlled by the flag.

If capture initialization fails, the reader keeps draining and retaining its
startup snapshot without falling back to per-line DEBUG logging. It attempts
one background `harness_diagnostic_capture_failed` warning containing only the
exception class, session ID, and PID. If no reporting thread can start, the
exception class remains available as `stderr_capture_error_type` in the startup
failure snapshot; warning delivery is best effort.

## Codex terminal launch and exit

Every runner-owned Codex TUI launch logs one `codex_terminal_launch` event with
the launched `command`, the probed `codex_cli_version`, whether the launch is a
`resume`, and the resolved `args` after the host's `harness.codex-native.args`
are merged. Values are redacted before logging: `NAME=value` environment
assignments and every `-c` override outside the permission and model keys are
masked, and URLs lose their userinfo and query string.

The TUI's private tmux server keeps its pane after the process exits
(`keep_alive_after_exit`), so once the terminal has started, its exit is
captured by the watcher's pane-dead path instead of collapsing into a bare "no
server running" probe failure. The Codex terminal's `terminal_exit_observed`
event then records the inner exit status (`terminal_exit_status`) and a bounded,
redacted excerpt of the pane's final screen (`terminal_last_output`, stripped of
terminal control sequences and known credential patterns). This excerpt is
scoped to the Codex terminal — other terminals keep the guarantee that
lifecycle-event attributes carry no pane contents (see
`terminal-lifecycle-diagnostics.md`). Codex's exit is auxiliary, so its
`last_output` never reaches the terminal failure display (that path is
required-terminal only); the event excerpt is its durable record.

Liveness probes preserve the exit status and output before marking a pane
stopped, so they cannot silence the exit watcher. If tmux reports a dead pane
without a wait status, a bounded refresh nudges its private server to reap the
child; signal-only or unavailable statuses remain unknown. A terminal that exits
before registration also emits `terminal_exit_observed`, with
`before_observation=True`. These early records always include available exit
metadata; only opted-in Codex launches include a sanitized recent-output tail.
They do not publish lifecycle changes for a resource that was never observed.

The new startup paths capture up to 100 rows of recent scrollback plus the visible
screen, so an argument parser's first error line is not lost when usage text
scrolls it away. A possibly incomplete first joined record is discarded when
history could have lost its prefix; unknown capture metadata suppresses the new
tail. Export sanitizes the captured text before trimming to
the last 40 lines and 4,000 characters, plus an omission notice. Existing
registered Codex `terminal_exit_observed` records retain their screen-only
excerpt independently of this flag; the flag is not a master content switch for
ordinary lifecycle logs. Other terminals still export no pane text in these
event attributes.

## Codex startup failure snapshot

When a fresh native Codex session's TUI exits, thread discovery times out, or
the event stream ends before the first thread arrives, the runner emits
`event_name=codex_thread_start_failed`. Discovery races the exact launched
terminal's exit, including during an unbounded sign-in wait; it does not wait
for a timeout or follow a replacement terminal. The record uses the actual
session ID, including for a child sharing its parent's runner.

The snapshot is taken before cleanup closes the app-server. It is available at
ERROR level without enabling DEBUG logging. Process and reader status are always
included; with the flag disabled, the event omits stderr text and tail metadata.

### Attributes

| Field | Meaning |
| --- | --- |
| `harness`, `phase` | `codex-native`, `thread_discovery` |
| `reason` | `terminal_exited`, `timeout`, or `event_stream_ended` |
| `terminal_instance_id`, `terminal_exit_status` | Exact launched terminal and exit status, when terminal exit ended discovery |
| `terminal_last_output` | With capture enabled, bounded sanitized recent terminal output when exit ended discovery |
| `timeout_s`, `elapsed_ms` | Configured wait budget and observed discovery duration; the budget is absent for an unbounded sign-in wait |
| `login_required` | Whether startup was waiting for interactive sign-in |
| `app_server_state` | `unavailable`, `not_started`, `running`, or `exited` |
| `app_server_pid`, `app_server_returncode` | Process identity and observed exit status, when known |
| `codex_version` | Previously probed app-server CLI version, when known |
| `stderr_reader_state` | `unavailable`, `not_started`, `running`, `cancelled`, `failed`, or `completed` |
| `stderr_reader_error_type`, `stderr_reader_cause_type` | Exception and immediate cause/context classes when the reader failed; no exception payload |
| `stderr_capture_enabled` | Whether stderr text capture was explicitly enabled |
| `stderr_capture_error_type` | Exception class when continuous capture initialization failed; no exception payload |
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

The same terminal-exit cause and opted-in excerpt are retained in the bridge's
startup-error marker for chat execution. A late failure cannot overwrite a
replacement launch's marker or close its app-server. Successful discovery and
cancellation do not emit this failure event; a discovered thread remains usable
through its app-server even if the auxiliary TUI exits simultaneously.

This event covers fresh-thread discovery. A terminal that dies before
registration uses the `terminal_exit_observed` path described above, since no
discovery task exists yet. Earlier app-server process-launch failures, resume
failures, and errors after a thread starts keep their existing logging.

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
uv run --no-sync pytest -q tests/test_codex_native_continuous_diagnostics.py tests/test_codex_native_app_server_stderr.py
uv run --no-sync pytest -q tests/test_codex_native_logging_env.py
uv run --no-sync pytest -q tests/inner/test_terminal.py tests/runner/test_terminal_startup_exit.py
uv run --no-sync pytest -q tests/e2e/test_codex_continuous_diagnostics_e2e.py
uv run --no-sync pytest -q tests/e2e/test_codex_native_runtime_diagnostics_e2e.py
uv run --no-sync pytest -q tests/test_claude_native_diagnostics.py tests/test_claude_native_diagnostics_integration.py
```

These tests inject a startup timeout and an ended event stream, inspect the
serialized debug-log row, and verify the process snapshot precedes teardown.
They also check disabled capture, host-to-runner environment forwarding, local
log output, child attribution, credential redaction, UTF-8 byte limits, and
unchanged success/cancellation behavior.

Continuous Codex tests check startup and runtime delivery before EOF, session
switches, final fragments, record and queue bounds, and recovery after logger
failure. A real subprocess floods stderr while its exporter is blocked to verify
that pipe draining and shutdown remain responsive.
The credential-free e2e test uses an isolated runner and synthetic Codex
subprocess to verify delivery to the real local log and structured-log handler.
The native runtime e2e test additionally uses an installed Codex CLI (or
`OMNIGENT_CODEX_PATH`): it executes a real shell command, directs a model request
at an unreachable loopback endpoint, and verifies engine HTTP diagnostics reach
the structured sink before exit. It needs no credentials or external model
service and checks the disabled control too. A forced runner-timeout case
verifies that the native subprocess does not survive test cleanup.

Claude tests exercise both launch paths, explicit debug-file preservation,
opt-out without file access, rotation, partial records, bounded shutdown,
session attribution, and continued collection while forwarding is blocked.

After installing the updated runtime, start a fresh host and Claude native
session with `OMNIGENT_HARNESS_STDERR_ENABLED=1`. Run a normal prompt and filter
local logs or the configured debug-log sink by the exact session ID and
`event_name = 'harness_diagnostic_output'`. Confirm `harness = 'claude-native'`,
`source_kind = 'claude_debug_log'`, and diagnostic text. A fresh session with the
flag set to `0` should receive no injected debug-file argument or these events.

For a fresh Codex native session with the flag enabled, use the same event filter
and confirm `harness = 'codex-native'` and
`source_kind = 'codex_app_server_stderr'`. Runtime stderr should appear while
the session remains open, without requiring a startup timeout. A quiet stderr
pipe produces no events. Setting the flag to `0` disables these INFO events.

To check early failure, start a fresh opted-in session with
`omnigent codex --omni-telemetry-invalid-flag`. Inspect that session's
`codex_thread_start_failed` or `terminal_exit_observed` records: expect exit
status `2` and `unexpected argument` in `terminal_last_output`. A discovery
failure should have `reason=terminal_exited`, not `timeout`. Repeat with capture
disabled: the discovery snapshot and pre-observation exit record retain metadata
but omit terminal text. The CLI's separate terminal-readiness wait is unchanged.

After deploying the runner, filter the debug-log table by the incident time
window, exact session ID, and `event_name = 'codex_thread_start_failed'`.
Confirm `stderr_capture_enabled = 'True'` for an opted-in runner and compare
the process and reader states with its tail. With the flag unset or `0`, confirm
`stderr_capture_enabled = 'False'` and no `stderr_tail` attribute. A row with
`stderr_reader_state = 'failed'` and `stderr_reader_error_type = 'ValueError'`
distinguishes a failed drain from a live reader with an otherwise stalled
startup. Use the return code and adjacent lifecycle events to interpret it.
