# Terminal lifecycle diagnostics

Structured fields distinguish terminal disappearance from session failure.
Error severity, terminal lifecycle behavior, and dashboard exclusions are
unchanged, except that a person's own quit (see
`native_terminal_exit_classified`) settles the session idle instead of failing
it. Deploy the updated runner before expecting these fields.

The debug-log sink stores `event_name` separately and serializes non-null
`attributes` values as strings. Booleans appear as `True` or `False`.

## Events and correlation

- `terminal_probe_failed`: the existing capture-pane warning.
- `terminal_unavailable`: the existing three-probe error.
- `terminal_exit_observed`: an informational lifecycle record with the owning
  `session_id`, `terminal_lifecycle` (`required` or `auxiliary`),
  `session_status_before_exit` (`idle`, `running`, or `unknown`),
  `terminal_exit_status` when known, `terminal_input_ready_at` once the native
  TUI had accepted input, and `superseded`.
- `native_terminal_exit_classified`: an informational record for each required
  terminal exit with `harness`, `command` (basename), `exit_status`, `signal`,
  `session_end_reason`, `session_end_signal`, `session_end_evidence` (whether
  Claude's `SessionEnd` hook fired), `banner_seen`, `interactive`, `decision`,
  and `rule`. `voluntary` means the person quit the agent themselves: the
  session goes idle, no conversation item is written (an `error`-type item
  would be counted as a failure), and the next message resumes it. `failed`
  keeps the `required_terminal_exited` failure.
  - A Claude or Pi exit is voluntary only after the TUI accepted input and
    without a non-zero status, a fatal signal, or a SIGTERM/SIGQUIT reported by
    Claude's hook. The rule is `session_end_reason` (`prompt_input_exit`,
    `logout`, `clear`), `user_signal`, or `exit_zero`; Claude's exit banner
    also counts, even before readiness.
  - `user_signal` is SIGINT/SIGHUP. Pi dies by the signal; Claude handles
    SIGHUP by exiting 129, which is a non-zero exit and stays a failure.
  - `exit_zero` alone is weak evidence: a launcher wrapper can exit 0 over a
    crashed agent, so compare `command` and `session_end_evidence`. A tracked
    sub-agent is settled `cancelled` on strong evidence (banner, reason, user
    signal) and `failed` on a bare zero exit, so its parent retries.
  - The record carries no pane text.
- `harness_stream_ended_by_voluntary_exit`: a turn stream was still open when
  the person quit, so the release severed it. The turn ends with
  `response.cancelled` instead of `response.failed`
  (`harness_stream_ended_by_terminal_exit`, the failure case), and the
  sub-agent dispatch, if any, is settled as above.
- `terminal_close_requested`: an informational record before the explicit
  terminal-close path runs. It does not assert who requested the close or that
  cleanup succeeded.

Join records by `attributes['terminal_instance_id']`. The identifier is unique
to a terminal instance, so a replacement with the same name/key is distinguishable.
Watcher logs may inherit a runner's primary session; the lifecycle record carries
the actual owning session, including subagents.

The probe error includes `consecutive_probe_failures`, `last_capture_age_ms`,
`pane_output_seen`, `keep_alive_after_exit`, and `shutdown_requested`.
An absent `terminal_exit_status` means unknown, not success. An auxiliary terminal
exit does not by itself prove an agent failure. A required terminal exiting while
idle differs from losing it during a turn. Check the subsequent turn-status
event for the actual user-visible outcome.

Lifecycle metadata excludes command arguments and filesystem paths. Codex
terminal-text excerpts can include either, along with other user content.
Pane text is limited to Codex `terminal_exit_observed` records: registered
terminals export `terminal_last_output`, a bounded excerpt of the final screen
with terminal control sequences stripped and known credential patterns redacted.
This existing excerpt is exported independently of
`OMNIGENT_HARNESS_STDERR_ENABLED`.

A terminal that exits before registration also emits `terminal_exit_observed`,
with `before_observation=True`. Available exit metadata is always included;
only Codex with the flag enabled includes a sanitized recent-output tail. Other
terminals' lifecycle-event attributes contain no pane text.

Exit handling can retain recent scrollback in memory before tmux cleanup, even
with the flag disabled. The flag gates the new diagnostic consumers' reads and
exports of this history, including Codex startup-error excerpts; it is not a
switch for all terminal text retention or existing logging. See
[Native harness diagnostics](harness-diagnostics.md) for the capture bounds,
startup-failure fields, and app-server logging controls.

## Verification

```sh
uv run --no-sync pytest -q tests/inner/test_terminal.py tests/runner/test_resource_registry.py
```

Run `omnidev` and explicitly close a disposable terminal. Verify that
`terminal_close_requested` carries its session and terminal-instance identifiers
in the debug-log table. For a naturally observed terminal disappearance, use the
instance identifier to find the probe warning and lifecycle record. Compare
required/auxiliary and prior session status against the subsequent turn outcome.
