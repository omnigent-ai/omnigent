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
from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
    from omnigent.codex_native_app_server import CodexAppServerClient

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
    if raw_argv and raw_argv[0] == "route-turn":
        return _main_route_turn(raw_argv[1:])
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


def _main_route_turn(argv: list[str]) -> int:
    """
    Route the model this session runs on, from its first real prompt.

    The in-harness half of first-message routing (see
    :mod:`omnigent.runner.turn_routing`), registered as a second
    ``UserPromptSubmit`` command alongside the policy gate. On every
    prompt submit, in order:

    1. Fast skip on ``<bridge_dir>/turn_routing_done`` **when it names this
       session** — no output, no network. The authoritative gate is the
       endpoint's routing-decision check; this file only saves the round
       trip, and a marker another conversation in the same bridge dir wrote
       is not ours to skip on.
    2. POST ``{session_id, prompt, harness, turn_id, model}`` to the
       advertised loopback ``route-turn`` endpoint. ``model`` comes from
       the hook payload, which tracks the LIVE thread model —
       ``config.toml`` reports the stale launch model.
    3. On a routed verdict: check the pick against this pane's live
       ``model/list``, switch the thread with ``thread/settings/update``
       (codex binds the turn's model before this hook runs, so the switch
       lands from the next turn), write the marker, and BLOCK the prompt.
       The runner then replays it as a normal user turn, which runs on the
       routed model.

    Fails open everywhere: an absent advertisement, an unreachable
    endpoint, an unroutable verdict, a pick this pane's gateway does not
    serve, or a failed switch all exit ``0`` with no output, and the prompt
    runs untouched on the current model.

    :param argv: CLI argv after the ``route-turn`` subcommand, e.g.
        ``["--bridge-dir", "/tmp/x", "--harness", "codex-native"]``.
    :returns: Process exit code. Always ``0`` — the block is expressed via
        the JSON on stdout, never via the exit code.
    """
    from omnigent.runner.turn_routing import (
        ADVERTISEMENT_FILE,
        HOOK_REQUEST_TIMEOUT_S,
        ROUTE_PATH_TEMPLATE,
        trace_turn_routing,
        turn_routing_marker_present,
    )

    parser = argparse.ArgumentParser(prog="python -m omnigent.codex_native_hook route-turn")
    parser.add_argument("--bridge-dir", required=True)
    parser.add_argument("--harness", default="codex-native")
    args = parser.parse_args(argv)
    bridge_dir = Path(args.bridge_dir)

    # Every prompt submit is traced, including the ones that fall open. A
    # session that "just never routed" is otherwise indistinguishable from
    # one the harness never fired the hook for at all.
    raw = sys.stdin.read()

    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError:
        trace_turn_routing(bridge_dir, "fail-open", "malformed hook payload")
        return 0
    if not isinstance(payload, dict):
        trace_turn_routing(bridge_dir, "fail-open", "hook payload is not an object")
        return 0
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        trace_turn_routing(bridge_dir, "skip", "no prompt text on this submit")
        return 0

    from omnigent.inner.hook_scripts.subagent_router import read_router_endpoint

    endpoint = read_router_endpoint(bridge_dir, filename=ADVERTISEMENT_FILE)
    if endpoint is None:
        trace_turn_routing(bridge_dir, "fail-open", f"no usable {ADVERTISEMENT_FILE}")
        return 0
    state = read_bridge_state(bridge_dir)
    session_id = endpoint.session_id or (state.session_id if state is not None else None)
    if not session_id:
        trace_turn_routing(bridge_dir, "fail-open", "no session id to route")
        return 0

    # The marker is checked here, after the session id is known, because it is
    # scoped to a session: this bridge dir is shared with whichever
    # conversation a ``/clear`` rotation or a fork left behind, and their
    # verdict is not ours. Still zero network on the fast path.
    if turn_routing_marker_present(bridge_dir, session_id):
        trace_turn_routing(bridge_dir, "skip", "marker present")
        return 0

    body = {
        "harness": args.harness,
        "prompt": prompt,
        "turn_id": _payload_str(payload, "turn_id"),
        # The payload's model tracks thread/settings/update; config.toml does not.
        "model": _payload_str(payload, "model"),
    }
    url = endpoint.url + ROUTE_PATH_TEMPLATE.format(
        session_id=urllib.parse.quote(session_id, safe="")
    )
    decision = _post_json(url, endpoint.token, body, HOOK_REQUEST_TIMEOUT_S)
    if decision is None:
        # Not the endpoint URL: it comes out of the advertisement that also
        # holds the bearer token, and this trace is world-readable stderr.
        trace_turn_routing(bridge_dir, "fail-open", "no verdict from the turn router")
        return 0
    model = decision.get("model")
    if decision.get("action") != "route" or not isinstance(model, str) or not model:
        rationale = decision.get("rationale")
        trace_turn_routing(
            bridge_dir,
            "allow",
            f"{rationale if isinstance(rationale, str) else ''} "
            f"(terminal={bool(decision.get('terminal'))})",
        )
        if decision.get("terminal"):
            # Nothing will route this session again, so stop asking. Covers the
            # no-op verdict too (the pick equals the live model): terminal and
            # unblocking, so the prompt runs where it already was.
            _write_marker(bridge_dir, session_id, decision)
        return 0

    declined = _apply_thread_model(bridge_dir, model)
    if declined is not None:
        # No marker: the prompt is about to run, and the marker is what
        # tells the runner to replay it. Writing one here would replay a
        # prompt that already ran. The server-side pin still keeps the
        # next prompt from re-routing.
        trace_turn_routing(bridge_dir, "fail-open", declined)
        print(
            f"omnigent codex route-turn hook: {declined}; "
            "letting the prompt run on the current model",
            file=sys.stderr,
        )
        return 0
    # Marker after the switch and before the block, so its presence means
    # both "the routed model is applied" and "this prompt was dropped, you
    # owe it a replay".
    if not _write_marker(bridge_dir, session_id, decision):
        trace_turn_routing(bridge_dir, "fail-open", "could not write the block marker")
        return 0
    trace_turn_routing(bridge_dir, "route", f"blocked and switched to {model}")
    sys.stdout.write(
        json.dumps(
            {
                "decision": "block",
                "reason": f"Smart Routing selected {model}; rerunning your message on it.",
            }
        )
    )
    return 0


def _payload_str(payload: dict[str, object], key: str) -> str | None:
    """
    Read an optional string field from a hook payload.

    :param payload: Decoded hook payload.
    :param key: Field name, e.g. ``"turn_id"``.
    :returns: The value, or ``None`` when absent or not a non-empty string.
    """
    value = payload.get(key)
    return value if isinstance(value, str) and value else None


def _write_marker(bridge_dir: Path, session_id: str, decision: dict[str, object]) -> bool:
    """
    Write the session-scoped turn-routing marker file.

    :param bridge_dir: Native Codex bridge directory.
    :param session_id: Session the verdict belongs to — a later conversation
        sharing this dir must not fast-skip on it.
    :param decision: The verdict, for its ``decision_id``.
    :returns: ``True`` when the marker is on disk.
    """
    from omnigent.runner.turn_routing import write_turn_routing_marker

    decision_id = decision.get("decision_id")
    if write_turn_routing_marker(
        bridge_dir,
        session_id=session_id,
        decision_id=decision_id if isinstance(decision_id, str) else None,
    ):
        return True
    print(
        f"omnigent codex route-turn hook: could not write the marker in {bridge_dir}",
        file=sys.stderr,
    )
    return False


def _post_json(
    url: str,
    token: str,
    body: dict[str, object],
    timeout: float,
) -> dict[str, object] | None:
    """
    POST one JSON body to the loopback endpoint.

    :param url: Fully-qualified loopback URL.
    :param token: Bearer token from the advertisement.
    :param body: Request body.
    :param timeout: Socket timeout in seconds.
    :returns: The decoded response object, or ``None`` on any transport or
        decode failure (callers treat that as "allow unrouted").
    """
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            decoded = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _apply_thread_model(bridge_dir: Path, model: str) -> str | None:
    """
    Switch the live Codex thread onto *model*, if this pane can serve it.

    ``thread/settings/update`` is the thread-level switch (the same one the
    web picker drives through the executor); the app-server accepts a
    second concurrent client while a turn is in flight, so the hook can
    fire it from inside its own synchronous window. The accepted switch is
    mirrored into ``config.toml`` the way the executor does, so the
    cost-budget gate reads the routed model rather than the launch one.

    The routed id is resolved against this pane's live ``model/list`` first
    (see :mod:`omnigent.codex_model_vocabulary`), which is both the spelling
    translation and the reachability check. The routing verdict comes from a
    server-side gateway map that can go stale, and switching a pane onto a
    model its gateway cannot serve fails silently at the next turn — so a
    routed id no row names declines the switch instead, and the pane keeps
    running on its own model.

    :param bridge_dir: Native Codex bridge directory.
    :param model: Routed model id, e.g. ``"databricks-gpt-5-6-luna"``.
    :returns: ``None`` when Codex accepted the switch, else a short reason
        the switch was declined, for the caller's trace and stderr note.
    """
    import asyncio

    from omnigent.codex_model_vocabulary import codex_reachable_model_slug
    from omnigent.codex_native_app_server import client_for_transport
    from omnigent.codex_native_bridge import write_codex_config_model
    from omnigent.runner.turn_routing import SETTINGS_UPDATE_TIMEOUT_S

    state = read_bridge_state(bridge_dir)
    if state is None:
        return "no bridge state to switch through"

    # The spelling codex accepted, mirrored into config.toml below so the
    # file and the live thread never disagree about the model.
    applied: str | None = None
    declined: str | None = None

    async def _switch() -> None:
        nonlocal applied, declined
        client = client_for_transport(state.socket_path, client_name="omnigent-route-turn-hook")
        await client.connect()
        try:
            rows = await _list_codex_models(client)
            if rows is None:
                declined = "could not read this pane's model catalog"
                return
            slug = codex_reachable_model_slug(model, rows)
            if slug is None:
                declined = f"routed model not in this pane's catalog ({model})"
                return
            await client.request(
                "thread/settings/update",
                {"threadId": state.thread_id, "model": slug},
            )
            applied = slug
        finally:
            await client.close()

    try:
        asyncio.run(asyncio.wait_for(_switch(), timeout=SETTINGS_UPDATE_TIMEOUT_S))
    except Exception as exc:  # noqa: BLE001 - any failure means "leave the model alone"
        return f"thread/settings/update failed: {exc}"
    if declined is not None:
        return declined
    if applied is None:
        return f"could not switch to {model}"
    if not write_codex_config_model(bridge_dir, applied):
        print(
            f"omnigent codex route-turn hook: could not mirror {applied} into config.toml",
            file=sys.stderr,
        )
    return None


async def _list_codex_models(client: CodexAppServerClient) -> list[dict[str, object]] | None:
    """
    Read this session's codex model catalog over an open app-server client.

    Hidden rows are included: they are still switchable, and a routed model
    listed only there is reachable all the same.

    :param client: Connected app-server client.
    :returns: Raw ``model/list`` rows, or ``None`` when the call failed —
        which is not the same as an empty catalog, and the caller declines
        the switch rather than reading "no rows" as "not reachable".
    """
    rows: list[dict[str, object]] = []
    cursor: str | None = None
    try:
        while True:
            params: dict[str, object] = {"includeHidden": True}
            if cursor is not None:
                params["cursor"] = cursor
            response = await client.request("model/list", params)
            result = response.get("result")
            if not isinstance(result, dict):
                break
            rows.extend(row for row in result.get("data") or () if isinstance(row, dict))
            cursor = result.get("nextCursor")
            if not isinstance(cursor, str) or not cursor:
                break
    except Exception as exc:  # noqa: BLE001 - an unreadable catalog means "do not switch"
        print(
            f"omnigent codex route-turn hook: model/list failed: {exc}",
            file=sys.stderr,
        )
        return None
    return rows


if __name__ == "__main__":
    raise SystemExit(main())
