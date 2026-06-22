"""Shared conversion between native-harness hooks and Omnigent policy events.

Both Claude Code and Codex expose a command-hook system whose
``PreToolUse`` / ``PostToolUse`` payloads use the same field names
(``hook_event_name``, ``tool_name``, ``tool_input``, ``tool_output``)
and whose ``UserPromptSubmit`` payload carries the user prompt under
``prompt``. This module owns the harness-neutral translation between
that hook shape and the server's proto-compatible ``EvaluationRequest``
/ ``EvaluationResponse`` schema served by
``POST /v1/sessions/{id}/policies/evaluate``, so the per-harness hook
entrypoints (:mod:`omnigent.claude_native_hook`,
:mod:`omnigent.codex_native_hook`) share one implementation.

The output contract differs by hook event: ``PreToolUse`` enforces via
``hookSpecificOutput.permissionDecision``, while ``UserPromptSubmit``
enforces via the top-level ``decision`` / ``reason`` fields (both
harnesses parse ``decision: "block"`` to drop the prompt before the
model sees it).
"""

from __future__ import annotations

import json
import sys
import time

import httpx

# How long to keep retrying transient 5xx / connect errors on the
# policy evaluate POST before failing closed. Keeps the pre-execution
# gate from blocking long on a sick server while still absorbing brief
# DB hiccups on a hosted deployment.
_EVALUATE_POLICY_RETRY_BUDGET_S = 30.0
_EVALUATE_POLICY_RETRY_INITIAL_BACKOFF_S = 1.0
_EVALUATE_POLICY_RETRY_MAX_BACKOFF_S = 10.0
# Fast connect budget so an unreachable server fails into the retry
# loop quickly rather than blocking on the day-long read timeout.
_EVALUATE_POLICY_CONNECT_TIMEOUT_S = 5.0

# Hook event names that gate tool execution and therefore carry policy
# meaning. ``PreToolUse`` fires before the tool runs (can block);
# ``PostToolUse`` fires after (observational — can only warn).
_PRE_TOOL_USE = "PreToolUse"
_POST_TOOL_USE = "PostToolUse"
# ``UserPromptSubmit`` fires when a new user prompt reaches the harness —
# for native sessions this is the request-phase gate (the server-level
# ``_evaluate_input_policy`` is bypassed for native message events, so
# this hook is the sole REQUEST gate and covers both web-UI-injected and
# direct-terminal prompts). It can block the prompt before the model runs.
_USER_PROMPT_SUBMIT = "UserPromptSubmit"

# Reason surfaced when a tool call is denied because its policy verdict
# could not be obtained (server unreachable / non-2xx / empty or malformed
# body). Mirrors the runner-side fail-closed default in
# ``omnigent.runner.app._evaluate_policy_via_omnigent`` (PR #163).
_EVAL_UNAVAILABLE_REASON = (
    "Omnigent policy evaluation unavailable; failing closed for this tool call."
)


def hook_payload_to_evaluation_request(
    hook_event: str,
    payload: dict[str, object],
) -> dict[str, object] | None:
    """
    Convert a native-harness tool-hook payload into a proto ``EvaluationRequest``.

    Maps ``PreToolUse`` to a ``PHASE_TOOL_CALL`` event, ``PostToolUse``
    to a ``PHASE_TOOL_RESULT`` event, and ``UserPromptSubmit`` to a
    ``PHASE_REQUEST`` event (the prompt text from the payload's
    ``prompt`` field becomes the request content). Omnigent MCP tools
    (``mcp__omnigent__*``) are skipped because they are already
    policy-checked by the relay path (``ProxyMcpManager`` → Omnigent
    ``/mcp`` endpoint → ``_evaluate_tool_call_policy``); evaluating
    them here would double-count. Connector-native MCP tools
    (for example ``mcp__github__*``) still need this pre-call gate.

    :param hook_event: Hook event name from the payload's
        ``hook_event_name`` field, e.g. ``"PreToolUse"``,
        ``"PostToolUse"``, or ``"UserPromptSubmit"``.
    :param payload: Raw hook JSON from the harness, e.g.
        ``{"hook_event_name": "PreToolUse", "tool_name": "Bash",
        "tool_input": {"command": "rm -rf /"}}``.
    :returns: An ``EvaluationRequest`` dict suitable for POSTing to
        ``/policies/evaluate``, or ``None`` when the event is not
        policy-relevant (unknown event or an ``mcp__omnigent__*`` tool).
    """
    if hook_event == _USER_PROMPT_SUBMIT:
        # Request-phase gate for native sessions. The server reads REQUEST
        # content from ``data.text`` (see ``_build_evaluation_context``).
        prompt = payload.get("prompt", "")
        return {
            "event": {
                "type": "PHASE_REQUEST",
                "target": "",
                "data": {
                    "text": prompt if isinstance(prompt, str) else json.dumps(prompt),
                },
                "context": {},
            },
        }
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

    For ``UserPromptSubmit`` the output uses the top-level ``decision`` /
    ``reason`` contract (not ``permissionDecision``): ``DENY`` → ``{"decision":
    "block", "reason": ...}``, which drops the prompt before the model sees
    it. ASK is resolved server-side (``_hold_native_ask_gate`` collapses it
    to a hard ALLOW/DENY before the response reaches the hook), so the hook
    should never see ASK; if it somehow does, it fails closed by blocking.
    ALLOW (and the engine's no-match default) returns ``None`` so the prompt
    proceeds. Unlike ``PreToolUse``, there is no separate user-consent gate
    on a prompt, so ALLOW need not preserve one.

    Both Claude Code and Codex consume these exact output shapes, so the
    ``hookEventName`` echoed back is the harness-supplied ``hook_event``.

    :param hook_event: Hook event name, e.g. ``"PreToolUse"``,
        ``"PostToolUse"``, or ``"UserPromptSubmit"``.
    :param eval_response: Parsed ``EvaluationResponse`` from AP, e.g.
        ``{"result": "POLICY_ACTION_DENY", "reason": "blocked by policy"}``.
    :returns: Hook output dict for the harness to read on stdout, or
        ``None`` when there is no verdict to express (allow with no
        rewrite on PostToolUse, or an unknown action).
    """
    action = eval_response.get("result", "POLICY_ACTION_UNSPECIFIED")
    reason = eval_response.get("reason")

    if hook_event == _USER_PROMPT_SUBMIT:
        # DENY blocks the prompt; a stray ASK fails closed (also block) since
        # ASK is meant to be resolved server-side before reaching the hook.
        # ALLOW / no-match → None so the prompt proceeds. A non-empty reason
        # is required for the block to take effect (both harnesses drop a
        # block with an empty reason), so default one in.
        if action in ("POLICY_ACTION_DENY", "POLICY_ACTION_ASK"):
            return {
                "decision": "block",
                "reason": reason or "Denied by policy",
            }
        return None

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


def fail_closed_hook_output(hook_event: str) -> dict[str, object] | None:
    """
    Build the fail-closed hook output for an unobtainable policy verdict.

    Called by the per-harness hooks when the ``/policies/evaluate``
    round-trip cannot produce a usable verdict for an *already-governed*
    session — the server is unreachable, returns a non-2xx status, or
    returns an empty / malformed body. Without this the hooks emitted "no
    opinion" on those paths, silently letting the gated tool run: for
    native harnesses this hook is the sole enforcement point (it gates
    Bash / Write / Edit / the native Skill tool / connector-native
    ``mcp__*`` tools), so a transient outage disabled all DENY/ASK
    enforcement.

    The default is phase-aware, matching
    :data:`omnigent.policies.types.FAIL_CLOSED_PHASES` (the runner-side
    precedent from PR #163) — but expressed in hook-event terms so the
    lightweight hook subprocess need not import the policy package:

    - ``PreToolUse`` (``PHASE_TOOL_CALL``) fails CLOSED → ``deny``. This is
      the authoritative pre-execution gate; an unevaluable policy must not
      let the call through.
    - ``UserPromptSubmit`` (``PHASE_REQUEST``) and ``PostToolUse``
      (``PHASE_TOOL_RESULT``) fail OPEN → ``None``. The request gate is
      advisory (the tool-call gate still catches dangerous actions) and by
      the result phase the tool has already executed, so denying would only
      block an already-incurred side effect.

    :param hook_event: Hook event name, e.g. ``"PreToolUse"``.
    :returns: A ``permissionDecision: "deny"`` hook output for
        ``PreToolUse``; ``None`` for every other event (fail open).
    """
    if hook_event == _PRE_TOOL_USE:
        return {
            "hookSpecificOutput": {
                "hookEventName": _PRE_TOOL_USE,
                "permissionDecision": "deny",
                "permissionDecisionReason": _EVAL_UNAVAILABLE_REASON,
            },
        }
    return None


def post_evaluate_with_retry(
    url: str,
    headers: dict[str, str],
    eval_request: dict[str, object],
    read_timeout: float,
    hook_label: str,
) -> httpx.Response | None:
    """
    POST to the Omnigent policy evaluate endpoint, retrying on transient errors.

    Retries on 5xx HTTP responses and connection-level errors
    (:class:`httpx.ConnectError`, :class:`httpx.ConnectTimeout`) within
    :data:`_EVALUATE_POLICY_RETRY_BUDGET_S`. Returns the successful response,
    or ``None`` if the budget is exhausted or a non-retryable error occurs.

    4xx responses are final — a bad request won't succeed on retry. Other
    mid-stream errors (e.g. :class:`httpx.ReadTimeout`) are also not retried:
    a read timeout can indicate a long-polling ASK gate being severed, and
    retrying would open a new server-side elicitation, prompting the human
    twice. The caller is responsible for fail-closed handling on ``None``.

    :param url: Absolute URL of the evaluate endpoint.
    :param headers: Auth headers for the Omnigent server.
    :param eval_request: ``EvaluationRequest`` JSON body to POST.
    :param read_timeout: Per-attempt read timeout in seconds. Should be
        large (e.g. one day) to accommodate long-polling ASK gates.
    :param hook_label: Diagnostic label used in stderr messages,
        e.g. ``"evaluate-policy hook"`` or ``"codex evaluate-policy hook"``.
    :returns: Successful :class:`httpx.Response`, or ``None`` when retries
        are exhausted or the error is non-retryable.
    """
    deadline = time.monotonic() + _EVALUATE_POLICY_RETRY_BUDGET_S
    backoff_s = _EVALUATE_POLICY_RETRY_INITIAL_BACKOFF_S
    timeout = httpx.Timeout(read_timeout, connect=_EVALUATE_POLICY_CONNECT_TIMEOUT_S)
    while True:
        try:
            with httpx.Client(headers=headers, timeout=timeout) as client:
                resp = client.post(url, json=eval_request)
                resp.raise_for_status()
                return resp
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code < 500:
                print(
                    f"omnigent {hook_label}: Omnigent returned {exc.response.status_code}",
                    file=sys.stderr,
                )
                return None
            print(
                f"omnigent {hook_label}: Omnigent returned {exc.response.status_code}; retrying",
                file=sys.stderr,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            print(
                f"omnigent {hook_label}: Omnigent request failed; retrying: {exc}",
                file=sys.stderr,
            )
        except httpx.HTTPError as exc:
            # Other HTTP errors (ReadTimeout while a long ASK poll is in flight,
            # etc.) are not retried — retrying a severed ASK would open a new
            # elicitation and prompt the human twice.
            print(
                f"omnigent {hook_label}: Omnigent request failed: {exc}",
                file=sys.stderr,
            )
            return None
        if time.monotonic() + backoff_s >= deadline:
            print(
                f"omnigent {hook_label}: retry budget exhausted",
                file=sys.stderr,
            )
            return None
        # Two-step backoff; not worth a retry library in this dependency-light hook.
        time.sleep(backoff_s)
        backoff_s = min(backoff_s * 2, _EVALUATE_POLICY_RETRY_MAX_BACKOFF_S)
