# Capturing terminal probe failures

When the idle watcher declares tmux unavailable, the ERROR includes a
`diagnostics=` JSON object in local logs and the same fields in the structured
`terminal_unavailable` event. The existing readable summary remains present.

The watcher retains both `capture-pane` and `has-session` failures as they occur.
On the third consecutive failed pair, it logs that history and a filesystem
snapshot before marking the terminal stopped and invoking its exit callback.
No additional subprocess is started to gather diagnostics.

`probe_failures_json` is a JSON array containing up to six failed attempts. Each
entry has the fixed probe command name, completion time (`failed_at_unix_ms`),
`elapsed_ms`, `returncode`, OS `errno`, and error text. Permanent spawn failures
carry an errno instead of a return code. Transient spawn failures still leave
liveness unknown and trigger the existing warning/backoff; they reset the
failure streak rather than counting toward terminal death. A successful capture
or session confirmation also clears the history.

The snapshot includes:

- `terminal_instance_id`, terminal name/key, `process_id`, and `effective_uid`.
- `socket_state` and `private_dir_state`, ownership and modes when readable,
  or stat errnos when unavailable.
- `consecutive_probe_failures`, `last_capture_age_ms`, `pane_output_seen`,
  `keep_alive_after_exit`, `terminal_exit_status`, and `shutdown_requested`.

The new bundle excludes pane contents, environment variables, and full command
arguments. Error text has the private socket/directory paths replaced, passes
through the standard secret redactor, and is capped at 1,024 characters per
attempt. `error_truncated` marks truncation. This does not change the existing
readable summary's pane tail or error strings. Structured log attributes are
stored as strings; decode `attributes['probe_failures_json']` to inspect the
attempts.

These are observations after a probe fails, captured before watcher-driven
cleanup. They distinguish a missing socket, a surviving socket that refuses
connections, and a permanent client-spawn error. They do not establish why the
tmux server disappeared. Correlate the timestamps with host lifecycle and OS
crash/OOM records. An abruptly killed runner cannot finish logging, and remote
log delivery remains best-effort. Local file logging flushes through the normal
handler; it is not an fsync guarantee against host power loss.

## Verification

```sh
.venv/bin/python -m pytest -q tests/inner/test_terminal.py tests/inner/test_terminal_probe_diagnostics.py tests/runner/test_resource_registry.py
```

Both watcher tests read a real log file inside the exit callback, before that
callback removes the socket directory. They check all six attempts are already
present even with warnings filtered out. Other tests cover recovery, transient
spawn failures, redaction, bounded history, and failed socket inspection.

For a manual check, run this from the checkout with tmux on PATH. It creates a
private test server, captures a healthy pane, stops that server, and observes
the diagnostic ERROR. Only the temporary test server is affected.

```sh
.venv/bin/python - <<'PY'
import asyncio
import logging
import tempfile
from pathlib import Path
from omnigent.inner.terminal import TerminalInstance

async def main():
    logging.basicConfig(level=logging.ERROR)
    with tempfile.TemporaryDirectory(prefix="tmux-probe-check-") as directory:
        terminal = TerminalInstance(
            name="diagnostic-check",
            session_key="main",
            socket_path=Path(directory) / "tmux.sock",
            private_dir=Path(directory),
            running=True,
        )
        try:
            await terminal._tmux("new-session", "-d", "-s", "main", "sleep 60")
            snapshot = await terminal._tmux_output("capture-pane", "-t", "main", "-p")
            terminal._remember_pane_snapshot(snapshot)
            await terminal._tmux("kill-server")
            await terminal._idle_watch_loop(lambda: None, on_exit=terminal.close)
        finally:
            await terminal.close()

asyncio.run(main())
PY
```

Expect one ERROR containing `diagnostics=`, three `capture-pane` / `has-session`
pairs with return codes and error text, `pane_output_seen=true`,
and `private_dir_state="directory"`. Depending on the tmux version, the socket
may be `"missing"` or still present as `"socket"` after the server exits; the
snapshot records which occurred before cleanup removes the directory.
