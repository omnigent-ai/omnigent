"""Goose ``PreToolUse`` hook for Omnigent policy enforcement.

Registered as a ``PreToolUse`` command hook in the per-session goose plugin
``<GOOSE_PATH_ROOT>/.agents/plugins/omnigent-policy/hooks/hooks.json`` written by
:func:`omnigent.goose_native_bridge.write_goose_policy_plugin`. goose runs the
hook BEFORE every tool call (``emit_blocking(PreToolUse)``) and honors a block
verdict — so this gates **every** tool, whether the turn came from the web
composer or was typed directly into the embedded terminal. (This is the goose
analog of :mod:`omnigent.inner.hermes_policy_hook`.)

goose pipes a JSON payload to stdin before each tool::

    {"event": "PreToolUse", "tool_name": "shell",
     "tool_input": {"command": "rm -rf /"}, "session_id": "..."}

To block, the hook writes ``{"decision": "block", "reason": "..."}`` to stdout;
empty JSON / ``{}`` means allow. ASK is resolved server-side (``/policies/evaluate``
parks the request, shows the web approval card, and returns a hard ALLOW/DENY),
so the hook only ever blocks or allows.

Environment (set in the goose terminal's spawn env; the hook subprocess inherits
it — goose runs hooks via ``sh`` without clearing the env):

    _OMNIGENT_SERVER_URL : Omnigent server base URL (e.g. ``http://127.0.0.1:6767``).
    _OMNIGENT_SESSION_ID : Omnigent conversation id for policy evaluation.
"""

from __future__ import annotations

import json
import os
import sys


def main() -> None:
    server_url = os.environ.get("_OMNIGENT_SERVER_URL", "")
    session_id = os.environ.get("_OMNIGENT_SESSION_ID", "")

    if not server_url or not session_id:
        # No server wired — fail open (allow).
        json.dump({}, sys.stdout)
        return

    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError, ValueError):
        json.dump({}, sys.stdout)
        return
    if not isinstance(payload, dict):
        json.dump({}, sys.stdout)
        return

    tool_name = payload.get("tool_name") or "unknown"
    tool_input = payload.get("tool_input")

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
            # One day — matches the server's ASK timeout so the hook stays alive
            # while the human answers the web-UI approval card.
            read_timeout=86400.0,
            hook_label="goose PreToolUse",
        )
    except Exception:  # noqa: BLE001 — fail open on import / unexpected error
        json.dump({}, sys.stdout)
        return

    if resp is None:
        # Network error / retry budget exhausted — fail closed so a transient
        # server outage can't let unreviewed tools through.
        json.dump(
            {"decision": "block", "reason": "Policy evaluation unavailable"},
            sys.stdout,
        )
        return

    try:
        result = resp.json()
    except Exception:  # noqa: BLE001
        json.dump({"decision": "block", "reason": "Malformed policy response"}, sys.stdout)
        return

    action = result.get("result", "POLICY_ACTION_ALLOW")
    reason = result.get("reason", "")

    if action == "POLICY_ACTION_DENY":
        out: dict[str, str] = {"decision": "block"}
        out["reason"] = (
            f"Tool '{tool_name}' denied by Omnigent policy: {reason}"
            if reason
            else f"Tool '{tool_name}' denied by Omnigent policy"
        )
        json.dump(out, sys.stdout)
    elif action == "POLICY_ACTION_ASK":
        # ASK should be collapsed to a hard verdict server-side; if it reaches
        # the hook the gate wasn't held — fail closed rather than allow.
        out = {"decision": "block"}
        out["reason"] = (
            f"Tool '{tool_name}' requires approval: {reason}"
            if reason
            else f"Tool '{tool_name}' requires approval"
        )
        json.dump(out, sys.stdout)
    else:
        # ALLOW / UNSPECIFIED — empty JSON means no objection.
        json.dump({}, sys.stdout)


if __name__ == "__main__":
    main()
