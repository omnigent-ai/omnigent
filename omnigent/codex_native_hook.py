"""Codex Code hook entrypoint for native Omnigent policy enforcement.

Registered as the ``PreToolUse`` / ``PostToolUse`` command hook in the
per-session private ``CODEX_HOME`` (see
:mod:`omnigent.codex_native_app_server`). Codex spawns this module as
a short subprocess before/after each built-in tool call, piping the hook
payload on stdin and reading a verdict on stdout. The conversion to/from
the Omnigent policy schema is shared with the Claude-native hook via
:mod:`omnigent.native_policy_hook`.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
from pathlib import Path

import httpx

from omnigent.codex_native_bridge import (
    read_bridge_state,
    read_codex_config_model,
    read_policy_hook_config,
)
from omnigent.native_policy_hook import (
    evaluation_response_to_hook_output,
    hook_payload_to_evaluation_request,
)

# Budget for the policy evaluation POST. Normally a quick
# request/reply, but a TOOL_CALL ASK now parks server-side (URL-based
# elicitation) until a human resolves it via the approve URL, so the
# client must wait as long as the permission long-poll. Held at one
# day; the server caps the real wait via the deciding policy's
# ``ask_timeout``. Kept in lockstep with the Claude-native hook's
# ``_EVALUATE_POLICY_TIMEOUT_S``.
_EVALUATE_POLICY_TIMEOUT_S = 86400.0


def main(argv: list[str] | None = None) -> int:
    """
    Dispatch a Codex hook subcommand.

    :param argv: Optional argv override excluding program name.
        ``None`` reads :data:`sys.argv`.
    :returns: Process exit code. Always ``0`` — blocking verdicts are
        expressed via the JSON written to stdout, never via exit code,
        so a hook failure never wedges Codex.
    """
    raw_argv = sys.argv[1:] if argv is None else argv
    if raw_argv and raw_argv[0] == "evaluate-policy":
        return _main_evaluate_policy(raw_argv[1:])
    print(
        f"omnigent codex hook: unknown subcommand {raw_argv[:1]!r}",
        file=sys.stderr,
    )
    return 0


def _main_evaluate_policy(argv: list[str]) -> int:
    """
    Evaluate a Codex ``PreToolUse`` / ``PostToolUse`` /
    ``UserPromptSubmit`` hook against Omnigent policies.

    Reads the hook JSON payload from stdin, converts it into the
    proto-compatible ``EvaluationRequest`` schema via
    :func:`omnigent.native_policy_hook.hook_payload_to_evaluation_request`,
    POSTs to ``/v1/sessions/{id}/policies/evaluate``, and converts the
    ``EvaluationResponse`` back into Codex's hook output format
    (``hookSpecificOutput.permissionDecision`` for PreToolUse;
    ``additionalContext`` warning for PostToolUse; top-level
    ``decision: "block"`` for UserPromptSubmit — the request-phase gate
    for native sessions, which drops the prompt before the model runs).

    On any transport or lookup failure the hook returns exit 0 with no
    output, which Codex treats as "no opinion". This is the deliberate
    fail-open behavior shared with the Claude-native hook: a network
    blip must not block every tool call. The complementary fail-loud
    guard — asserting the hook is actually registered and trusted — lives
    at session startup in :mod:`omnigent.codex_native_app_server`, not
    here, because a silently-skipped hook cannot report its own absence.

    :param argv: CLI argv after the ``evaluate-policy`` subcommand,
        e.g. ``["--bridge-dir", "/tmp/x"]``.
    :returns: Process exit code. Always ``0``.
    """
    args = _parse_evaluate_policy_args(argv)
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        print(f"omnigent codex evaluate-policy hook: malformed JSON: {exc}", file=sys.stderr)
        return 0
    if not isinstance(payload, dict):
        print("omnigent codex evaluate-policy hook: expected JSON object", file=sys.stderr)
        return 0

    bridge_dir = Path(args.bridge_dir)
    state = read_bridge_state(bridge_dir)
    if state is None:
        return 0
    session_id = state.session_id

    config = read_policy_hook_config(bridge_dir)
    if config is None:
        # No Omnigent server configured for this session — nothing to enforce.
        return 0
    ap_server_url = config.get("ap_server_url")
    if not isinstance(ap_server_url, str) or not ap_server_url:
        return 0
    headers: dict[str, str] = {}
    raw_headers = config.get("ap_auth_headers")
    if isinstance(raw_headers, dict):
        headers = {str(key): str(value) for key, value in raw_headers.items()}

    hook_event = payload.get("hook_event_name", "")
    eval_request = hook_payload_to_evaluation_request(hook_event, payload)
    if eval_request is None:
        # Unrecognized hook event or an mcp__omnigent__* tool (relay-enforced).
        return 0

    # Stamp the live model from this session's config.toml (what an in-TUI
    # ``/model`` writes) onto the request so the cost-budget gate evaluates
    # against the user's CURRENT selection. Reading it here — synchronously,
    # the instant the tool call is gated — is race-free, unlike relying on the
    # forwarder's async ``model_override`` mirror which can lag behind the
    # tool call within the same turn. The server prefers this over its own
    # resolved model (see ``PolicyEngine._inject_model``).
    # hook_payload_to_evaluation_request always returns an event dict with a
    # "context" dict, so index it directly (fail loud if that contract ever
    # changes rather than silently dropping these).
    context = eval_request["event"]["context"]
    # Stamp the harness so policies can tailor messages to codex-native's
    # model-switch surface (terminal /model only — no web picker).
    context["harness"] = "codex-native"
    model = read_codex_config_model(bridge_dir)
    if model:
        context["model"] = model

    session_component = urllib.parse.quote(session_id, safe="")
    url = f"{ap_server_url.rstrip('/')}/v1/sessions/{session_component}/policies/evaluate"
    try:
        with httpx.Client(
            headers=headers, timeout=httpx.Timeout(_EVALUATE_POLICY_TIMEOUT_S)
        ) as client:
            resp = client.post(url, json=eval_request)
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        print(
            f"omnigent codex evaluate-policy hook: Omnigent request failed: {exc}",
            file=sys.stderr,
        )
        return 0
    if not resp.content:
        return 0

    try:
        eval_response = resp.json()
    except json.JSONDecodeError:
        return 0

    hook_output = evaluation_response_to_hook_output(hook_event, eval_response)
    if hook_output is not None:
        sys.stdout.write(json.dumps(hook_output))
    return 0


def _parse_evaluate_policy_args(argv: list[str]) -> argparse.Namespace:
    """
    Parse ``evaluate-policy`` hook arguments.

    :param argv: CLI argv excluding program name and subcommand, e.g.
        ``["--bridge-dir", "/tmp/x"]``.
    :returns: Parsed namespace with a ``bridge_dir`` attribute.
    """
    parser = argparse.ArgumentParser(prog="python -m omnigent.codex_native_hook evaluate-policy")
    parser.add_argument("--bridge-dir", required=True)
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
