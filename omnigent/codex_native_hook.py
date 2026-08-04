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
    policy_hook_reauth,
    post_evaluate_with_retry,
    read_relay_policy_config,
    relay_policy_evaluate_url,
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
    if raw_argv and raw_argv[0] == "spike-userprompt":
        return _main_spike_userprompt(raw_argv[1:])
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

    hook_event = payload.get("hook_event_name", "")
    eval_request = hook_payload_to_evaluation_request(hook_event, payload)
    if eval_request is None:
        # Unrecognized hook event or an mcp__omnigent__* tool (relay-enforced).
        return 0

    # Stamp the live model from this session's config.toml (what an in-TUI
    # ``/model`` writes) onto the request so the cost-budget gate evaluates
    # against the user's CURRENT selection.
    context = eval_request["event"]["context"]
    context["harness"] = "codex-native"
    model = read_codex_config_model(bridge_dir)
    if model:
        context["model"] = model

    def _fail_closed(detail: str | None = None) -> int:
        out = fail_closed_hook_output(hook_event, detail)
        if out is not None:
            sys.stdout.write(json.dumps(out))
        return 0

    # Prefer the relay (non-expiring local token); fall back to direct server
    # call when the relay isn't up yet (first-call race) or not configured.
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
        config = read_policy_hook_config(bridge_dir)
        if config is None:
            return 0
        ap_server_url = config.get("ap_server_url")
        if not isinstance(ap_server_url, str) or not ap_server_url:
            return 0
        raw_headers = config.get("ap_auth_headers")
        headers = {}
        if isinstance(raw_headers, dict):
            headers = {str(k): str(v) for k, v in raw_headers.items()}
        session_component = urllib.parse.quote(session_id, safe="")
        url = f"{ap_server_url.rstrip('/')}/v1/sessions/{session_component}/policies/evaluate"
        reauth = policy_hook_reauth(ap_server_url, headers)

    resp, api_error = post_evaluate_with_retry(
        url,
        headers,
        eval_request,
        _EVALUATE_POLICY_TIMEOUT_S,
        "codex evaluate-policy hook",
        reauth=reauth,
    )
    if resp is None:
        return _fail_closed(api_error or (reauth.failure_reason if reauth else None))
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


def _main_spike_userprompt(argv: list[str]) -> int:
    """
    SPIKE ONLY (in-harness routing S1/S2). Not product code.

    Logs every invocation to ``<bridge_dir>/spike_hook_log.jsonl`` (S2:
    does codex fire ``UserPromptSubmit`` for app-server ``turn/start``
    turns?) and, when ``<bridge_dir>/spike_switch_model`` exists, fires
    ``thread/settings/update`` on the live thread from inside the hook's
    synchronous window before allowing the prompt (S1: does the
    just-submitted turn pick up the new model?).

    :param argv: CLI argv after the subcommand.
    :returns: Always ``0`` (allow).
    """
    import time

    parser = argparse.ArgumentParser(prog="python -m omnigent.codex_native_hook spike-userprompt")
    parser.add_argument("--bridge-dir", required=True)
    args = parser.parse_args(argv)
    bridge_dir = Path(args.bridge_dir)
    log_path = bridge_dir / "spike_hook_log.jsonl"

    raw = sys.stdin.read()
    record: dict[str, object] = {
        "ts": time.time(),
        "iso": time.strftime("%H:%M:%S", time.localtime()),
        "argv": argv,
        "cwd": str(Path.cwd()),
        "raw_stdin": raw[:4000],
    }
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        payload = {}
        record["parse_error"] = str(exc)
    if isinstance(payload, dict):
        record["payload_keys"] = sorted(payload.keys())
        record["hook_event_name"] = payload.get("hook_event_name")

    marker = bridge_dir / "spike_switch_model"
    if marker.exists():
        target = marker.read_text(encoding="utf-8").strip()
        record["spike_mode"] = "settings_update"
        record["spike_target_model"] = target
        record["model_before"] = read_codex_config_model(bridge_dir)
        started = time.monotonic()
        try:
            record["settings_update_result"] = _spike_settings_update(bridge_dir, target)
        except Exception as exc:  # noqa: BLE001 - spike diagnostics
            record["settings_update_error"] = f"{type(exc).__name__}: {exc}"
        record["settings_update_seconds"] = round(time.monotonic() - started, 3)
        # One-shot: remove the marker so the next prompt is a clean control.
        try:
            marker.unlink()
        except OSError:
            pass

    block_marker = bridge_dir / "spike_block"
    if block_marker.exists():
        record["spike_blocked"] = True
        sys.stdout.write(
            json.dumps({"decision": "block", "reason": "SPIKE: Smart Routing is picking a model…"})
        )
        try:
            block_marker.unlink()
        except OSError:
            pass

    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, default=str) + "\n")
    return 0


def _spike_settings_update(bridge_dir: Path, target_model: str) -> object:
    """
    SPIKE ONLY. Fire ``thread/settings/update`` on the live thread.

    :param bridge_dir: Native Codex bridge directory.
    :param target_model: Model id to switch the thread to.
    :returns: A JSON-safe summary of the app-server exchange.
    """
    import asyncio

    from omnigent.codex_native_app_server import client_for_transport

    state = read_bridge_state(bridge_dir)
    if state is None:
        return {"error": "no bridge state"}

    async def _run() -> object:
        client = client_for_transport(state.socket_path, client_name="omnigent-spike-hook")
        await client.connect()
        try:
            resp = await client.request(
                "thread/settings/update",
                {"threadId": state.thread_id, "model": target_model},
            )
            return {
                "thread_id": state.thread_id,
                "transport": state.socket_path,
                "active_turn_id": state.active_turn_id,
                "response": resp,
            }
        finally:
            await client.close()

    return asyncio.run(asyncio.wait_for(_run(), timeout=20))


if __name__ == "__main__":
    raise SystemExit(main())
