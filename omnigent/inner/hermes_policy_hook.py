"""Hermes ``pre_tool_call`` shell hook for Omnigent policy enforcement.

Registered as a ``pre_tool_call`` hook in the per-session
``HERMES_HOME/config.yaml`` written by :func:`_populate_hermes_home`
in :mod:`hermes_executor`.

Hermes pipes a JSON payload to stdin before each tool execution::

    {
        "hook_event_name": "pre_tool_call",
        "tool_name": "terminal",
        "tool_input": {"command": "rm -rf /"},
        "session_id": "...",
        "cwd": "..."
    }

The hook evaluates ``PHASE_TOOL_CALL`` policy via the Omnigent server.
To block, it writes to stdout::

    {"decision": "block", "reason": "..."}

Empty JSON or ``{}`` means allow.

Environment variables (set by the wrapper shell script):

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
        json.dump({}, sys.stdout)
        return

    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError, ValueError):
        json.dump({}, sys.stdout)
        return

    tool_name = payload.get("tool_name") or "unknown"
    tool_input = payload.get("tool_input") or {}

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
        from omnigent.native_policy_hook import (
            policy_hook_reauth,
            policy_hook_request_headers,
            post_evaluate_with_retry,
        )

        headers = policy_hook_request_headers()
        resp = post_evaluate_with_retry(
            url=url,
            headers=headers,
            eval_request=eval_body,
            # One day — must match the server's ``ask_timeout`` so the hook
            # stays alive while the human responds to the web-UI approval card.
            read_timeout=86400.0,
            hook_label="hermes pre_tool_call",
            # Re-mint the baked one-shot token if it lapses mid-session.
            reauth=policy_hook_reauth(server_url, headers),
        )
    except Exception:  # noqa: BLE001 -- fail open on import / unexpected error
        json.dump({}, sys.stdout)
        return

    if resp is None:
        # Network error / retry budget exhausted -- fail closed so a
        # transient server outage doesn't let unreviewed tools through.
        json.dump(
            {"decision": "block", "reason": "Policy evaluation unavailable"},
            sys.stdout,
        )
        return

    try:
        result = resp.json()
    except Exception:  # noqa: BLE001
        json.dump(
            {"decision": "block", "reason": "Malformed policy response"},
            sys.stdout,
        )
        return

    action = result.get("result", "POLICY_ACTION_ALLOW")
    reason = result.get("reason", "")

    if action == "POLICY_ACTION_DENY":
        out: dict[str, str] = {"decision": "block"}
        if reason:
            out["reason"] = f"Tool '{tool_name}' denied by Omnigent policy: {reason}"
        else:
            out["reason"] = f"Tool '{tool_name}' denied by Omnigent policy"
        json.dump(out, sys.stdout)
    elif action == "POLICY_ACTION_ASK":
        # The server resolves ASK by parking the HTTP request until the
        # human decides via the web-UI approval card and returning a hard
        # ALLOW/DENY.  Receiving ASK here means the gate was not held
        # — fail closed rather than granting unreviewed permission.
        out = {"decision": "block"}
        if reason:
            out["reason"] = f"Tool '{tool_name}' requires approval: {reason}"
        else:
            out["reason"] = f"Tool '{tool_name}' requires approval"
        json.dump(out, sys.stdout)
    else:
        # ALLOW or UNSPECIFIED — empty JSON means no objection.
        json.dump({}, sys.stdout)


if __name__ == "__main__":
    main()
