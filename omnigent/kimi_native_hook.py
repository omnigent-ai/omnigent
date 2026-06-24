"""Kimi Code hook commands for the native Omnigent wrapper.

Registered into a per-session ``config.toml`` ``[[hooks]]`` array (see
:mod:`omnigent.kimi_native_credentials`) so the running ``kimi`` TUI invokes
them. Kimi spawns each hook with ``shell: true``, feeds the event JSON on
stdin, and reads the decision back from stdout as
``{"hookSpecificOutput": {"permissionDecision": ..., "permissionDecisionReason": ...}}``
(``permissionDecision == "deny"`` blocks the tool). Two subcommands:

- ``evaluate-policy`` — the ``PreToolUse`` deny-gate. Mirrors
  :func:`omnigent.claude_native_hook._main_evaluate_policy`: it converts the
  Kimi hook payload into an Omnigent ``EvaluationRequest`` (the snake-cased
  Kimi fields ``tool_name`` / ``tool_input`` / ``hook_event_name`` line up
  with :func:`omnigent.native_policy_hook.hook_payload_to_evaluation_request`),
  POSTs to ``/v1/sessions/{id}/policies/evaluate``, and emits a ``deny`` only
  for a constraining ``POLICY_ACTION_DENY`` verdict. ``ALLOW`` (the engine's
  no-match default) emits nothing, so kimi's own in-TUI approval prompt still
  runs — Omnigent enforces its deny-policy without silencing the user's
  consent. Fails CLOSED (deny) when an already-governed session can't reach a
  verdict, matching the claude-native gate.

- ``permission-request`` — the interactive web-UI approval. Kimi fires
  ``PermissionRequest`` fire-and-forget (it does NOT read this hook's output —
  approval is answered by kimi's own TUI menu), so the hook cannot return an
  honored decision. Instead it drives a real web-UI Approve/Deny: it POSTs the
  gated tool to ``/v1/sessions/{id}/hooks/permission-request`` (the same
  endpoint claude-native uses — the server publishes the approval card and
  long-polls for the web verdict), then types the answer back into kimi's
  prompt via ``inject_approval_keystroke`` (option digit + Enter:
  :data:`~omnigent.kimi_native_bridge.APPROVE_KEY` "Approve once" /
  :data:`~omnigent.kimi_native_bridge.DENY_KEY` "Reject"). Fail-safe: on no
  verdict (timeout / unreachable / already answered in the terminal) it injects
  nothing and kimi's own TUI prompt stands. Never blocks the TUI.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import urllib.parse
from pathlib import Path

import httpx

from omnigent.kimi_native_bridge import (
    APPROVE_KEY,
    DENY_KEY,
    inject_approval_keystroke,
    read_active_session_id,
    read_hook_config,
)
from omnigent.native_policy_hook import (
    evaluation_response_to_hook_output,
    fail_closed_hook_output,
    hook_payload_to_evaluation_request,
    post_evaluate_with_retry,
)

# PreToolUse evaluations are normally a quick request/reply. (Unlike
# claude-native, a TOOL_CALL ASK does NOT park here — kimi owns the ask via
# its own TUI prompt, so the policy layer only ever DENY/ALLOWs for kimi.)
_EVALUATE_POLICY_TIMEOUT_S = 70.0
# Short timeout for the keystroke-injection tmux round-trip; never delay the TUI.
_SURFACE_TIMEOUT_S = 10.0
# Long-poll budget for the web approval verdict — the human may take a while.
# On timeout the server returns an empty 200 and we fall back to kimi's own TUI
# prompt (manual approval in the terminal).
_PERMISSION_REQUEST_TIMEOUT_S = 3600.0
_HARNESS = "kimi-native"


def _url_component(value: str) -> str:
    """Percent-encode one URL path component (slashes escaped)."""
    return urllib.parse.quote(value, safe="")


def _headers_from_config(config: dict[str, object]) -> dict[str, str]:
    """Extract replayable auth headers from the bridge hook config."""
    raw = config.get("ap_auth_headers")
    if not isinstance(raw, dict):
        return {}
    return {str(key): str(value) for key, value in raw.items()}


def _read_stdin_payload() -> dict[str, object] | None:
    """Parse the hook event JSON from stdin; ``None`` when unusable."""
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        print(f"omnigent kimi hook: malformed JSON: {exc}", file=sys.stderr)
        return None
    if not isinstance(payload, dict):
        print("omnigent kimi hook: expected JSON object", file=sys.stderr)
        return None
    return payload


def _main_evaluate_policy(argv: list[str]) -> int:
    """Evaluate a kimi ``PreToolUse`` hook against Omnigent policies.

    Reads the hook payload from stdin, POSTs an ``EvaluationRequest`` to
    ``/v1/sessions/{id}/policies/evaluate``, and writes kimi's hook decision
    to stdout. Only ``POLICY_ACTION_DENY`` produces a ``deny``; everything
    else emits nothing ("no opinion") so kimi's own approval prompt still
    fires. An already-governed session that cannot obtain a verdict fails
    CLOSED with a ``deny`` (this hook is the sole Omnigent enforcement point
    for kimi tool calls).

    :param argv: CLI argv after the ``evaluate-policy`` subcommand.
    :returns: Always ``0`` — verdicts are expressed via JSON, not exit codes.
    """
    args = _parse_bridge_dir_args(argv, "evaluate-policy")
    payload = _read_stdin_payload()
    if payload is None:
        return 0
    bridge_dir = Path(args.bridge_dir)
    session_id = read_active_session_id(bridge_dir)
    if not session_id:
        return 0  # not a governed session — no opinion
    config = read_hook_config(bridge_dir)
    ap_server_url = config.get("ap_server_url")
    if not isinstance(ap_server_url, str) or not ap_server_url:
        return 0
    headers = _headers_from_config(config)

    hook_event = payload.get("hook_event_name", "")
    if not isinstance(hook_event, str):
        return 0
    eval_request = hook_payload_to_evaluation_request(hook_event, payload)
    if eval_request is None:
        # Unrecognized event or an mcp__omnigent__* tool already gated on the
        # relay path — no policy to evaluate here.
        return 0

    # hook_payload_to_evaluation_request always returns an event with a
    # "context" dict; index it directly (fail loud if that contract changes).
    context = eval_request["event"]["context"]
    context["harness"] = _HARNESS

    def _fail_closed() -> int:
        out = fail_closed_hook_output(hook_event)
        if out is not None:
            sys.stdout.write(json.dumps(out))
        return 0

    url = (
        f"{ap_server_url.rstrip('/')}"
        f"/v1/sessions/{_url_component(session_id)}/policies/evaluate"
    )
    resp = post_evaluate_with_retry(
        url, headers, eval_request, _EVALUATE_POLICY_TIMEOUT_S, "kimi evaluate-policy hook"
    )
    if resp is None or not resp.content:
        return _fail_closed()
    try:
        eval_response = resp.json()
    except json.JSONDecodeError:
        print("omnigent kimi evaluate-policy hook: malformed Omnigent response", file=sys.stderr)
        return _fail_closed()

    hook_output = evaluation_response_to_hook_output(hook_event, eval_response)
    if hook_output is not None:
        sys.stdout.write(json.dumps(hook_output))
    return 0


def _main_permission_request(argv: list[str]) -> int:
    """Mirror a kimi ``PermissionRequest`` to the web UI and inject the verdict.

    Kimi fires this hook **fire-and-forget** — it answers approval in its own
    TUI and does NOT read the hook's stdout — so we cannot return a decision it
    honors. Instead we drive an interactive web-UI approval and type the answer
    back into kimi's prompt:

    1. POST the gated tool to ``/v1/sessions/{id}/hooks/permission-request`` —
       the server publishes the standard ``response.elicitation_request``
       approval card and long-polls for the web verdict (the very endpoint
       claude-native uses).
    2. On ``allow`` / ``deny``, inject the matching kimi permission-menu option
       digit + Enter into the TUI pane via :func:`inject_approval_keystroke`
       (:data:`APPROVE_KEY` "Approve once" / :data:`DENY_KEY` "Reject").

    Fail-safe: on no verdict (timeout / server unreachable / the prompt was
    already answered in the terminal) it injects nothing and kimi's own TUI
    prompt stands for manual approval. Always returns 0 (kimi ignores output).

    :param argv: CLI argv after the ``permission-request`` subcommand.
    :returns: Always ``0``.
    """
    args = _parse_bridge_dir_args(argv, "permission-request")
    payload = _read_stdin_payload()
    if payload is None:
        return 0
    bridge_dir = Path(args.bridge_dir)
    session_id = read_active_session_id(bridge_dir)
    if not session_id:
        return 0
    config = read_hook_config(bridge_dir)
    ap_server_url = config.get("ap_server_url")
    if not isinstance(ap_server_url, str) or not ap_server_url:
        return 0
    headers = _headers_from_config(config)

    tool_name = payload.get("tool_name")
    if not isinstance(tool_name, str) or not tool_name:
        return 0
    body: dict[str, object] = {
        "tool_name": tool_name,
        # Stable re-attach id so a severed long-poll re-parks the SAME
        # elicitation (mirrors the claude permission hook).
        "_omnigent_elicitation_id": f"elicit_kimi_{secrets.token_hex(16)}",
    }
    tool_input = payload.get("tool_input")
    if isinstance(tool_input, dict):
        body["tool_input"] = tool_input

    url = (
        f"{ap_server_url.rstrip('/')}/v1/sessions/"
        f"{_url_component(session_id)}/hooks/permission-request"
    )
    verdict = _request_web_approval(url, headers, body)
    if verdict is None:
        # No web verdict: leave kimi's own TUI prompt for manual approval.
        return 0
    key = APPROVE_KEY if verdict == "allow" else DENY_KEY
    try:
        inject_approval_keystroke(bridge_dir, key=key, timeout_s=_SURFACE_TIMEOUT_S)
    except RuntimeError as exc:
        print(
            f"omnigent kimi permission-request hook: keystroke inject failed: {exc}",
            file=sys.stderr,
        )
    return 0


def _request_web_approval(
    url: str, headers: dict[str, str], body: dict[str, object]
) -> str | None:
    """POST the approval card and long-poll for the web verdict.

    :returns: ``"allow"`` / ``"deny"``, or ``None`` on timeout (server returns
        an empty 200), transport failure, or an unparseable verdict — all of
        which fall back to kimi's own TUI prompt.
    """
    timeout = httpx.Timeout(_PERMISSION_REQUEST_TIMEOUT_S, connect=_SURFACE_TIMEOUT_S)
    try:
        with httpx.Client(headers=headers, timeout=timeout) as client:
            resp = client.post(url, json=body)
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        print(
            f"omnigent kimi permission-request hook: approval request failed: {exc}",
            file=sys.stderr,
        )
        return None
    if not resp.content:
        return None
    try:
        data = resp.json()
    except json.JSONDecodeError:
        return None
    return _verdict_from_response(data)


def _verdict_from_response(data: object) -> str | None:
    """Extract ``"allow"`` / ``"deny"`` from the PermissionRequest hook response.

    The endpoint returns Claude's PermissionRequest contract
    (``hookSpecificOutput.decision.behavior``), with ``permissionDecision`` as a
    fallback shape. Any persistent-allow variant (``allow_*``) maps to allow.
    """
    if not isinstance(data, dict):
        return None
    hook_output = data.get("hookSpecificOutput")
    if not isinstance(hook_output, dict):
        return None
    decision = hook_output.get("decision")
    behavior = decision.get("behavior") if isinstance(decision, dict) else None
    raw = behavior if isinstance(behavior, str) else hook_output.get("permissionDecision")
    if not isinstance(raw, str):
        return None
    low = raw.lower()
    if low.startswith("allow") or low in ("approve", "approved", "accept"):
        return "allow"
    if low in ("deny", "reject", "rejected", "block"):
        return "deny"
    return None


def _parse_bridge_dir_args(argv: list[str], prog: str) -> argparse.Namespace:
    """Parse the shared ``--bridge-dir`` argument for a hook subcommand."""
    parser = argparse.ArgumentParser(prog=f"omnigent.kimi_native_hook {prog}")
    parser.add_argument("--bridge-dir", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Dispatch a kimi hook subcommand.

    :param argv: Process argv tail (defaults to ``sys.argv[1:]``).
    :returns: Process exit code.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("usage: kimi_native_hook {evaluate-policy|permission-request} ...", file=sys.stderr)
        return 2
    subcommand, rest = args[0], args[1:]
    if subcommand == "evaluate-policy":
        return _main_evaluate_policy(rest)
    if subcommand == "permission-request":
        return _main_permission_request(rest)
    print(f"omnigent kimi hook: unknown subcommand {subcommand!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
