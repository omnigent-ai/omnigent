"""Devin lifecycle-hook entrypoint for the devin-native harness.

Devin runs a configured hook as a subprocess per event, hands it the event JSON
on stdin, and reads a JSON verdict from stdout (exit 2 also blocks). This module
is that subprocess. It has three jobs:

#. **Record.** Every event is appended to the bridge's ``hooks.jsonl`` so
   :mod:`omnigent.harnesses.devin_native.forwarder` can mirror the conversation into
   Omnigent. This happens first and unconditionally — a policy or network
   failure must not cost us the transcript.

#. **Enforce Omnigent policy.** ``UserPromptSubmit`` (request phase),
   ``PreToolUse`` (tool-call phase) and ``PostToolUse`` (tool-result phase) are
   POSTed to ``/v1/sessions/{id}/policies/evaluate``. The shared
   :mod:`omnigent.native.native_policy_hook` seam owns the request/response
   translation, including the fail-closed defaults, so devin behaves exactly
   like claude-native and codex-native here. An ASK verdict is resolved
   server-side (the POST parks until a human answers) and comes back as a hard
   ALLOW/DENY.

#. **Mirror interactive prompts.** ``PermissionRequest`` carries approvals;
   ``PreToolUse`` carries ``ask_user_question``. Both become web elicitations,
   with question answers returned through Devin's ``updatedInput`` contract.

Kept deliberately import-light: it runs on every tool call, so it must not pull
in the runner or server stacks.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
from pathlib import Path

import httpx

from omnigent.harnesses.devin_native.questions import answered_question_input, question_payload
from omnigent.native.native_policy_hook import (
    _is_login_redirect_or_unauthorized,
    evaluation_response_to_hook_output,
    fail_closed_hook_output,
    hook_payload_to_evaluation_request,
    policy_hook_reauth,
    policy_hook_request_headers,
    post_evaluate_with_retry,
)
from omnigent.util.json_types import JsonObject as _JsonObject

_SERVER_URL_ENV = "_OMNIGENT_SERVER_URL"
_SESSION_ID_ENV = "_OMNIGENT_SESSION_ID"

_HOOK_LABEL = "devin evaluate-policy hook"

#: Policy ASK gates park server-side until a human answers, so the evaluate POST
#: needs a read timeout long enough to outlast a coffee break. Matches the
#: claude/codex hooks' day-long budget.
_EVALUATE_READ_TIMEOUT_S = 86_400.0
#: PermissionRequest long-poll budget. On expiry we emit nothing, which Devin
#: treats as "no opinion" and falls back to its own TUI prompt (fail-ask).
_PERMISSION_READ_TIMEOUT_S = 86_400.0
_PERMISSION_CONNECT_TIMEOUT_S = 5.0

_PRE_TOOL_USE = "PreToolUse"
_POST_TOOL_USE = "PostToolUse"
_USER_PROMPT_SUBMIT = "UserPromptSubmit"
_PERMISSION_REQUEST = "PermissionRequest"
_SESSION_END = "SessionEnd"

#: Events carrying an Omnigent policy verdict.
_POLICY_EVENTS = frozenset({_PRE_TOOL_USE, _POST_TOOL_USE, _USER_PROMPT_SUBMIT})


def _read_payload() -> _JsonObject | None:
    """Read and parse the hook payload from stdin."""
    try:
        raw = sys.stdin.read()
    except OSError:
        return None
    if not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _normalize_tool_result(payload: _JsonObject) -> _JsonObject:
    """Return *payload* with Devin's ``tool_response`` exposed as ``tool_output``.

    :mod:`omnigent.native.native_policy_hook` reads a ``PostToolUse`` result from
    ``tool_output`` (Claude Code / Codex spelling). Devin sends the richer
    ``tool_response`` object (``{"success", "output", "error"}``) instead, so
    normalize here rather than teaching the shared seam a third spelling.
    Prefers the human-readable ``output``, falling back to ``error`` and then
    the whole object so a policy that matches on result text still sees it.
    """
    if "tool_output" in payload:
        return payload
    response = payload.get("tool_response")
    if response is None:
        return payload
    normalized = dict(payload)
    if isinstance(response, dict):
        output = response.get("output")
        error = response.get("error")
        if isinstance(output, str) and output:
            normalized["tool_output"] = output
        elif isinstance(error, str) and error:
            normalized["tool_output"] = error
        else:
            normalized["tool_output"] = json.dumps(response, ensure_ascii=False)
    else:
        normalized["tool_output"] = response
    return normalized


def _emit(output: _JsonObject | None) -> int:
    """Write a hook verdict to stdout and return the process exit code."""
    if output is None:
        return 0
    sys.stdout.write(json.dumps(output))
    sys.stdout.flush()
    return 0


def _evaluate_policy(
    hook_event: str,
    payload: _JsonObject,
    *,
    server_url: str,
    session_id: str,
) -> _JsonObject | None:
    """Evaluate Omnigent policy for *payload* and return Devin hook output."""
    normalized = _normalize_tool_result(payload) if hook_event == _POST_TOOL_USE else payload
    eval_request = hook_payload_to_evaluation_request(hook_event, normalized)
    if eval_request is None:
        # Not policy-relevant (unknown event, or an mcp__omnigent__* tool the
        # relay path already gated) — no opinion.
        return None
    headers = policy_hook_request_headers()
    url = f"{server_url.rstrip('/')}/v1/sessions/{session_id}/policies/evaluate"
    response, error = post_evaluate_with_retry(
        url,
        headers,
        eval_request,
        _EVALUATE_READ_TIMEOUT_S,
        _HOOK_LABEL,
        reauth=policy_hook_reauth(server_url, headers),
    )
    if response is None:
        return fail_closed_hook_output(hook_event, error)
    try:
        body = response.json()
    except ValueError:
        return fail_closed_hook_output(hook_event, "server returned a non-JSON body")
    if not isinstance(body, dict):
        return fail_closed_hook_output(hook_event, "server returned a non-object body")
    return evaluation_response_to_hook_output(hook_event, body)


def _devin_output_for_policy_verdict(
    hook_event: str,
    verdict: _JsonObject | None,
) -> _JsonObject | None:
    """Translate a shared-seam hook verdict into Devin's output contract.

    The shared seam emits Claude Code's shapes, which Devin accepts verbatim for
    ``PreToolUse`` (``hookSpecificOutput.permissionDecision``, live-verified to
    block even under ``--permission-mode bypass``) and for ``UserPromptSubmit``
    (top-level ``decision``/``reason``). ``PostToolUse`` deny is surfaced as
    ``additionalContext``, which Devin also reads. So the translation is the
    identity — this indirection exists to document that equivalence and to give
    a single place to diverge if Devin's contract drifts.
    """
    del hook_event
    return verdict


def _post_elicitation(
    body: _JsonObject,
    *,
    server_url: str,
    session_id: str,
    endpoint: str,
) -> _JsonObject | None:
    """Wait for a web answer, refreshing expired auth once; defer on failure."""
    url = f"{server_url.rstrip('/')}/v1/sessions/{session_id}/hooks/{endpoint}"
    timeout = httpx.Timeout(_PERMISSION_READ_TIMEOUT_S, connect=_PERMISSION_CONNECT_TIMEOUT_S)
    headers = policy_hook_request_headers()
    reauth = policy_hook_reauth(server_url, headers)
    try:
        for attempt in range(2):
            with httpx.Client(headers=headers, timeout=timeout) as client:
                response = client.post(url, json=body)
                if attempt == 0 and _is_login_redirect_or_unauthorized(response):
                    refreshed = reauth()
                    if refreshed:
                        headers = refreshed
                        print(
                            f"omnigent devin {endpoint} hook: Omnigent auth "
                            "expired; re-minted token and retrying",
                            file=sys.stderr,
                        )
                        continue
                response.raise_for_status()
                if not response.content:
                    # Endpoint timed out waiting for a human — defer to the TUI.
                    return None
                result = response.json()
                break
        else:  # pragma: no cover — the loop always breaks or returns
            return None
    except (httpx.HTTPError, ValueError) as exc:
        print(
            f"omnigent devin {endpoint} hook: {exc}; deferring to the Devin prompt",
            file=sys.stderr,
        )
        return None
    return result if isinstance(result, dict) else None


def _mirror_permission_request(
    payload: _JsonObject,
    *,
    server_url: str,
    session_id: str,
) -> _JsonObject | None:
    """Mirror consent and translate the web verdict to Devin's decision shape.

    An unreachable server leaves Devin's TUI prompt authoritative. The separate
    PreToolUse policy gate has already enforced any policy denial.
    """
    tool_name = payload.get("tool_name")
    if not isinstance(tool_name, str) or not tool_name:
        return None
    body = _post_elicitation(
        {**payload, "_omnigent_elicitation_id": f"elicit_devin_{secrets.token_hex(16)}"},
        server_url=server_url,
        session_id=session_id,
        endpoint="permission-request",
    )
    if body is None:
        return None
    specific = body.get("hookSpecificOutput")
    decision = specific.get("decision") if isinstance(specific, dict) else None
    behavior = decision.get("behavior") if isinstance(decision, dict) else None
    reason = decision.get("message") if isinstance(decision, dict) else None
    if behavior == "allow":
        return {"decision": "approve"}
    if behavior == "deny":
        output: _JsonObject = {"decision": "deny"}
        if isinstance(reason, str) and reason:
            output["reason"] = reason
        return output
    return None


def _mirror_user_question(
    payload: _JsonObject,
    *,
    server_url: str,
    session_id: str,
) -> _JsonObject | None:
    """Collect native question answers in Chat before Devin opens its picker."""
    tool_input = payload.get("tool_input")
    questions = question_payload(tool_input)
    if questions is None or not isinstance(tool_input, dict):
        return None
    result = _post_elicitation(
        {
            "elicitation_id": f"elicit_devin_question_{secrets.token_hex(16)}",
            "agent": "Devin",
            "operation_type": "question",
            "message": "Devin is asking a question",
            "policy_name": "devin_native_ask_question",
            "ask_user_question": questions,
        },
        server_url=server_url,
        session_id=session_id,
        endpoint="native-permission-request",
    )
    if result is None:
        return None
    action = result.get("action")
    if not isinstance(action, str) or action not in {"accept", "decline", "cancel"}:
        return None
    specific: _JsonObject = {
        "hookEventName": _PRE_TOOL_USE,
        "permissionDecision": "deny",
        "permissionDecisionReason": "The user canceled the question.",
    }
    if action == "accept":
        content = result.get("content")
        if not isinstance(content, dict):
            specific["permissionDecisionReason"] = "No readable question answers were received."
        else:
            try:
                updated_input = answered_question_input(tool_input, content)
            except ValueError as exc:
                specific["permissionDecisionReason"] = str(exc)
            else:
                specific = {
                    "hookEventName": _PRE_TOOL_USE,
                    "permissionDecision": "allow",
                    "updatedInput": updated_input,
                }
    return {"hookSpecificOutput": specific}


def _record_event(bridge_dir: Path | None, payload: _JsonObject) -> None:
    """Append *payload* to the hook log, tolerating an unwritable bridge dir."""
    if bridge_dir is None:
        return
    try:
        from omnigent.harnesses.devin_native.bridge import record_hook_event

        record_hook_event(bridge_dir, payload)
    except OSError as exc:
        print(f"omnigent devin hook: could not record event: {exc}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    """Run the Devin hook: record the event, then apply policy / elicitation.

    :param argv: ``[bridge_dir]`` — the per-session bridge directory. Passed by
        the wrapper script written by
        :func:`omnigent.harnesses.devin_native.bridge.write_hook_wrapper`.
    :returns: Process exit code. Always ``0``; a block is expressed through the
        JSON verdict on stdout rather than exit 2, so the reason text reaches
        the model.
    """
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    bridge_dir = Path(raw_argv[0]) if raw_argv else None
    payload = _read_payload()
    if payload is None:
        return 0
    hook_event = payload.get("hook_event_name")
    if not isinstance(hook_event, str):
        return 0

    server_url = os.environ.get(_SERVER_URL_ENV, "").strip()
    session_id = os.environ.get(_SESSION_ID_ENV, "").strip()
    governed = bool(server_url and session_id)

    # UserPromptSubmit is the one event whose verdict must reach the transcript
    # with it: the forwarder mirrors the prompt and opens the turn from this
    # record, and a blocked prompt never reaches the model — so no Stop follows
    # and the turn would stay open forever. Every failure mode here blocks too
    # (`fail_closed_hook_output` fails CLOSED on PHASE_REQUEST), so recording
    # verdict-blind is wrong even when the server is merely unreachable.
    if governed and hook_event == _USER_PROMPT_SUBMIT:
        verdict = _evaluate_policy(
            hook_event, payload, server_url=server_url, session_id=session_id
        )
        if isinstance(verdict, dict) and verdict.get("decision") == "block":
            from omnigent.harnesses.devin_native.bridge import DEVIN_POLICY_BLOCKED_KEY

            _record_event(bridge_dir, {**payload, DEVIN_POLICY_BLOCKED_KEY: True})
        else:
            _record_event(bridge_dir, payload)
        return _emit(_devin_output_for_policy_verdict(hook_event, verdict))

    # Everything else records first: the transcript must survive a policy or
    # network failure, and these events describe work that already happened.
    _record_event(bridge_dir, payload)

    if hook_event == _SESSION_END and bridge_dir is not None and session_id:
        # The session is over: remove this session's always-on agent rule so it
        # does not load into a later Devin run in the same workspace. Gated on the
        # ownership stamp, so a rule another session owns is left alone.
        from omnigent.harnesses.devin_native.bridge import (
            read_devin_workspace_hint,
            remove_devin_agent_rule_if_owned,
        )

        workspace = read_devin_workspace_hint(bridge_dir)
        if workspace is not None:
            remove_devin_agent_rule_if_owned(workspace, session_id)
        return 0

    if not governed:
        # Not a governed session (e.g. the user ran `devin` outside Omnigent with
        # a stale config). Record-only; never block.
        return 0

    if hook_event == _PERMISSION_REQUEST:
        return _emit(
            _mirror_permission_request(payload, server_url=server_url, session_id=session_id)
        )
    if hook_event in _POLICY_EVENTS:
        verdict = _evaluate_policy(
            hook_event, payload, server_url=server_url, session_id=session_id
        )
        if (
            verdict is None
            and hook_event == _PRE_TOOL_USE
            and payload.get("tool_name") == "ask_user_question"
        ):
            verdict = _mirror_user_question(payload, server_url=server_url, session_id=session_id)
        return _emit(_devin_output_for_policy_verdict(hook_event, verdict))
    return 0


if __name__ == "__main__":  # pragma: no cover — subprocess entrypoint
    raise SystemExit(main())
