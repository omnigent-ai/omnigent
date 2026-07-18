"""
Fast lifecycle-event hook for the native Omnigent CoCo (Cortex Code) wrapper.

The coco-native bridge registers this script in the per-session
``SNOWFLAKE_HOME``'s ``cortex/hooks.json`` for the ``SessionStart``,
``UserPromptSubmit`` and ``Stop`` events. CoCo **blocks** on command hooks, so
this module is deliberately tiny: it imports only the standard library and does
nothing but append the hook's stdin JSON payload as one structured line to
``<bridge_dir>/hook_events.jsonl``.

The background transcript forwarder tails that file:

- ``SessionStart`` / ``UserPromptSubmit`` carry ``session_id`` +
  ``transcript_path`` — this is how Omnigent discovers which CoCo conversation
  the TUI actually opened (CoCo mints its own session id in TUI mode and
  ignores ``--session-id``, verified on v1.1.1).
- ``UserPromptSubmit`` marks a turn opening (the ``running`` status edge).
- ``Stop`` marks a turn ending; CoCo flushes the session's
  ``<session_id>.history.jsonl`` transcript before firing it (verified on
  v1.1.1), so the forwarder mirrors the finished turn from that file.

Observed payload shape (CoCo v1.1.1, Claude-Code-compatible)::

    {
      "hook_event_name": "SessionStart" | "UserPromptSubmit" | "Stop",
      "session_id": "<coco-session-uuid>",
      "transcript_path": "/.../conversations/<session_id>.json",
      "cwd": "/path/to/workspace",
      "permission_mode": "default",
      "prompt": "...",            # UserPromptSubmit only
      "stop_hook_active": false   # Stop only
    }
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# Append-only events file written in the bridge directory. The forwarder
# imports this constant so both sides agree on the path without the forwarder
# paying this module's (stdlib-only) import cost on its hot path. Kept here
# because this module is the writer.
HOOK_EVENTS_FILE = "hook_events.jsonl"


def main(argv: list[str] | None = None) -> int:
    """Append the hook payload from stdin to the bridge events file.

    Exits 0 unconditionally: a hook failure must never block or degrade the
    user's CoCo TUI session (a non-zero exit could suppress the action the
    hook observed).

    :param argv: CLI args; ``None`` uses ``sys.argv[1:]``.
    :returns: Process exit code (always 0).
    """
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--bridge-dir", required=True)
    try:
        args, _ = parser.parse_known_args(argv)
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            payload = {"malformed_payload": raw[:2000]}
        line = json.dumps(payload, separators=(",", ":"))
        path = os.path.join(args.bridge_dir, HOOK_EVENTS_FILE)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:  # noqa: BLE001 — never fail the TUI from a hook
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
