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
  gated tool to ``/v1/sessions/{id}/hooks/native-permission-request`` (the
  vendor-agnostic native-permission endpoint shared with qwen/kiro/hermes/goose
  — the payload's ``agent`` / ``policy_name`` label the card "Kimi", and the
  server publishes the approval card and long-polls for the web verdict), then
  types the answer back into kimi's prompt via ``inject_approval_keystroke``
  (option digit + Enter:
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
    policy_hook_reauth,
    post_evaluate_with_retry,
    read_relay_policy_config,
    relay_policy_evaluate_url,
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
# Cap on the preview string POSTed to the card (server truncates too).
_PREVIEW_MAX = 1024


def _url_component(value: str) -> str:
    """Percent-encode one URL path component (slashes escaped)."""
    return urllib.parse.quote(value, safe="")


def _content_preview(tool_input: object) -> str | None:
    """Render a compact card preview from the gated tool's input.

    Mirrors qwen's ``_preview_for``: a shell tool's ``command`` is the most
    useful single line, otherwise the JSON-encoded input, truncated to
    ``_PREVIEW_MAX``. Returns ``None`` when there is no usable input so the
    field is omitted rather than echoing the bare tool name.
    """
    if not isinstance(tool_input, dict):
        return None
    command = tool_input.get("command")
    if isinstance(command, str) and command.strip():
        return command.strip()[:_PREVIEW_MAX]
    try:
        return json.dumps(tool_input, ensure_ascii=False)[:_PREVIEW_MAX]
    except (TypeError, ValueError):
        return None


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

    hook_event = payload.get("hook_event_name", "")
    if not isinstance(hook_event, str):
        return 0
    eval_request = hook_payload_to_evaluation_request(hook_event, payload)
    if eval_request is None:
        return 0

    context = eval_request["event"]["context"]
    context["harness"] = _HARNESS

    def _fail_closed(detail: str | None = None) -> int:
        out = fail_closed_hook_output(hook_event, detail)
        if out is not None:
            sys.stdout.write(json.dumps(out))
        return 0

    # Prefer the relay; fall back to direct server call.
    relay = read_relay_policy_config(bridge_dir)
    if relay:
        relay_url, relay_token, _sid = relay
        url = relay_policy_evaluate_url(relay_url)
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {relay_token}",
        }
        reauth = None
    else:
        config = read_hook_config(bridge_dir)
        ap_server_url = config.get("ap_server_url")
        if not isinstance(ap_server_url, str) or not ap_server_url:
            return 0
        headers = _headers_from_config(config)
        session_component = _url_component(session_id)
        url = f"{ap_server_url.rstrip('/')}/v1/sessions/{session_component}/policies/evaluate"
        reauth = policy_hook_reauth(ap_server_url, headers)

    resp, api_error = post_evaluate_with_retry(
        url,
        headers,
        eval_request,
        _EVALUATE_POLICY_TIMEOUT_S,
        "kimi evaluate-policy hook",
        reauth=reauth,
    )
    if resp is None or not resp.content:
        return _fail_closed(api_error or (reauth.failure_reason if reauth else None))
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

    1. POST the gated tool to ``/v1/sessions/{id}/hooks/native-permission-request``
       — the vendor-agnostic native-permission endpoint (shared with
       qwen/kiro/hermes/goose); the payload labels the card "Kimi", and the
       server publishes the ``response.elicitation_request`` approval card and
       long-polls for the web verdict.
    2. Answer kimi's permission menu from the web verdict via
       :func:`inject_approval_keystroke` (option digit + Enter): ``accept``
       types :data:`APPROVE_KEY` "Approve once"; ``cancel`` types
       :data:`DENY_KEY` "Reject". ``decline`` types NOTHING — the server
       forwards an Escape that rejects the menu, so a second keystroke would
       race it.

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
        # Stable re-attach id so a severed long-poll re-parks the SAME
        # elicitation. ``agent`` / ``policy_name`` label the card "Kimi".
        "elicitation_id": f"elicit_kimi_{secrets.token_hex(16)}",
        "agent": "Kimi",
        "policy_name": "kimi_native_permission",
        "message": f"Kimi wants to call **{tool_name}**",
        "operation_type": tool_name,
    }
    preview = _content_preview(payload.get("tool_input"))
    if preview is not None:
        body["content_preview"] = preview

    url = (
        f"{ap_server_url.rstrip('/')}/v1/sessions/"
        f"{_url_component(session_id)}/hooks/native-permission-request"
    )
    verdict = _request_web_approval(url, headers, body)
    if verdict is None or verdict == "decline":
        # Inject nothing. No verdict leaves kimi's own TUI prompt for manual
        # answer. On an explicit decline the server best-effort forwards an
        # Escape (kimi 0.41.0: Escape on an open menu IS Reject): when it lands
        # it rejects and closes the menu, so a second keystroke here would race
        # it. If that forward fails or reaches no runner the menu stays open for
        # manual terminal input — the same fail-safe as no verdict, never a
        # silent approve. cancel gets no forward, so it still types the digit.
        return 0
    key = APPROVE_KEY if verdict == "accept" else DENY_KEY
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

    :returns: the verdict action (``"accept"`` / ``"decline"`` / ``"cancel"``),
        or ``None`` on timeout (server returns an empty 200), transport failure,
        or an unparseable verdict — all of which fall back to kimi's own TUI
        prompt.
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
    """Extract the web verdict action from the native-permission response.

    The vendor-agnostic endpoint returns an ``ElicitationResult``
    (``{"action": "accept"|"decline"|"cancel"}``); the action is returned
    verbatim so the caller can act on ``decline`` (which the server answers
    with a forwarded Escape) differently from ``cancel``. Anything else is no
    verdict.
    """
    if not isinstance(data, dict):
        return None
    action = data.get("action")
    if action in ("accept", "decline", "cancel"):
        return action
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
