"""Unit tests for the shared native-harness policy hook converters."""

from __future__ import annotations

import contextlib
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from omnigent.native import native_policy_hook
from omnigent.native.native_policy_hook import (
    _is_login_redirect_or_unauthorized,
    evaluation_response_to_hook_output,
    fail_ask_hook_output,
    fail_closed_hook_output,
    hook_payload_to_evaluation_request,
    post_evaluate_with_retry,
)


def test_pre_tool_use_maps_to_phase_tool_call() -> None:
    """
    A PreToolUse payload becomes a PHASE_TOOL_CALL EvaluationRequest.

    The tool name and arguments must land in ``event.data`` so the
    server's policy engine can match on them. A failure here means the
    server would evaluate an empty/garbled tool call and likely ALLOW
    everything.
    """
    result = hook_payload_to_evaluation_request(
        "PreToolUse",
        {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}},
    )
    assert result is not None
    event = result["event"]
    assert event["type"] == "PHASE_TOOL_CALL"
    # The command must survive into args, or the policy can't inspect it.
    assert event["data"] == {"name": "Bash", "arguments": {"command": "rm -rf /"}}


def test_post_tool_use_maps_to_phase_tool_result() -> None:
    """
    A PostToolUse payload becomes a PHASE_TOOL_RESULT EvaluationRequest.

    The result text goes in ``event.data.result`` and the originating
    tool name/args ride along in ``request_data`` so a TOOL_RESULT
    policy can correlate output to the call that produced it. A failure
    means output-inspection policies would see no result or no tool.
    """
    result = hook_payload_to_evaluation_request(
        "PostToolUse",
        {
            "tool_name": "Bash",
            "tool_input": {"command": "cat /etc/passwd"},
            "tool_output": "root:x:0:0:...",
        },
    )
    assert result is not None
    event = result["event"]
    assert event["type"] == "PHASE_TOOL_RESULT"
    assert event["data"]["result"] == "root:x:0:0:..."
    # request_data carries the originating call so result policies can
    # correlate output back to the tool + args that produced it.
    assert event["request_data"] == {
        "name": "Bash",
        "arguments": {"command": "cat /etc/passwd"},
    }


@pytest.mark.parametrize("hook_event", ["PreToolUse", "PostToolUse"])
def test_omnigent_mcp_tools_are_skipped(hook_event: str) -> None:
    """
    Omnigent MCP tools return None and are never sent to /policies/evaluate.

    Omnigent MCP tool calls are already policy-checked by the relay path
    (ProxyMcpManager → Omnigent /mcp endpoint → _evaluate_tool_call_policy).
    If this guard regressed, every MCP tool call would be evaluated
    twice — once via the relay, once via this hook.
    """
    result = hook_payload_to_evaluation_request(
        hook_event,
        {"tool_name": "mcp__omnigent__list_comments", "tool_input": {}, "tool_output": "x"},
    )
    # None signals the caller to skip the POST entirely.
    assert result is None


@pytest.mark.parametrize(
    "hook_event,expected_type",
    [("PreToolUse", "PHASE_TOOL_CALL"), ("PostToolUse", "PHASE_TOOL_RESULT")],
)
def test_connector_native_mcp_tools_are_evaluated(hook_event: str, expected_type: str) -> None:
    """
    Connector-native MCP tools must not be skipped by the native pre-call hook.

    Tools such as ``mcp__github__*`` are injected by the connector layer and
    do not round-trip through Omnigent's MCP proxy, so this hook is their
    TOOL_CALL/TOOL_RESULT policy enforcement site.
    """
    result = hook_payload_to_evaluation_request(
        hook_event,
        {
            "tool_name": "mcp__github__create_issue",
            "tool_input": {"title": "blocked?"},
            "tool_output": "created",
        },
    )
    assert result is not None
    event = result["event"]
    assert event["type"] == expected_type
    if hook_event == "PreToolUse":
        assert event["data"] == {
            "name": "mcp__github__create_issue",
            "arguments": {"title": "blocked?"},
        }
    else:
        assert event["request_data"] == {
            "name": "mcp__github__create_issue",
            "arguments": {"title": "blocked?"},
        }


def test_unknown_hook_event_returns_none() -> None:
    """
    A non-tool hook event (e.g. SessionStart) is not policy-relevant.

    Returning None makes the hook a no-op for events that carry no tool
    call. A failure (returning a request) would POST garbage to the
    server for every lifecycle event.
    """
    assert hook_payload_to_evaluation_request("SessionStart", {"tool_name": "Bash"}) is None


@pytest.mark.parametrize(
    "action,expected_decision",
    [
        ("POLICY_ACTION_DENY", "deny"),
        ("POLICY_ACTION_ASK", "deny"),
    ],
)
def test_pre_tool_use_response_maps_action_to_permission_decision(
    action: str, expected_decision: str
) -> None:
    """
    A constraining proto action maps to the matching permissionDecision.

    DENY→deny. ASK→deny too: ASK is resolved server-side now (URL-based
    elicitation — ``POST /policies/evaluate`` holds the gate and returns
    a hard ALLOW/DENY), so the hook should never see ASK; if it does, it
    must fail closed with ``deny`` rather than the old ``defer`` (which
    handed control to a possibly-permissive harness permission_mode,
    re-opening the bypass). ALLOW is deliberately NOT here — it returns
    None (see test_pre_tool_use_allow_returns_none). A wrong mapping here
    would, e.g., let a DENY verdict run the tool, defeating enforcement.
    """
    output = evaluation_response_to_hook_output("PreToolUse", {"result": action})
    assert output is not None
    hook_specific = output["hookSpecificOutput"]
    assert hook_specific["hookEventName"] == "PreToolUse"
    assert hook_specific["permissionDecision"] == expected_decision


def test_pre_tool_use_allow_returns_none() -> None:
    """
    A PreToolUse ALLOW yields no opinion (None), not ``"allow"``.

    ALLOW is the policy engine's default verdict when no policy matches a
    tool call. Emitting ``permissionDecision: "allow"`` would auto-approve
    the tool in the native harness, suppressing its own permission prompt
    — and, for Claude Code, the ``PermissionRequest`` hook that routes
    that prompt to the web UI. Returning None keeps the policy gate and
    the user's own consent gate independent: the policy layer may block
    (DENY) or demand approval (ASK), but must never silence the harness's
    native prompt. Regression guard for "claude-native elicitations stop
    showing in the web UI" once a PreToolUse policy hook was wired in.
    """
    output = evaluation_response_to_hook_output("PreToolUse", {"result": "POLICY_ACTION_ALLOW"})
    assert output is None


def test_pre_tool_use_deny_includes_reason() -> None:
    """
    A DENY verdict surfaces the policy reason as permissionDecisionReason.

    The reason is what the user/agent sees explaining the block. A
    failure (missing reason) would block tools with no explanation.
    """
    output = evaluation_response_to_hook_output(
        "PreToolUse",
        {"result": "POLICY_ACTION_DENY", "reason": "rm blocked by admin policy"},
    )
    assert output is not None
    hook_specific = output["hookSpecificOutput"]
    assert hook_specific["permissionDecision"] == "deny"
    assert hook_specific["permissionDecisionReason"] == "rm blocked by admin policy"


def test_pre_tool_use_unknown_action_returns_none() -> None:
    """
    An unrecognized/unspecified action yields no opinion (None).

    POLICY_ACTION_UNSPECIFIED (e.g. no agent / no policies) must not be
    coerced into allow or deny — returning None lets the harness apply
    its own default. A failure would fabricate a verdict from no policy.
    """
    output = evaluation_response_to_hook_output(
        "PreToolUse", {"result": "POLICY_ACTION_UNSPECIFIED"}
    )
    assert output is None


def test_post_tool_use_deny_maps_to_additional_context() -> None:
    """
    A PostToolUse DENY becomes an additionalContext warning, not a block.

    PostToolUse fires after the tool ran, so it cannot block — the
    verdict is surfaced to the model as context. A failure would either
    drop the warning or wrongly attempt to block an already-run tool.
    """
    output = evaluation_response_to_hook_output(
        "PostToolUse",
        {"result": "POLICY_ACTION_DENY", "reason": "Sensitive data in output"},
    )
    assert output is not None
    hook_specific = output["hookSpecificOutput"]
    assert hook_specific["hookEventName"] == "PostToolUse"
    # The warning text must carry the reason so the model sees why.
    assert hook_specific["additionalContext"] == "[Policy violation] Sensitive data in output"


def test_post_tool_use_allow_returns_none() -> None:
    """
    A PostToolUse ALLOW produces no output (nothing to inject).

    Only DENY warrants an additionalContext warning. A failure
    (emitting output on ALLOW) would spam the model with empty context
    on every successful tool result.
    """
    output = evaluation_response_to_hook_output("PostToolUse", {"result": "POLICY_ACTION_ALLOW"})
    assert output is None


def test_user_prompt_submit_maps_to_phase_request() -> None:
    """
    A UserPromptSubmit payload becomes a PHASE_REQUEST EvaluationRequest.

    The prompt text must land in ``event.data.text`` because the server's
    ``_build_evaluation_context`` reads REQUEST content from ``data.text``
    (falling back to ``data.content``). If the prompt were dropped, the
    request-phase gate would evaluate empty content and ALLOW everything.
    """
    result = hook_payload_to_evaluation_request(
        "UserPromptSubmit",
        {"prompt": "delete the prod database"},
    )
    assert result is not None
    event = result["event"]
    assert event["type"] == "PHASE_REQUEST"
    assert event["data"] == {"text": "delete the prod database"}
    # A context dict must exist so the per-harness hook can stamp model/harness.
    assert event["context"] == {}


def test_user_prompt_submit_missing_prompt_yields_empty_text() -> None:
    """
    A UserPromptSubmit payload with no ``prompt`` still produces a request.

    The text falls back to an empty string rather than ``None`` so the
    server always receives a well-formed REQUEST event.
    """
    result = hook_payload_to_evaluation_request("UserPromptSubmit", {})
    assert result is not None
    assert result["event"]["data"] == {"text": ""}


@pytest.mark.parametrize("action", ["POLICY_ACTION_DENY", "POLICY_ACTION_ASK"])
def test_user_prompt_submit_blocking_actions_emit_decision_block(action: str) -> None:
    """
    DENY (and a stray ASK) block the prompt via top-level ``decision``.

    UserPromptSubmit uses the top-level ``decision`` / ``reason`` contract
    (NOT ``permissionDecision``) — both harnesses parse ``decision: "block"``
    to drop the prompt before the model sees it. ASK is meant to be resolved
    server-side (``_hold_native_ask_gate``), so if the hook ever sees it, it
    must fail closed by blocking rather than letting the prompt through.
    """
    output = evaluation_response_to_hook_output(
        "UserPromptSubmit",
        {"result": action, "reason": "no prod mutations"},
    )
    assert output is not None
    # Top-level decision/reason, not hookSpecificOutput.permissionDecision.
    assert output == {"decision": "block", "reason": "no prod mutations"}


def test_user_prompt_submit_block_defaults_reason() -> None:
    """
    A block with no reason still carries a non-empty reason.

    Both harnesses drop a block whose reason is empty (the block is treated
    as invalid), so a missing reason must be defaulted or the gate would
    silently fail open.
    """
    output = evaluation_response_to_hook_output(
        "UserPromptSubmit", {"result": "POLICY_ACTION_DENY"}
    )
    assert output == {"decision": "block", "reason": "Denied by policy"}


@pytest.mark.parametrize("action", ["POLICY_ACTION_ALLOW", "POLICY_ACTION_UNSPECIFIED"])
def test_user_prompt_submit_non_blocking_actions_return_none(action: str) -> None:
    """
    ALLOW and the no-match default proceed with no output.

    Returning None lets the prompt reach the model. Unlike PreToolUse there
    is no separate user-consent gate on a prompt to preserve, so ALLOW need
    not emit anything.
    """
    output = evaluation_response_to_hook_output("UserPromptSubmit", {"result": action})
    assert output is None


def test_fail_closed_pre_tool_use_denies() -> None:
    """
    An unobtainable verdict on PreToolUse fails CLOSED with ``deny``.

    PreToolUse is the authoritative pre-execution gate for native tools —
    the sole enforcement point for connector-native ``mcp__*`` tools and
    native Bash/Write/Edit — so a verdict that cannot be fetched must deny
    rather than silently let the call through (issue #536).
    """
    output = fail_closed_hook_output("PreToolUse")
    assert output is not None
    hook_specific = output["hookSpecificOutput"]
    assert hook_specific["hookEventName"] == "PreToolUse"
    assert hook_specific["permissionDecision"] == "deny"
    # A deny is inert without a reason on the consuming harnesses, so one
    # must always be present.
    assert hook_specific["permissionDecisionReason"]


def test_fail_closed_user_prompt_submit_fails_closed() -> None:
    """
    ``UserPromptSubmit`` (``PHASE_REQUEST``) fails CLOSED on an unobtainable verdict.

    The request gate is the sole pre-turn enforcement point for native
    sessions — a server hiccup must not let an over-budget or otherwise-
    blocked request proceed. The output must be a top-level
    ``decision: "block"`` with a non-empty reason (both Claude Code and
    Codex drop a block with an empty reason).
    """
    output = fail_closed_hook_output("UserPromptSubmit")
    assert output is not None
    assert output["decision"] == "block"
    assert output["reason"]


def test_fail_closed_post_tool_use_fails_open() -> None:
    """
    ``PostToolUse`` (``PHASE_TOOL_RESULT``) fails OPEN (``None``).

    The tool has already executed by this point, so denying would only
    block an already-incurred side effect. Mirrors the runner-side
    ``FAIL_CLOSED_PHASES``.
    """
    assert fail_closed_hook_output("PostToolUse") is None


def test_fail_closed_unknown_event_fails_open() -> None:
    """
    An unrecognized hook event fails OPEN (``None``), not closed.

    Only the exact ``PreToolUse`` event denies; any novel event name added
    by a future harness must fall through to "no opinion" rather than
    accidentally blocking — the conservative default for an unknown gate.
    """
    assert fail_closed_hook_output("SomeNewEvent") is None


def test_fail_ask_pre_tool_use_returns_ask() -> None:
    """
    ``fail_ask_hook_output`` returns ``permissionDecision: "ask"`` for ``PreToolUse``.

    Using ``"ask"`` explicitly prompts the user regardless of permission mode
    (unlike ``None``, which fails open in ``bypassPermissions``/``acceptEdits``).
    """
    output = fail_ask_hook_output("PreToolUse")
    assert output is not None
    hook = output["hookSpecificOutput"]
    assert hook["hookEventName"] == "PreToolUse"
    assert hook["permissionDecision"] == "ask"
    assert hook["permissionDecisionReason"]


def test_fail_ask_pre_tool_use_with_detail_includes_detail() -> None:
    """Detail string is appended to the ask reason."""
    output = fail_ask_hook_output("PreToolUse", "server connection refused")
    assert output is not None
    assert "server connection refused" in output["hookSpecificOutput"]["permissionDecisionReason"]


def test_fail_ask_user_prompt_submit_still_fails_closed() -> None:
    """
    ``fail_ask_hook_output`` still blocks ``UserPromptSubmit``.

    The request gate is the sole pre-turn enforcement point; a server
    hiccup must not silently allow an over-budget or blocked request.
    """
    output = fail_ask_hook_output("UserPromptSubmit")
    assert output is not None
    assert output["decision"] == "block"
    assert output["reason"]


def test_fail_ask_post_tool_use_fails_open() -> None:
    """``PostToolUse`` fails open under fail-ask, same as fail-closed."""
    assert fail_ask_hook_output("PostToolUse") is None


def test_fail_ask_unknown_event_fails_open() -> None:
    """Unknown events fail open under fail-ask."""
    assert fail_ask_hook_output("SomeNewEvent") is None


def _resp(status: int, location: str | None = None) -> httpx.Response:
    """Build a fake response for re-auth classification tests."""
    headers = {"Location": location} if location else {}
    return httpx.Response(status, headers=headers, request=httpx.Request("POST", "https://ap/x"))


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (_resp(401), True),
        # Databricks Apps returns 403 "Invalid Token" for an expired bearer.
        (_resp(403), True),
        (_resp(302, "https://w.example.com/oidc/oauth2/v2.0/authorize"), True),
        (_resp(302, "https://omnigents.example.databricksapps.com/.auth/callback"), True),
        # Unrelated redirect / success must NOT trigger a wasted token round-trip.
        (_resp(302, "https://w.example.com/some/other/page"), False),
        (_resp(302, None), False),
        (_resp(200), False),
        (_resp(503), False),
    ],
)
def test_is_login_redirect_or_unauthorized_classifies_reauth_signals(
    response: httpx.Response, expected: bool
) -> None:
    """
    401 and an Apps OAuth-login 302 are re-auth signals; nothing else is.

    The Databricks Apps front door bounces an *expired* bearer with a
    ``302 → /oidc/`` (or ``/.auth/``), NOT a ``401`` — so a hook that only
    checked ``401`` silently failed closed once the one-shot token lapsed.
    This is the classifier that lets the hook re-mint instead.
    """
    assert _is_login_redirect_or_unauthorized(response) is expected


def _make_redirect_then_ok_client(
    seen_headers: list[dict[str, str]],
    *,
    redirect: httpx.Response,
    ok: httpx.Response,
) -> type:
    """Build an httpx.Client stub: redirect on attempt 1, ``ok`` thereafter."""

    class _Client:
        def __init__(self, *, headers: dict[str, str], timeout: object, **_kwargs: object) -> None:
            del timeout
            self._headers = headers

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *args: object) -> None:
            del args

        def post(self, url: str, *, json: dict[str, object]) -> httpx.Response:
            del url, json
            seen_headers.append(dict(self._headers))
            return redirect if len(seen_headers) == 1 else ok

    return _Client


@pytest.mark.parametrize(
    ("url", "expected_trust_env"),
    [
        ("http://127.0.0.1:6767/v1/sessions/s/policies/evaluate", False),
        ("http://localhost:6767/v1/sessions/s/policies/evaluate", False),
        ("http://[::1]:6767/v1/sessions/s/policies/evaluate", False),
        ("https://omnigent.example.com/v1/sessions/s/policies/evaluate", True),
    ],
)
def test_post_evaluate_with_retry_bypasses_proxies_only_for_loopback_targets(
    monkeypatch: pytest.MonkeyPatch,
    url: str,
    expected_trust_env: bool,
) -> None:
    """Local policy callbacks bypass proxies; remote deployments retain them."""
    captured_trust_env: list[bool] = []

    class _Client:
        def __init__(
            self,
            *,
            headers: dict[str, str],
            timeout: object,
            trust_env: bool,
        ) -> None:
            del headers, timeout
            captured_trust_env.append(trust_env)

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *args: object) -> None:
            del args

        def post(self, request_url: str, *, json: dict[str, object]) -> httpx.Response:
            del json
            return httpx.Response(
                200,
                text='{"result":"POLICY_ACTION_ALLOW"}',
                request=httpx.Request("POST", request_url),
            )

    monkeypatch.setattr(native_policy_hook.httpx, "Client", _Client)

    response, error = post_evaluate_with_retry(url, {}, {"event": {}}, 5.0, "evaluate-policy hook")

    assert response is not None
    assert error is None
    assert captured_trust_env == [expected_trust_env]


_ALLOW_BODY = b'{"result":"POLICY_ACTION_ALLOW"}'


@contextlib.contextmanager
def _http_endpoint(status: int, body: bytes) -> Iterator[tuple[str, list[str]]]:
    """
    Serve *status*/*body* to every POST on a loopback port, recording request targets.

    Doubles as a forward-proxy stand-in: a proxied request arrives with the
    absolute URL as its target, a direct one with just the path.

    :param status: HTTP status to answer with.
    :param body: Response body.
    :returns: ``(base_url, targets)`` -- the targets list fills as requests land.
    """
    targets: list[str] = []

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: object) -> None:
            del args

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            targets.append(self.path)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", targets
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _point_proxy_env_at(monkeypatch: pytest.MonkeyPatch, proxy_url: str) -> None:
    """Export *proxy_url* the way a proxied host does: every scheme, no loopback exemption."""
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, proxy_url)
        monkeypatch.setenv(name.lower(), proxy_url)
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)


def test_post_evaluate_with_retry_loopback_callback_ignores_ambient_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A loopback callback reaches the local server even when the host exports a proxy.

    On a proxied host the runner-local relay is ``127.0.0.1``, which the proxy
    resolves against itself and refuses. Sending that callback through the
    proxy made every prompt fail closed; it must connect directly and never
    consult the proxy at all.
    """
    # Keep the fail-closed retry loop short so the buggy path fails in seconds.
    monkeypatch.setattr(native_policy_hook, "_EVALUATE_POLICY_RETRY_BUDGET_S", 3.0)
    with (
        _http_endpoint(200, _ALLOW_BODY) as (relay_url, relay_targets),
        _http_endpoint(502, b"proxy: connect to 127.0.0.1 refused") as (proxy_url, proxy_targets),
    ):
        _point_proxy_env_at(monkeypatch, proxy_url)
        resp, error = post_evaluate_with_retry(
            f"{relay_url}/policies/evaluate",
            {"Authorization": "Bearer relay-token"},
            {"event": {}},
            5.0,
            "evaluate-policy hook",
        )

    assert error is None, f"loopback callback failed: {error}; proxy saw {proxy_targets!r}"
    assert resp is not None and resp.json() == {"result": "POLICY_ACTION_ALLOW"}
    assert relay_targets == ["/policies/evaluate"]
    assert proxy_targets == []


def test_post_evaluate_with_retry_remote_callback_honors_ambient_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A non-loopback callback still travels through the environment's proxy.

    Hosted deployments reach the Omnigent server only through a corporate
    proxy, so the bypass must stay limited to loopback targets. The server
    name here does not resolve; the request can only succeed via the proxy.
    """
    url = "http://omnigent-server.corp.invalid/v1/sessions/s/policies/evaluate"
    with _http_endpoint(200, _ALLOW_BODY) as (proxy_url, proxy_targets):
        _point_proxy_env_at(monkeypatch, proxy_url)
        resp, error = post_evaluate_with_retry(url, {}, {"event": {}}, 5.0, "evaluate-policy hook")

    assert error is None
    assert resp is not None and resp.json() == {"result": "POLICY_ACTION_ALLOW"}
    assert proxy_targets == [url]


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:28700/policies/evaluate",
        "http://127.8.9.10:1/x",
        "http://localhost:6767/x",
        "http://relay.localhost/x",
        "http://[::1]:6767/x",
        "https://omnigent.example.com/x",
        "http://10.0.0.7:6767/x",
        "http://[fe80::1]:6767/x",
        "not a url",
        "",
    ],
)
def test_is_loopback_url_matches_the_sdk_helper(url: str) -> None:
    """The hook's stdlib copy must classify URLs exactly like the SDK helper it mirrors."""
    from omnigent_client._http import is_loopback_url as sdk_is_loopback_url

    assert native_policy_hook.is_loopback_url(url) is sdk_is_loopback_url(url)


def test_policy_hook_import_does_not_load_the_sdk() -> None:
    """
    Importing the hook module must not pull in ``omnigent_client``.

    The hook runs as a fresh subprocess on every native tool call; the SDK
    package loads the server schemas and roughly triples that startup time.
    """
    probe = (
        "import sys; import omnigent.native.native_policy_hook; "
        "loaded = {name.partition('.')[0] for name in sys.modules}; "
        "sys.exit(1 if 'omnigent_client' in loaded else 0)"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"omnigent_client was imported by the hook module\n{result.stderr}"
    )


def test_post_evaluate_with_retry_reauths_on_login_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A 302→/oidc/ re-mints the bearer and retries, returning the real verdict.

    Regression guard for the production bug where an "old" native session
    (token past the ~1h Databricks OAuth lifetime) failed CLOSED on every tool
    call. The first attempt carries the lapsed token (302), the retry carries
    the fresh token and gets the ALLOW verdict — exactly as the runner's
    refresh-capable ``_RunnerDatabricksAuth`` does for its own callbacks.
    """
    seen_headers: list[dict[str, str]] = []
    redirect = httpx.Response(
        302,
        headers={"Location": "https://w.example.com/oidc/oauth2/v2.0/authorize"},
        request=httpx.Request("POST", "https://ap/x"),
    )
    ok = httpx.Response(
        200,
        text='{"result":"POLICY_ACTION_ALLOW"}',
        request=httpx.Request("POST", "https://ap/x"),
    )
    monkeypatch.setattr(
        native_policy_hook.httpx,
        "Client",
        _make_redirect_then_ok_client(seen_headers, redirect=redirect, ok=ok),
    )
    reauth_calls: list[int] = []

    def _reauth() -> dict[str, str]:
        reauth_calls.append(1)
        return {"Authorization": "Bearer fresh", "X-Databricks-Org-Id": "o1"}

    resp, error = post_evaluate_with_retry(
        "https://ap/x",
        {"Authorization": "Bearer stale"},
        {"event": {}},
        5.0,
        "evaluate-policy hook",
        reauth=_reauth,
    )

    assert resp is ok
    assert error is None
    assert reauth_calls == [1]  # re-minted exactly once
    assert seen_headers[0]["Authorization"] == "Bearer stale"  # first attempt: lapsed token
    assert seen_headers[1]["Authorization"] == "Bearer fresh"  # retry: fresh token


@pytest.mark.parametrize("sever", ["504", "torn"])
def test_post_evaluate_with_retry_reparks_a_held_ask_poll_the_gateway_severed(
    monkeypatch: pytest.MonkeyPatch,
    sever: str,
) -> None:
    """
    A held ASK poll the gateway severs re-POSTs the same id and spends no budget.

    The Databricks front door caps any request at 300s and answers 504 (or
    tears the connection down). The transient budget is 30s from the first
    attempt, so counting that 504 as a fault exhausted it at once and an
    unattended ASK gate failed closed after five minutes.
    """
    clock = {"t": 0.0}
    monkeypatch.setattr(native_policy_hook.time, "monotonic", lambda: clock["t"])
    sleeps: list[float] = []
    monkeypatch.setattr(native_policy_hook.time, "sleep", sleeps.append)
    held = native_policy_hook._EVALUATE_POLICY_HELD_POLL_FLOOR_S + 290.0
    bodies: list[dict[str, object]] = []
    ok = httpx.Response(
        200,
        text='{"result":"POLICY_ACTION_ALLOW"}',
        request=httpx.Request("POST", "https://ap/x"),
    )

    class _Client:
        def __init__(self, *, headers: dict[str, str], timeout: object, **_kwargs: object) -> None:
            del headers, timeout

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *args: object) -> None:
            del args

        def post(self, url: str, *, json: dict[str, object]) -> httpx.Response:
            bodies.append(json)
            if len(bodies) > 2:
                return ok
            clock["t"] += held  # the gateway held the poll for its full budget
            req = httpx.Request("POST", url)
            if sever == "504":
                return httpx.Response(504, request=req)
            raise httpx.RemoteProtocolError("gateway severed the poll", request=req)

    monkeypatch.setattr(native_policy_hook.httpx, "Client", _Client)

    resp, error = post_evaluate_with_retry(
        "https://ap/x", {}, {"event": {}}, 86400.0, "evaluate-policy hook"
    )

    assert resp is ok
    assert error is None
    assert len(bodies) == 3
    assert len({body["_omnigent_elicitation_id"] for body in bodies}) == 1, (
        "every re-POST must re-park the same elicitation"
    )
    initial = native_policy_hook._EVALUATE_POLICY_RETRY_INITIAL_BACKOFF_S
    assert sleeps == [initial, initial], "a severed held poll re-POSTs inside the re-park grace"


def test_post_evaluate_with_retry_fast_5xx_still_exhausts_the_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A server answering 5xx at once is a fault bounded by the transient budget.

    Only a poll held past the floor is a gateway sever; a sick server that
    refuses quickly must still fail closed once the budget is spent.
    """
    clock = {"t": 0.0}
    monkeypatch.setattr(native_policy_hook.time, "monotonic", lambda: clock["t"])

    def _sleep(seconds: float) -> None:
        """
        Advance the fake clock instead of waiting.

        :param seconds: Backoff the loop asked for.
        :returns: None.
        """
        clock["t"] += seconds

    monkeypatch.setattr(native_policy_hook.time, "sleep", _sleep)
    posts: list[str] = []

    class _Client:
        def __init__(self, *, headers: dict[str, str], timeout: object, **_kwargs: object) -> None:
            del headers, timeout

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *args: object) -> None:
            del args

        def post(self, url: str, *, json: dict[str, object]) -> httpx.Response:
            del json
            posts.append(url)
            return httpx.Response(503, request=httpx.Request("POST", url))

    monkeypatch.setattr(native_policy_hook.httpx, "Client", _Client)

    resp, error = post_evaluate_with_retry(
        "https://ap/x", {}, {"event": {}}, 86400.0, "evaluate-policy hook"
    )

    assert resp is None
    assert error is not None and "retry budget exhausted" in error
    assert 2 <= len(posts) <= 8, "retried within the budget, then gave up"


def test_post_evaluate_with_retry_reauths_on_403_invalid_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A 403 "Invalid Token" re-mints the bearer and retries, returning the verdict.

    Databricks Apps returns 403 (not 401) for an expired bearer. Guards the fix
    that added 403 to the re-auth signal set alongside 401 and 302→/oidc/.
    """
    seen_headers: list[dict[str, str]] = []
    forbidden = httpx.Response(
        403,
        text="Invalid Token",
        request=httpx.Request("POST", "https://ap/x"),
    )
    ok = httpx.Response(
        200,
        text='{"result":"POLICY_ACTION_ALLOW"}',
        request=httpx.Request("POST", "https://ap/x"),
    )
    monkeypatch.setattr(
        native_policy_hook.httpx,
        "Client",
        _make_redirect_then_ok_client(seen_headers, redirect=forbidden, ok=ok),
    )
    reauth_calls: list[int] = []

    def _reauth() -> dict[str, str]:
        reauth_calls.append(1)
        return {"Authorization": "Bearer fresh"}

    resp, error = post_evaluate_with_retry(
        "https://ap/x",
        {"Authorization": "Bearer stale"},
        {"event": {}},
        5.0,
        "evaluate-policy hook",
        reauth=_reauth,
    )

    assert resp is ok
    assert error is None
    assert reauth_calls == [1]
    assert seen_headers[0]["Authorization"] == "Bearer stale"
    assert seen_headers[1]["Authorization"] == "Bearer fresh"


def test_post_evaluate_with_retry_no_reauth_fails_on_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    With no ``reauth`` callable, a login-redirect fails closed with a clear reason.

    Callers without a token source (e.g. codex/kimi today) still fail closed on
    a 302→/oidc bounce, but the helper rejects the redirect explicitly (rather
    than leaning on ``raise_for_status``'s version-dependent 3xx handling and a
    cryptic downstream "empty/malformed response"): it returns ``None`` with an
    error that names the login redirect, and does not retry.
    """
    seen_headers: list[dict[str, str]] = []
    redirect = httpx.Response(
        302,
        headers={"Location": "https://w.example.com/oidc/x"},
        request=httpx.Request("POST", "https://ap/x"),
    )
    monkeypatch.setattr(
        native_policy_hook.httpx,
        "Client",
        _make_redirect_then_ok_client(seen_headers, redirect=redirect, ok=redirect),
    )
    resp, error = post_evaluate_with_retry(
        "https://ap/x", {}, {"event": {}}, 5.0, "evaluate-policy hook"
    )
    assert resp is None
    assert error is not None
    assert "login redirect" in error and "302" in error
    assert len(seen_headers) == 1  # one attempt; a 302 is not retried without reauth


def test_post_evaluate_with_retry_reauth_unavailable_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    When re-mint yields no token, the helper returns ``None`` (caller fails closed).

    Re-auth is best-effort: a ``reauth`` that returns ``None`` (no creds /
    transient mint failure) must not loop — the redirect is rejected explicitly
    (returning ``None`` with a login-redirect error) so the caller keeps the
    fail-closed safety net.
    """
    seen_headers: list[dict[str, str]] = []
    redirect = httpx.Response(
        302,
        headers={"Location": "https://w.example.com/oidc/x"},
        request=httpx.Request("POST", "https://ap/x"),
    )
    monkeypatch.setattr(
        native_policy_hook.httpx,
        "Client",
        _make_redirect_then_ok_client(seen_headers, redirect=redirect, ok=redirect),
    )
    resp, error = post_evaluate_with_retry(
        "https://ap/x",
        {"Authorization": "Bearer stale"},
        {"event": {}},
        5.0,
        "evaluate-policy hook",
        reauth=lambda: None,
    )
    assert resp is None
    assert error is not None
    assert "login redirect" in error and "302" in error
    assert len(seen_headers) == 1  # one attempt only; no retry loop


# ── shared policy-hook header plumbing (writer + reader) ─────────────


def test_policy_hook_request_headers_merges_baked_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reader merges the executor-baked auth + routing headers.

    The import-free hook subprocess can't resolve credentials in-process, so
    the executor bakes them into ``_OMNIGENT_AUTH_HEADERS``; the reader must
    fold them onto ``Content-Type`` for the policy POST.
    """
    monkeypatch.setenv(
        "_OMNIGENT_AUTH_HEADERS",
        '{"Authorization": "Bearer tok", "X-Databricks-Org-Id": "org123"}',
    )
    assert native_policy_hook.policy_hook_request_headers() == {
        "Content-Type": "application/json",
        "Authorization": "Bearer tok",
        "X-Databricks-Org-Id": "org123",
    }


@pytest.mark.parametrize("raw", ["", "not json", "[1,2]"])
def test_policy_hook_request_headers_tolerates_missing_or_bad_env(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """Absent / malformed env → just ``Content-Type`` (local-unauth path).

    A bad value must not crash the hook nor inject garbage headers — the
    server simply decides without auth (a local unauthenticated server needs
    none).
    """
    if raw:
        monkeypatch.setenv("_OMNIGENT_AUTH_HEADERS", raw)
    else:
        monkeypatch.delenv("_OMNIGENT_AUTH_HEADERS", raising=False)
    assert native_policy_hook.policy_hook_request_headers() == {"Content-Type": "application/json"}


def test_policy_hook_wrapper_script_bakes_auth_and_routing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The writer bakes bearer + routing into the wrapper via the one builder.

    A new harness wiring up its hook through this helper gets auth AND
    workspace routing for free — the gap that left the cursor/hermes hooks
    posting unauthenticated and unrouted.
    """
    import omnigent.cli_auth as cli_auth
    import omnigent.runner._entry as entry

    monkeypatch.setattr(
        entry, "_make_auth_token_factory", lambda *, server_url=None: lambda: "tok"
    )
    monkeypatch.setattr(cli_auth, "load_databricks_org_id", lambda _url: "org123")

    script = native_policy_hook.policy_hook_wrapper_script(
        "https://acme.databricks.com/api/2.0/omnigent", "conv_x", "/path/hook.py"
    )

    assert script.startswith("#!/bin/sh\n")
    assert "_OMNIGENT_SERVER_URL=https://acme.databricks.com/api/2.0/omnigent" in script
    assert "_OMNIGENT_SESSION_ID=conv_x" in script
    # The baked headers carry BOTH the bearer and the routing header.
    line = next(
        ln for ln in script.splitlines() if ln.startswith("export _OMNIGENT_AUTH_HEADERS=")
    )
    assert "Bearer tok" in line
    assert "X-Databricks-Org-Id" in line and "org123" in line


def test_policy_hook_wrapper_script_omits_auth_when_unauthenticated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No token + no recorded selector → empty auth dict (local-unauth runs).

    The wrapper still exports the (empty) header dict, so the reader yields
    just ``Content-Type`` — non-workspace callers are unaffected.
    """
    import omnigent.cli_auth as cli_auth
    import omnigent.runner._entry as entry

    monkeypatch.setattr(entry, "_make_auth_token_factory", lambda *, server_url=None: None)
    monkeypatch.setattr(cli_auth, "load_databricks_org_id", lambda _url: None)

    script = native_policy_hook.policy_hook_wrapper_script(
        "http://127.0.0.1:6767", "conv_local", "/path/hook.py"
    )
    line = next(
        ln for ln in script.splitlines() if ln.startswith("export _OMNIGENT_AUTH_HEADERS=")
    )
    assert "Bearer" not in line
    assert "X-Databricks-Org-Id" not in line


def test_policy_hook_reauth_remints_and_preserves_routing_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shared reauth re-mints the bearer and keeps the routing header.

    When a baked one-shot token lapses, the four hooks (codex/kimi/cursor/
    hermes) that wire this builder must mint a fresh bearer through the same
    factory the refresh-capable runtime auth uses — without dropping the
    workspace-routing header that travels alongside it.
    """
    monkeypatch.setattr(
        "omnigent.runner._entry._make_auth_token_factory",
        lambda _server_url: lambda: "fresh-token",
    )
    reauth = native_policy_hook.policy_hook_reauth(
        "https://acme.databricks.com/api/2.0/omnigent",
        {"Authorization": "Bearer stale", "X-Databricks-Org-Id": "o9"},
    )
    assert reauth() == {"Authorization": "Bearer fresh-token", "X-Databricks-Org-Id": "o9"}


def test_policy_hook_reauth_returns_none_without_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No refresh mechanism (local/unauth) → ``None`` so the caller fails closed."""
    monkeypatch.setattr(
        "omnigent.runner._entry._make_auth_token_factory",
        lambda _server_url: None,
    )
    reauth = native_policy_hook.policy_hook_reauth(
        "http://127.0.0.1:6767", {"Content-Type": "application/json"}
    )
    assert reauth() is None


def test_documented_tool_response_takes_precedence() -> None:
    result = hook_payload_to_evaluation_request(
        "PostToolUse",
        {
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "tool_response": "actual result",
            "tool_output": "legacy",
        },
    )
    assert result is not None
    assert result["event"]["data"]["result"] == "actual result"
