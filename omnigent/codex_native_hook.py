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

from omnigent.codex_native_bridge import (
    read_bridge_state,
    read_codex_config_model,
    read_policy_hook_config,
)
from omnigent.native_policy_hook import (
    evaluation_response_to_hook_output,
    fail_closed_hook_output,
    hook_payload_to_evaluation_request,
    post_evaluate_with_retry,
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

    Failure handling is phase-aware (mirroring the runner-side default
    from PR #163), shared with the Claude-native hook. Once the session is
    known to be governed (an active session id and a configured
    ``ap_server_url``) and the round-trip to ``/policies/evaluate`` cannot
    yield a usable verdict — server unreachable, non-2xx, or an empty /
    malformed body — a ``PreToolUse`` (``PHASE_TOOL_CALL``) call fails
    CLOSED with a ``deny`` (this hook is the sole enforcement point for
    native tools), while ``UserPromptSubmit`` and ``PostToolUse`` fail
    OPEN. Conditions that mean the session simply is not governed — no
    bridge state, no ``ap_server_url``, an unparseable payload, or an
    ``mcp__omnigent__*`` tool already gated on the relay path — still
    return exit 0 with no output ("no opinion") so non-Omnigent tool calls
    are never blocked. The complementary fail-loud guard — asserting the
    hook is actually registered and trusted — lives at session startup in
    :mod:`omnigent.codex_native_app_server`, not here, because a
    silently-skipped hook cannot report its own absence.

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

    # The session is governed (bridge state + ap_server_url) and we have a
    # policy-relevant event: from here a failure to obtain a usable verdict
    # fails CLOSED for the tool-call gate (see ``fail_closed_hook_output``).
    def _fail_closed() -> int:
        out = fail_closed_hook_output(hook_event)
        if out is not None:
            sys.stdout.write(json.dumps(out))
        return 0

    session_component = urllib.parse.quote(session_id, safe="")
    url = f"{ap_server_url.rstrip('/')}/v1/sessions/{session_component}/policies/evaluate"
    resp = post_evaluate_with_retry(
        url, headers, eval_request, _EVALUATE_POLICY_TIMEOUT_S, "codex evaluate-policy hook"
    )
    if resp is None:
        return _fail_closed()
    if not resp.content:
        print("omnigent codex evaluate-policy hook: empty Omnigent response", file=sys.stderr)
        return _fail_closed()

    try:
        eval_response = resp.json()
    except json.JSONDecodeError:
        print(
            "omnigent codex evaluate-policy hook: malformed Omnigent response",
            file=sys.stderr,
        )
        return _fail_closed()

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
