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

The new attributes contain no pane contents, command arguments, or filesystem
paths. Existing human-readable errors remain unchanged. No extra tmux probes
are introduced for diagnostics.

## Verification

```sh
uv run --no-sync pytest -q tests/inner/test_terminal.py tests/runner/test_resource_registry.py
```

Run `omnidev` and explicitly close a disposable terminal. Verify that
`terminal_close_requested` carries its session and terminal-instance identifiers
in the debug-log table. For a naturally observed terminal disappearance, use the
instance identifier to find the probe warning and lifecycle record. Compare
required/auxiliary and prior session status against the subsequent turn outcome.
