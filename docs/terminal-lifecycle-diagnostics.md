# Terminal lifecycle diagnostics

Structured fields distinguish terminal disappearance from session failure.
Error severity, terminal lifecycle behavior, and dashboard exclusions are
unchanged. Deploy the updated runner before expecting these fields.

The debug-log sink stores `event_name` separately and serializes non-null
`attributes` values as strings. Booleans appear as `True` or `False`.

## Events and correlation

- `terminal_probe_failed`: the existing capture-pane warning.
- `terminal_unavailable`: the existing three-probe error.
- `terminal_exit_observed`: an informational lifecycle record with the owning
  `session_id`, `terminal_lifecycle` (`required` or `auxiliary`),
  `session_status_before_exit` (`idle`, `running`, or `unknown`),
  `terminal_exit_status` when known, and `superseded`.
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

A Claude startup wrapper can print a failure while its launched command exits
zero. Before any identity-bound Claude hook has been observed for that launch,
a final launcher Error panel can add `launcher_failure_kind`,
`launcher_exit_evidence_source=terminal_final_error_panel`, and
`launcher_reported_exit_status` to the exit records. The reported nonzero code
comes from the panel; `terminal_exit_status` still records tmux's launched-command
status. Exception panels add a failure kind without inventing an exit code.

These fields contain allowlisted categories and numeric codes, without panel
text, paths, or credentials. Earlier panels, output followed by a later TUI,
unreadable hook evidence, and launches with any observed Claude hook do not
provide launcher attribution.

## Verification

```sh
uv run --no-sync pytest -q tests/inner/test_terminal.py tests/runner/test_resource_registry.py \
  tests/test_harness_diagnostics.py tests/runner/test_claude_lifecycle_telemetry.py --reruns 0
```

Run `omnidev` and explicitly close a disposable terminal. Verify that
`terminal_close_requested` carries its session and terminal-instance identifiers
in the debug-log table. For a naturally observed terminal disappearance, use the
instance identifier to find the probe warning and lifecycle record. Compare
required/auxiliary and prior session status against the subsequent turn outcome.
