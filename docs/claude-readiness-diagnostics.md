# Claude prompt-readiness diagnostics

The existing prompt-delivery timeout error gains structured diagnostics. Timeout
length, error severity, cleanup behavior, and dashboard exclusions are unchanged.
Deploy the updated runner/harness code before expecting these fields; historical
logs are unchanged.

The debug-log sink stores `event_name` separately and serializes non-null
`attributes` values as strings. Booleans appear as `True` or `False`.

## Event and fields

`harness_prompt_delivery_timeout` includes:

- The owning `session_id` and `harness`.
- `delivery_operation`: `model_switch` or `message_delivery`.
- `message_delivered` and `cleanup_succeeded`.
- `readiness_stage`, `readiness_timeout_s`, `readiness_elapsed_ms`,
  `readiness_polls`, `readiness_empty_captures`, and
  `readiness_last_capture_empty`.
- `readiness_*_visible` signals for config setup, authentication flows, fullscreen
  prompts, external-import prompts, confirmation dialogs, and a dead pane.

Visibility signals describe the last nonempty capture observed during the wait.
They can overlap and are clues, not proven causes: text may still be visible after
a setup step completed. No extra capture or subprocess runs at timeout. For
example, all-empty captures differ from a consistently visible setup dialog.
`cleanup_succeeded=True` also covers a terminal that was already absent.

The new attributes contain no terminal text, user prompts, or credentials.
Existing human-readable error messages and exception tails are unchanged.

## Verification

```sh
uv run --no-sync pytest -q tests/inner/test_claude_native_executor.py tests/test_claude_native_bridge.py
```

For a manual check, start `omnidev` and use a disposable session. If Claude is
waiting on a startup/login dialog, send a web-chat message and let the readiness
timeout occur. Inspect `harness_prompt_delivery_timeout` in the debug-log table:
verify the session, delivery operation, poll counts, visibility signals, and
cleanup result. Do not dismiss approval dialogs automatically just to test logs.
