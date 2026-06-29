"""Cursor preToolUse hook script for Omnigent policy enforcement.

Runs as a subprocess of the Cursor SDK bridge process, not the harness.

Reads tool-call info from stdin (Cursor hook protocol), evaluates
PHASE_TOOL_CALL policy via the Omnigent server, and returns the
verdict on stdout.

Environment variables (baked into the hooks.json command by the
CursorExecutor at session startup):

    _OMNIGENT_SERVER_URL  : Base URL of the Omnigent server
                            (e.g. ``http://127.0.0.1:6767``).
    _OMNIGENT_SESSION_ID  : Session / conversation ID for policy
                            evaluation.
"""

from __future__ import annotations

import json
import os
import sys


def main() -> None:
    server_url = os.environ.get("_OMNIGENT_SERVER_URL", "")
    session_id = os.environ.get("_OMNIGENT_SESSION_ID", "")

    if not server_url or not session_id:
        # No server wired -- fail open (allow).
        json.dump({"permission": "allow"}, sys.stdout)
        return

    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError, ValueError):
        json.dump({"permission": "allow"}, sys.stdout)
        return

    tool_name = payload.get("tool_name") or payload.get("toolName") or "unknown"
    tool_input = payload.get("tool_input") or payload.get("toolInput") or {}

    # Build the evaluation request matching the server's EvaluationRequest
    # schema.
    eval_body: dict[str, object] = {
        "event": {
            "type": "PHASE_TOOL_CALL",
            "target": "",
            "data": {
                "name": tool_name,
                "arguments": tool_input if isinstance(tool_input, dict) else {},
            },
            "context": {},
        },
    }

    url = f"{server_url.rstrip('/')}/v1/sessions/{session_id}/policies/evaluate"

    try:
        from omnigent.native_policy_hook import post_evaluate_with_retry

        resp = post_evaluate_with_retry(
            url=url,
            headers={"Content-Type": "application/json"},
            eval_request=eval_body,
            # One day — must match the hooks.json ``timeout`` and the
            # server's ``ask_timeout`` so the hook stays alive while the
            # human responds to the web-UI approval card.
            read_timeout=86400.0,
            hook_label="cursor preToolUse",
        )
    except Exception:  # noqa: BLE001 -- fail open on import / unexpected error
        json.dump({"permission": "allow"}, sys.stdout)
        return

    if resp is None:
        # Network error / retry budget exhausted -- fail open (allow) so a
        # transient server outage doesn't block the Cursor turn.
        json.dump({"permission": "allow"}, sys.stdout)
        return

    try:
        result = resp.json()
    except Exception:  # noqa: BLE001
        json.dump({"permission": "allow"}, sys.stdout)
        return

    action = result.get("result", "POLICY_ACTION_ALLOW")
    reason = result.get("reason", "")

    if action == "POLICY_ACTION_DENY":
        out: dict[str, str] = {"permission": "deny"}
        if reason:
            out["agent_message"] = f"Tool '{tool_name}' denied by Omnigent policy: {reason}"
        json.dump(out, sys.stdout)
    elif action == "POLICY_ACTION_ASK":
        # The server resolves ASK by parking the HTTP request until the
        # human decides via the web-UI approval card and returning a hard
        # ALLOW/DENY.  Receiving ASK here means the gate was not held
        # (e.g. read-only caller) — fail closed rather than granting
        # unreviewed permission.
        out = {"permission": "deny"}
        if reason:
            out["agent_message"] = f"Tool '{tool_name}' requires approval: {reason}"
        json.dump(out, sys.stdout)
    else:
        # ALLOW or UNSPECIFIED
        json.dump({"permission": "allow"}, sys.stdout)


if __name__ == "__main__":
    main()
