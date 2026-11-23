"""agy hook entrypoint for native Omnigent policy enforcement.

Registered as the ``PreToolUse`` command hook in the session's isolated
``hooks.json`` (see :func:`omnigent.harnesses.antigravity_native.bridge.seed_isolated_agy_home`).
agy runs it before every tool call, including under ``--dangerously-skip-permissions``,
pipes ``{"toolCall": {"name", "args"}, "modelName", ...}`` on stdin, and reads
its verdict from stdout:

- empty stdout lets the tool run;
- ``{"decision": "deny", "reason": ...}`` blocks it and shows the model the reason.

agy also blocks the tool on any other output, a non-zero exit, or a hook that
outlives its ``timeout``. The policy conversion is shared with the other native
hooks via :mod:`omnigent.native.native_policy_hook`.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
from collections.abc import Mapping
from pathlib import Path

from omnigent.harnesses.antigravity_native.audit import (
    _TOOL_ARG_DISPLAY_KEYS,
    build_audit_evaluation_request,
)
from omnigent.harnesses.antigravity_native.bridge import read_policy_hook_config
from omnigent.native.native_policy_hook import (
    evaluation_response_to_hook_output,
    fail_closed_hook_output,
    policy_hook_reauth,
    post_evaluate_with_retry,
    read_relay_policy_config,
    relay_policy_evaluate_url,
)

_PRE_TOOL_USE = "PreToolUse"
# A TOOL_CALL ASK parks server-side until a human answers, so wait as long as
# the claude/codex hooks do; the seeded agy hook timeout matches.
_EVALUATE_POLICY_TIMEOUT_S = 86400.0
# agy routes every MCP tool through this meta-tool.
_MCP_META_TOOL = "call_mcp_tool"
_DEFAULT_DENY_REASON = "Denied by Omnigent policy."
_HOOK_LABEL = "antigravity evaluate-policy hook"


def main(argv: list[str] | None = None) -> int:
    """
    Dispatch an agy hook subcommand.

    :param argv: Optional argv override excluding the program name.
        ``None`` reads :data:`sys.argv`.
    :returns: Process exit code: ``0`` for ``evaluate-policy``, whose verdict
        goes to stdout, and ``2`` for an unknown subcommand, which agy blocks on.
    """
    raw_argv = sys.argv[1:] if argv is None else argv
    if raw_argv and raw_argv[0] == "evaluate-policy":
        return _main_evaluate_policy(raw_argv[1:])
    print(f"omnigent antigravity hook: unknown subcommand {raw_argv[:1]!r}", file=sys.stderr)
    return 2


def _main_evaluate_policy(argv: list[str]) -> int:
    """
    Evaluate one agy ``PreToolUse`` call against the session's Omnigent policies.

    agy runs this hook only when the launch registered it, so every invocation
    belongs to a governed session. A verdict that cannot be obtained (missing
    config, an unreadable payload, an unreachable or failing server, a
    malformed response) therefore fails CLOSED with a deny. ``ALLOW`` and
    ``mcp__omnigent__*`` tools, which the relay already gates, print nothing.

    :param argv: CLI argv after the ``evaluate-policy`` subcommand,
        e.g. ``["--bridge-dir", "/tmp/x"]``.
    :returns: Always ``0``.
    """
    parser = argparse.ArgumentParser(
        prog="python -m omnigent.harnesses.antigravity_native.hook evaluate-policy"
    )
    parser.add_argument("--bridge-dir", required=True)
    bridge_dir = Path(parser.parse_args(argv).bridge_dir)

    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        payload = None
    tool_call = _policy_tool_call(payload) if isinstance(payload, dict) else None
    if not isinstance(payload, dict) or tool_call is None:
        return _deny(_fail_closed_reason("unrecognized agy hook payload"))
    tool_name, tool_input = tool_call
    model = payload.get("modelName")
    eval_request = build_audit_evaluation_request(
        tool_name=tool_name,
        tool_input=tool_input,
        model=model if isinstance(model, str) and model else None,
    )
    if eval_request is None:
        return 0

    relay = read_relay_policy_config(bridge_dir)
    if relay:
        relay_url, relay_token, _relay_session_id = relay
        url = relay_policy_evaluate_url(relay_url)
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {relay_token}"}
        reauth = None
    else:
        config = read_policy_hook_config(bridge_dir)
        if config is None:
            return _deny(_fail_closed_reason("no Omnigent policy hook config for this session"))
        server_url, session_id, headers = config
        session_component = urllib.parse.quote(session_id, safe="")
        url = f"{server_url.rstrip('/')}/v1/sessions/{session_component}/policies/evaluate"
        reauth = policy_hook_reauth(server_url, headers)

    resp, api_error = post_evaluate_with_retry(
        url, headers, eval_request, _EVALUATE_POLICY_TIMEOUT_S, _HOOK_LABEL, reauth=reauth
    )
    if resp is None or not resp.content:
        detail = api_error or (reauth.failure_reason if reauth else None)
        return _deny(_fail_closed_reason(detail or "empty Omnigent response"))
    try:
        eval_response = resp.json()
    except json.JSONDecodeError:
        eval_response = None
    if not isinstance(eval_response, dict):
        return _deny(_fail_closed_reason("malformed Omnigent response"))

    reason = _deny_reason(evaluation_response_to_hook_output(_PRE_TOOL_USE, eval_response))
    return _deny(reason) if reason is not None else 0


def _policy_tool_call(payload: Mapping[str, object]) -> tuple[str, dict[str, object]] | None:
    """
    Return the policy-facing ``(tool_name, tool_input)`` for an agy hook payload.

    agy's display-only argument keys are dropped. An MCP call made through
    agy's meta-tool is named ``mcp__<server>__<tool>`` with the MCP tool's own
    arguments, the same name other harnesses evaluate.

    :param payload: Parsed agy hook stdin.
    :returns: The tool name and arguments, or ``None`` when the payload names no tool.
    """
    tool_call = payload.get("toolCall")
    if not isinstance(tool_call, dict):
        return None
    name = tool_call.get("name")
    if not isinstance(name, str) or not name:
        return None
    raw_args = tool_call.get("args")
    args: dict[str, object] = raw_args if isinstance(raw_args, dict) else {}
    server_name = args.get("ServerName")
    mcp_tool_name = args.get("ToolName")
    if name == _MCP_META_TOOL and isinstance(server_name, str) and isinstance(mcp_tool_name, str):
        arguments = args.get("Arguments")
        return (
            f"mcp__{server_name}__{mcp_tool_name}",
            arguments if isinstance(arguments, dict) else {},
        )
    return name, {key: value for key, value in args.items() if key not in _TOOL_ARG_DISPLAY_KEYS}


def _deny_reason(hook_output: Mapping[str, object] | None) -> str | None:
    """Return the deny reason from the shared ``PreToolUse`` hook output, or ``None``."""
    specific = hook_output.get("hookSpecificOutput") if hook_output else None
    if not isinstance(specific, dict) or specific.get("permissionDecision") != "deny":
        return None
    reason = specific.get("permissionDecisionReason")
    return reason if isinstance(reason, str) and reason else _DEFAULT_DENY_REASON


def _fail_closed_reason(detail: str) -> str:
    """Return the shared fail-closed reason with *detail* appended."""
    reason = _deny_reason(fail_closed_hook_output(_PRE_TOOL_USE, detail))
    return reason or _DEFAULT_DENY_REASON


def _deny(reason: str) -> int:
    """Write agy's deny verdict to stdout."""
    sys.stdout.write(json.dumps({"decision": "deny", "reason": reason}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
