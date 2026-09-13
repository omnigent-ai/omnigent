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

#. **Mirror Devin's own consent prompt.** ``PermissionRequest`` fires when Devin
   would show its TUI approval prompt. Rather than stranding that prompt in a
   pane the web user may not be looking at, it is republished as an Omnigent
   elicitation and the user's answer is relayed back.

Kept deliberately import-light: it runs on every tool call, so it must not pull
in the runner or server stacks.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx

from omnigent.native.native_policy_hook import (
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


def _mirror_permission_request(
    payload: _JsonObject,
    *,
    server_url: str,
    session_id: str,
) -> _JsonObject | None:
    """Publish Devin's approval prompt to the web UI and await the verdict.

    POSTs to the session's ``/hooks/permission-request`` endpoint, which raises
    an Omnigent elicitation (rendering the web UI's approval card) and
    long-polls until the user answers. The endpoint replies in Claude Code's
    ``hookSpecificOutput.decision.behavior`` shape, translated here into Devin's
    ``{"decision": "approve"|"deny"}``.

    Returns ``None`` on any failure or timeout, which Devin reads as "no
    opinion" and falls back to its own TUI prompt — the correct fail-ask
    behavior for an unreachable or unattended web UI. Note this is a *consent*
    mirror, not an enforcement gate: enforcement already happened in
    ``PreToolUse``, which fails closed.
    """
    tool_name = payload.get("tool_name")
    if not isinstance(tool_name, str) or not tool_name:
        return None
    url = f"{server_url.rstrip('/')}/v1/sessions/{session_id}/hooks/permission-request"
    timeout = httpx.Timeout(_PERMISSION_READ_TIMEOUT_S, connect=_PERMISSION_CONNECT_TIMEOUT_S)
    try:
        with httpx.Client(headers=policy_hook_request_headers(), timeout=timeout) as client:
            response = client.post(url, json=payload)
            response.raise_for_status()
            if not response.content:
                # Endpoint timed out waiting for a human — defer to the TUI.
                return None
            body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        print(
            f"omnigent devin permission-request hook: {exc}; deferring to the Devin prompt",
            file=sys.stderr,
        )
        return None
    if not isinstance(body, dict):
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

    # 1. Record first: the transcript must survive a policy/network failure.
    if bridge_dir is not None:
        try:
            from omnigent.harnesses.devin_native.bridge import record_hook_event

            record_hook_event(bridge_dir, payload)
        except OSError as exc:
            print(f"omnigent devin hook: could not record event: {exc}", file=sys.stderr)

    server_url = os.environ.get(_SERVER_URL_ENV, "").strip()
    session_id = os.environ.get(_SESSION_ID_ENV, "").strip()
    if not server_url or not session_id:
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
        return _emit(_devin_output_for_policy_verdict(hook_event, verdict))
    return 0


if __name__ == "__main__":  # pragma: no cover — subprocess entrypoint
    raise SystemExit(main())
