"""Shared conversion between native-harness tool hooks and Omnigent policy events.

Both Claude Code and Codex expose a command-hook system whose
``PreToolUse`` / ``PostToolUse`` payloads use the same field names
(``hook_event_name``, ``tool_name``, ``tool_input``, ``tool_output``)
and the same ``hookSpecificOutput.permissionDecision`` output contract.
This module owns the harness-neutral translation between that hook shape
and the server's proto-compatible ``EvaluationRequest`` /
``EvaluationResponse`` schema served by
``POST /v1/sessions/{id}/policies/evaluate``, so the per-harness hook
entrypoints (:mod:`omnigent.claude_native_hook`,
:mod:`omnigent.codex_native_hook`) share one implementation.
"""

from __future__ import annotations

# Hook event names that gate tool execution and therefore carry policy
# meaning. ``PreToolUse`` fires before the tool runs (can block);
# ``PostToolUse`` fires after (observational — can only warn).
_PRE_TOOL_USE = "PreToolUse"
_POST_TOOL_USE = "PostToolUse"


def hook_payload_to_evaluation_request(
    hook_event: str,
    payload: dict[str, object],
) -> dict[str, object] | None:
    """
    Convert a native-harness tool-hook payload into a proto ``EvaluationRequest``.

    Maps ``PreToolUse`` to a ``PHASE_TOOL_CALL`` event and
    ``PostToolUse`` to a ``PHASE_TOOL_RESULT`` event. Omnigent MCP tools
    (``mcp__omnigent__*``) are skipped because they are already
    policy-checked by the relay path (``ProxyMcpManager`` → Omnigent
    ``/mcp`` endpoint → ``_evaluate_tool_call_policy``); evaluating
    them here would double-count. Connector-native MCP tools
    (for example ``mcp__github__*``) still need this pre-call gate.

    :param hook_event: Hook event name from the payload's
        ``hook_event_name`` field, e.g. ``"PreToolUse"`` or
        ``"PostToolUse"``.
    :param payload: Raw hook JSON from the harness, e.g.
        ``{"hook_event_name": "PreToolUse", "tool_name": "Bash",
        "tool_input": {"command": "rm -rf /"}}``.
    :returns: An ``EvaluationRequest`` dict suitable for POSTing to
        ``/policies/evaluate``, or ``None`` when the event is not
        policy-relevant (unknown event or an ``mcp__omnigent__*`` tool).
    """
    tool_name = payload.get("tool_name", "")
    # Omnigent MCP tools are already policy-checked by the relay path
    # (ProxyMcpManager → Omnigent /mcp endpoint → _evaluate_tool_call_policy).
    # Skip only those here to avoid double evaluation; connector-native MCP
    # tools such as mcp__github__* must still go through this hook.
    if isinstance(tool_name, str) and tool_name.startswith("mcp__omnigent__"):
        return None
    tool_input = payload.get("tool_input") or {}
    if hook_event == _PRE_TOOL_USE:
        return {
            "event": {
                "type": "PHASE_TOOL_CALL",
                "target": "",
                "data": {
                    "name": tool_name,
                    "arguments": tool_input,
                },
                "context": {},
            },
        }
    if hook_event == _POST_TOOL_USE:
        tool_output = payload.get("tool_output", "")
        return {
            "event": {
                "type": "PHASE_TOOL_RESULT",
                "target": "",
                "data": {
                    "result": tool_output,
                },
                "context": {},
                "request_data": {
                    "name": tool_name,
                    "arguments": tool_input,
                },
            },
        }
    return None


def evaluation_response_to_hook_output(
    hook_event: str,
    eval_response: dict[str, object],
) -> dict[str, object] | None:
    """
    Convert an ``EvaluationResponse`` into native-harness hook output JSON.

    For ``PreToolUse`` the policy layer only *enforces* — it emits a
    ``hookSpecificOutput.permissionDecision`` solely for verdicts that
    constrain the tool: ``POLICY_ACTION_DENY`` → ``"deny"`` (with
    ``permissionDecisionReason``). ``POLICY_ACTION_ASK`` is resolved
    server-side now (URL-based elicitation: ``POST /policies/evaluate``
    holds the gate and returns a hard ALLOW/DENY), so the hook should
    never see ASK; if it does, it fails closed with ``"deny"`` rather
    than the old ``"defer"`` — ``defer`` handed control back to the
    harness's ``permission_mode``, which ``acceptEdits`` /
    ``bypassPermissions`` would auto-approve, bypassing the human.
    ``POLICY_ACTION_ALLOW`` — which is the engine's default verdict when
    no policy matches a tool call, not just an explicit author allow —
    returns ``None`` ("no opinion") so the harness's *own* permission
    system still runs. Emitting ``"allow"`` here would auto-approve the
    tool and suppress the harness's native permission prompt (and, for
    Claude Code, the ``PermissionRequest`` hook that routes that prompt
    to the web UI), collapsing two independent gates — the deployment's
    policy gate and the user's own consent gate — into one. The policy
    layer may block (DENY) or demand approval (ASK); it must not silence
    the user's consent. For ``PostToolUse`` a ``DENY`` is surfaced as
    ``additionalContext`` because the tool result is already committed
    — PostToolUse hooks cannot block.

    Both Claude Code and Codex consume this exact output shape, so the
    ``hookEventName`` echoed back is the harness-supplied ``hook_event``.

    :param hook_event: Hook event name, e.g. ``"PreToolUse"`` or
        ``"PostToolUse"``.
    :param eval_response: Parsed ``EvaluationResponse`` from AP, e.g.
        ``{"result": "POLICY_ACTION_DENY", "reason": "blocked by policy"}``.
    :returns: Hook output dict for the harness to read on stdout, or
        ``None`` when there is no verdict to express (allow with no
        rewrite on PostToolUse, or an unknown action).
    """
    action = eval_response.get("result", "POLICY_ACTION_UNSPECIFIED")
    reason = eval_response.get("reason")

    if hook_event == _PRE_TOOL_USE:
        # ALLOW (the engine default when no policy matches) is omitted → None,
        # so the harness's own permission prompt still fires; see docstring.
        decision_map = {
            "POLICY_ACTION_DENY": "deny",
            # ASK is resolved server-side now (URL-based elicitation:
            # POST /policies/evaluate holds the gate and returns a hard
            # ALLOW/DENY), so the hook should never see ASK here. If it
            # somehow does, fail closed with ``deny`` rather than the old
            # ``defer`` — ``defer`` returns control to the harness's
            # permission_mode, which acceptEdits / bypassPermissions would
            # auto-approve, re-opening the very bypass this closes.
            "POLICY_ACTION_ASK": "deny",
        }
        decision = decision_map.get(str(action))
        if decision is None:
            return None
        output: dict[str, object] = {
            "hookEventName": _PRE_TOOL_USE,
            "permissionDecision": decision,
        }
        if decision == "deny" and reason:
            output["permissionDecisionReason"] = reason
        return {"hookSpecificOutput": output}

    if hook_event == _POST_TOOL_USE:
        if action == "POLICY_ACTION_DENY" and reason:
            return {
                "hookSpecificOutput": {
                    "hookEventName": _POST_TOOL_USE,
                    "additionalContext": f"[Policy violation] {reason}",
                },
            }
        return None

    return None
