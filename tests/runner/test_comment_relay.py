"""Tests for the claude-native comment-tool relay wiring in the runner.

These exercise the public runner HTTP surface — ``POST
/v1/sessions/{id}/resources/terminals`` and ``DELETE /v1/sessions/{id}`` —
to verify that launching a Claude terminal with ``bridge_inject_dir``
starts the per-session comment-tool relay (writing ``tool_relay.json``
into the bridge directory) and that deleting the session tears it down.

The full round-trip (Claude Code actually calling ``list_comments`` /
``update_comment`` over the MCP bridge) is covered by the e2e test
``tests/e2e/test_comment_tools_claude_native.py``; these unit tests cover
the runner-side wiring that the e2e test cannot pinpoint when it fails.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from omnigent.claude_native_bridge import (
    BRIDGE_ID_LABEL_KEY,
    bridge_dir_for_bridge_id,
    prepare_bridge_dir,
)
from omnigent.entities.session_resources import SessionResourceView, terminal_resource_view
from omnigent.inner.datamodel import TerminalEnvSpec
from omnigent.runner import create_runner_app
from omnigent.spec.types import AgentSpec, ToolsConfig
from omnigent.terminals import TerminalListEntry
from tests.runner.helpers import NullServerClient, make_test_terminal_instance

# Matches ``_TOOL_RELAY_FILE`` in ``omnigent.claude_native_bridge``.
_TOOL_RELAY_FILE = "tool_relay.json"


class _StubResourceRegistry:
    """Resource registry stub that returns a terminal view without spawning.

    The real :class:`SessionResourceRegistry` would launch an actual tmux
    terminal; this stub returns a valid resource view so the route reaches
    the ``bridge_inject_dir`` branch without side effects. ``terminal_registry``
    is ``None`` so ``_publish_tmux_target_for_bridge`` no-ops, keeping the test
    focused on the comment relay.
    """

    # _publish_tmux_target_for_bridge returns early when this is None.
    terminal_registry = None

    def __init__(self, tmp_path: Path) -> None:
        """
        Initialize the stub.

        :param tmp_path: Temporary directory returned as the default env root.
        :returns: None.
        """
        self._tmp_path = tmp_path

    def set_terminal_activity_publisher(
        self,
        publisher: Callable[[str, str], None],
    ) -> None:
        """
        Accept the terminal-activity publisher installed by the runner app.

        The stub never launches a real terminal, so it just retains the
        callback (unused) to satisfy ``create_runner_app``'s wiring.

        :param publisher: Callable ``(session_id, terminal_id) -> None``.
        :returns: None.
        """
        self._terminal_activity_publisher = publisher

    def set_session_status_publisher(
        self,
        publisher: Callable[[str, str], None],
    ) -> None:
        """
        Accept the session-status publisher installed by the runner app.

        The stub never launches a real terminal, so it just retains the
        callback (unused) to satisfy ``create_runner_app``'s wiring.

        :param publisher: Callable ``(session_id, status) -> None``.
        :returns: None.
        """
        self._session_status_publisher = publisher

    def set_terminal_exit_publisher(
        self,
        publisher: Callable[[Any], None],
    ) -> None:
        """
        Accept the terminal-exit publisher installed by the runner app.

        :param publisher: Callable receiving a terminal-exit event.
        :returns: None.
        """
        self._terminal_exit_publisher = publisher

    def compute_default_env_root(self, session_id: str, agent_spec: Any) -> str:
        """
        Return a fixed env root for the launched terminal.

        :param session_id: Session/conversation identifier (unused).
        :param agent_spec: Resolved agent spec (unused).
        :returns: The temp directory path as a string.
        """
        del session_id, agent_spec
        return str(self._tmp_path)

    async def launch_required_terminal(
        self,
        session_id: str,
        terminal_name: str,
        session_key: str,
        spec: TerminalEnvSpec,
        cwd_override: str | None = None,
        sandbox_override: str | None = None,
        parent_os_env: object | None = None,
        resource_role: str | None = None,
    ) -> SessionResourceView:
        """Return a required terminal resource view for a fake instance."""
        return await self._launch(
            session_id=session_id,
            terminal_name=terminal_name,
            session_key=session_key,
            spec=spec,
            cwd_override=cwd_override,
            sandbox_override=sandbox_override,
            parent_os_env=parent_os_env,
            resource_role=resource_role,
        )

    async def launch_auxiliary_terminal(
        self,
        session_id: str,
        terminal_name: str,
        session_key: str,
        spec: TerminalEnvSpec,
        cwd_override: str | None = None,
        sandbox_override: str | None = None,
        parent_os_env: object | None = None,
        resource_role: str | None = None,
    ) -> SessionResourceView:
        """Return an auxiliary terminal resource view for a fake instance."""
        return await self._launch(
            session_id=session_id,
            terminal_name=terminal_name,
            session_key=session_key,
            spec=spec,
            cwd_override=cwd_override,
            sandbox_override=sandbox_override,
            parent_os_env=parent_os_env,
            resource_role=resource_role,
        )

    async def _launch(
        self,
        *,
        session_id: str,
        terminal_name: str,
        session_key: str,
        spec: TerminalEnvSpec,
        cwd_override: str | None = None,
        sandbox_override: str | None = None,
        parent_os_env: object | None = None,
        resource_role: str | None = None,
    ) -> SessionResourceView:
        """
        Return a terminal resource view for a fake instance.

        :param session_id: Session/conversation identifier.
        :param terminal_name: Terminal name from the request, e.g. ``"claude"``.
        :param session_key: Per-launch terminal key, e.g. ``"main"``.
        :param spec: Terminal environment spec (unused in the stub).
        :param cwd_override: Optional cwd override (unused).
        :param sandbox_override: Optional sandbox override (unused).
        :param parent_os_env: Agent's ``os_env`` threaded through by the
            runner so the launched terminal can inherit the sandbox
            (unused in the stub).
        :param resource_role: Runner-private role marker (e.g.
            ``"claude-native"``) for the bridge-inject path (unused in
            the stub).
        :returns: Terminal resource view for the fake instance.
        """
        del spec, cwd_override, sandbox_override, parent_os_env, resource_role
        instance = make_test_terminal_instance(terminal_name, session_key, self._tmp_path)
        return terminal_resource_view(
            session_id,
            TerminalListEntry(
                terminal_name=terminal_name,
                session_key=session_key,
                instance=instance,
            ),
        )

    async def cleanup_session(self, session_id: str) -> None:
        """
        No-op session cleanup invoked by ``DELETE /v1/sessions/{id}``.

        :param session_id: Session/conversation identifier (unused).
        :returns: None.
        """
        del session_id


@dataclass
class _RelayEnv:
    """
    Per-test environment for the comment-relay route tests.

    :param session_id: Unique session id, e.g. ``"conv_ab12cd34ef56"``.
    :param bridge_dir: Bridge directory derived from ``session_id``.
    :param client: HTTP client pointed at the runner app.
    """

    session_id: str
    bridge_dir: Path
    client: httpx.AsyncClient


@pytest.fixture(autouse=True)
def _skip_tools_changed_notification(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Neutralize the cold-path ``notifications/tools/list_changed`` post.

    A ``bridge_inject_dir`` launch makes the runner's
    ``_ensure_comment_relay_started`` fire ``post_tools_changed`` as a
    fire-and-forget executor task (the cold path, ``await_notify=False``).
    These unit tests stub the terminal, so no real Claude Code MCP bridge
    ever publishes ``server.json`` — ``post_tools_changed`` then spins in
    ``_wait_for_server_info`` for the full 30s ``_TOOLS_CHANGED_READY_TIMEOUT_S``
    before giving up. The notify runs in the default ``ThreadPoolExecutor``,
    so the call returns instantly but the worker thread stays stuck; at
    teardown the event loop's ``shutdown_default_executor(wait=True)`` joins
    that thread, making every relay test's teardown take ~30s.

    The relay wiring these tests cover (``tool_relay.json`` written, socket
    bound, idempotency, teardown unlink) does not involve the notification —
    the real notify round-trip is covered by
    ``tests/e2e/test_comment_tools_claude_native.py`` — so stubbing it to a
    no-op removes the dead wait without weakening coverage. Mirrors the
    established stub in ``tests/runner/test_app_sessions_native.py``.
    """

    def _noop(*args: object, **kwargs: object) -> None:
        del args, kwargs

    # The runner imports the name from this module at call time, so patching
    # the module attribute is picked up by _ensure_comment_relay_started.
    monkeypatch.setattr("omnigent.claude_native_bridge.post_tools_changed", _noop)


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    """
    Build a runner app with a non-spawning resource registry stub.

    :param tmp_path: Pytest temp directory used for the stub env root.
    :returns: The runner FastAPI app.
    """
    return create_runner_app(
        resource_registry=_StubResourceRegistry(tmp_path),
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """
    Yield an HTTP client bound to the runner app via ASGI transport.

    :param app: The runner FastAPI app.
    :yields: An ``httpx.AsyncClient`` for the runner.
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as c:
        yield c


@pytest.fixture
async def relay_env(tmp_path: Path, client: httpx.AsyncClient) -> AsyncIterator[_RelayEnv]:
    """
    Prepare a bridge directory for a unique session and clean it up.

    ``start_tool_relay`` writes ``tool_relay.json`` into the bridge dir but
    does not create it, so this mirrors what ``omnigent claude`` does on
    the client (``prepare_bridge_dir``) before the terminal launches. On
    teardown it deletes the session (closing any relay and unbinding its
    localhost socket) and removes the bridge dir so tests do not leak.

    :param tmp_path: Pytest temp directory used as the bridge workspace.
    :param client: HTTP client bound to the runner app.
    :yields: A :class:`_RelayEnv` for the test.
    """
    session_id = f"conv_{uuid.uuid4().hex[:12]}"
    bridge_dir = bridge_dir_for_bridge_id(session_id)
    prepare_bridge_dir(session_id, workspace=tmp_path)
    try:
        yield _RelayEnv(session_id=session_id, bridge_dir=bridge_dir, client=client)
    finally:
        with contextlib.suppress(httpx.HTTPError):
            await client.delete(f"/v1/sessions/{session_id}")
        shutil.rmtree(bridge_dir, ignore_errors=True)


async def _launch_terminal(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    bridge_inject_dir: bool,
) -> httpx.Response:
    """
    POST the claude terminal-launch request used by ``omnigent claude``.

    :param client: HTTP client bound to the runner app.
    :param session_id: Session/conversation identifier.
    :param bridge_inject_dir: When ``True``, set the ``bridge_inject_dir``
        opt-in that gates the comment-relay start (the claude-native signal).
    :returns: The route response.
    """
    body: dict[str, Any] = {"terminal": "claude", "session_key": "main"}
    if bridge_inject_dir:
        body["bridge_inject_dir"] = True
    return await client.post(
        f"/v1/sessions/{session_id}/resources/terminals",
        json=body,
    )


@pytest.mark.asyncio
async def test_terminal_launch_with_bridge_inject_advertises_comment_tools(
    relay_env: _RelayEnv,
) -> None:
    """A bridge_inject_dir launch writes tool_relay.json with the relay tools."""
    resp = await _launch_terminal(relay_env.client, relay_env.session_id, bridge_inject_dir=True)
    assert resp.status_code == 200, f"terminal launch failed: {resp.text}"

    relay_file = relay_env.bridge_dir / _TOOL_RELAY_FILE
    # The relay advertisement must exist: the bridge_inject_dir branch of
    # create_session_terminal called _ensure_comment_relay_started. If it is
    # missing, the wiring (gate or start) is broken and the bridge would never
    # expose the relay tools to Claude.
    assert relay_file.exists(), "tool_relay.json was not written by the relay"
    info = json.loads(relay_file.read_text())

    tools_by_name = {t["name"]: t for t in info["tools"]}
    # The framework comment tools, read-only session-discovery tools
    # (sys_session_list / sys_session_get_history / sys_session_get_info),
    # read-only agent tools (sys_agent_list / sys_agent_get /
    # sys_agent_download), policy tools (sys_add_policy /
    # sys_policy_registry), and OS tools (sys_os_*) — claude-native
    # ignores the harness tool schemas, so this relay is the only
    # surface that reaches Claude Code. All are routed through the AP
    # server's /mcp endpoint for policy enforcement. The opt-in spawn
    # writes (sys_session_send/close/create) are absent here because
    # this fixture's session has no resolvable spec — the fallback
    # can't evaluate the (tools.agents | spawn) gate; specs that opt
    # in get them via the ToolManager-derived branch.
    # No more, no less: a missing entry means the schema loop dropped a class;
    # an extra entry means an unintended tool leaked into the relay.
    assert set(tools_by_name) == {
        "list_comments",
        "update_comment",
        "sys_session_list",
        "sys_session_get_history",
        "sys_session_get_info",
        "sys_session_rename",
        "sys_agent_list",
        "sys_agent_get",
        "sys_agent_download",
        "sys_add_policy",
        "sys_policy_registry",
        "sys_os_read",
        "sys_os_write",
        "sys_os_edit",
        "sys_os_shell",
    }
    # Parameters must be the real schemas from the tool classes — proving
    # get_schema() flowed through rather than an empty placeholder. "status"
    # is a real list_comments filter; "comment_id" is required by update_comment;
    # "conversation_id" is the sys_session_get_history arg the runner dispatch matches;
    # "path" is a required param for sys_os_read.
    assert "status" in tools_by_name["list_comments"]["parameters"]["properties"]
    assert "comment_id" in tools_by_name["update_comment"]["parameters"]["properties"]
    assert (
        "conversation_id" in tools_by_name["sys_session_get_history"]["parameters"]["properties"]
    )
    assert "path" in tools_by_name["sys_os_read"]["parameters"]["properties"]
    # A url + token prove the localhost relay HTTP server actually started
    # (start_tool_relay bound a socket), not just that a file was written.
    assert info["url"].startswith("http://127.0.0.1:")
    assert info["token"]


@pytest.mark.asyncio
async def test_terminal_launch_without_bridge_inject_starts_no_relay(
    relay_env: _RelayEnv,
) -> None:
    """A plain terminal launch (no opt-in) must not start the comment relay."""
    resp = await _launch_terminal(relay_env.client, relay_env.session_id, bridge_inject_dir=False)
    assert resp.status_code == 200, f"terminal launch failed: {resp.text}"

    relay_file = relay_env.bridge_dir / _TOOL_RELAY_FILE
    # No bridge_inject_dir means no claude-native signal, so the relay must not
    # start. If this file exists, the gate fired for a non-claude-native launch
    # (e.g. a codex terminal would wrongly get a claude relay).
    assert not relay_file.exists(), "relay started without the bridge_inject_dir opt-in"


@pytest.mark.asyncio
async def test_session_delete_removes_comment_relay(relay_env: _RelayEnv) -> None:
    """Deleting the session closes the relay and removes tool_relay.json."""
    resp = await _launch_terminal(relay_env.client, relay_env.session_id, bridge_inject_dir=True)
    assert resp.status_code == 200
    relay_file = relay_env.bridge_dir / _TOOL_RELAY_FILE
    assert relay_file.exists()  # precondition: relay is up

    del_resp = await relay_env.client.delete(f"/v1/sessions/{relay_env.session_id}")
    assert del_resp.status_code == 200, f"delete failed: {del_resp.text}"

    # ClaudeNativeToolRelay.close() unlinks tool_relay.json and shuts the HTTP
    # server down. If the file remains, delete_session did not close the relay,
    # leaking a localhost socket and a stale advertisement for the next session.
    assert not relay_file.exists(), "tool_relay.json survived session deletion"


@pytest.mark.asyncio
async def test_repeated_terminal_launch_keeps_single_relay(relay_env: _RelayEnv) -> None:
    """A second bridge_inject_dir launch reuses the relay instead of rebinding."""
    first = await _launch_terminal(relay_env.client, relay_env.session_id, bridge_inject_dir=True)
    assert first.status_code == 200
    relay_file = relay_env.bridge_dir / _TOOL_RELAY_FILE
    first_url = json.loads(relay_file.read_text())["url"]

    second = await _launch_terminal(relay_env.client, relay_env.session_id, bridge_inject_dir=True)
    assert second.status_code == 200
    second_url = json.loads(relay_file.read_text())["url"]

    # The relay URL (its bound port) must be unchanged. A different port means
    # _ensure_comment_relay_started bound a second relay instead of
    # short-circuiting on the _session_comment_relays guard — which would leak
    # the first relay's HTTP server and socket.
    assert second_url == first_url, (
        f"second launch rebound the relay ({first_url!r} -> {second_url!r}); "
        f"the idempotency guard did not hold"
    )


@pytest.mark.asyncio
async def test_relay_executor_routes_through_omnigent_in_omnigent_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Route relay tool execution through Omnigent ``/mcp`` for policy enforcement.

    Verifies that when the runner is configured with a server_client (AP mode),
    the ``_relay_tool_executor`` closure routes calls through
    :class:`~omnigent.runner.proxy_mcp_manager.ProxyMcpManager` instead of
    dispatching directly to comment/session-query handlers.  Policy enforcement
    on these relay tools was previously bypassed; this test pins the fix.
    """
    import omnigent.claude_native_bridge as _bridge_mod

    # Records every POST sent to the fake Omnigent server.
    ap_mcp_posts: list[dict[str, Any]] = []

    class _FakeApClient:
        """Fake Omnigent server client that captures /mcp calls and returns a fixed result.

        Appends each POST request body to the outer ``ap_mcp_posts`` list via
        closure so the test can assert on what was sent to the Omnigent server.
        """

        async def get(self, url: str, *, timeout: float = 10.0) -> httpx.Response:
            """Return a session snapshot with no labels so bridge_id falls back to session_id.

            :param url: Request URL (unused beyond the response).
            :param timeout: Request timeout (unused).
            :returns: 200 response with an empty labels dict.
            """
            del timeout
            req = httpx.Request("GET", f"http://ap-server{url}")
            return httpx.Response(200, json={"labels": {}}, request=req)

        async def post(
            self,
            url: str,
            *,
            json: dict[str, Any],
            timeout: float = 60.0,
        ) -> httpx.Response:
            """Record the request and return a valid MCP tools/call response.

            :param url: Target URL, e.g. ``"/v1/sessions/conv_x/mcp"``.
            :param json: JSON-RPC 2.0 request body.
            :param timeout: Request timeout (unused).
            :returns: 200 response with a fixed MCP result.
            """
            del timeout
            ap_mcp_posts.append({"url": url, "json": json})
            req = httpx.Request("POST", f"http://ap-server{url}")
            return httpx.Response(
                200,
                json={
                    "result": {
                        "content": [{"type": "text", "text": '{"items": []}'}],
                        "isError": False,
                    }
                },
                request=req,
            )

    # Intercept start_tool_relay to capture the _relay_tool_executor callback
    # before it's wired into the HTTP relay server.
    captured_executors: list[Any] = []
    _real_start = _bridge_mod.start_tool_relay

    def _capturing_relay(**kwargs: Any) -> Any:
        """Wrap start_tool_relay to capture the tool_executor callback.

        :param kwargs: Forwarded to the real start_tool_relay.
        :returns: The real relay handle.
        """
        captured_executors.append(kwargs["tool_executor"])
        return _real_start(**kwargs)

    monkeypatch.setattr(_bridge_mod, "start_tool_relay", _capturing_relay)
    # The autouse _skip_tools_changed_notification fixture already stubs
    # post_tools_changed to a no-op for every test in this module, so no
    # local suppression is needed here.

    session_id = f"conv_{uuid.uuid4().hex[:12]}"
    bridge_dir = bridge_dir_for_bridge_id(session_id)
    prepare_bridge_dir(session_id, workspace=tmp_path)

    try:
        app = create_runner_app(
            resource_registry=_StubResourceRegistry(tmp_path),
            server_client=_FakeApClient(),  # type: ignore[arg-type]  # duck-typed for test
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://runner") as c:
            resp = await c.post(
                f"/v1/sessions/{session_id}/resources/terminals",
                json={"terminal": "claude", "session_key": "main", "bridge_inject_dir": True},
            )
            assert resp.status_code == 200, f"terminal launch failed: {resp.text}"

        # start_tool_relay must have fired exactly once — one terminal launch,
        # one relay. 0 means _ensure_comment_relay_started never called
        # start_tool_relay (wiring broken); >1 means multiple relays were
        # started for a single session (idempotency guard broken).
        assert len(captured_executors) == 1, (
            f"Expected start_tool_relay called once, got {len(captured_executors)}. "
            "0 means relay wiring is broken; >1 means idempotency guard failed."
        )
        executor = captured_executors[0]

        # Call the relay executor directly (simulates Claude Code invoking list_comments).
        result = await executor("list_comments", {"status": "pending"})

        # In Omnigent mode the executor must have POSTed a tools/call JSON-RPC to the
        # Omnigent server's /mcp endpoint, not called the direct comment handler.
        mcp_call = next(
            (
                r
                for r in ap_mcp_posts
                if "/mcp" in r["url"] and r["json"].get("method") == "tools/call"
            ),
            None,
        )
        assert mcp_call is not None, (
            "No tools/call request reached the Omnigent /mcp endpoint. "
            "The relay executor is bypassing ProxyMcpManager and policy enforcement."
        )
        # Tool name and arguments must be forwarded verbatim.
        assert mcp_call["json"]["params"]["name"] == "list_comments", (
            "Wrong tool name forwarded; policy would be evaluated against the wrong tool."
        )
        assert mcp_call["json"]["params"]["arguments"] == {"status": "pending"}, (
            "Arguments were not forwarded correctly to Omnigent /mcp."
        )
        # The request URL must be scoped to this session's /mcp endpoint.
        assert session_id in mcp_call["url"], (
            f"AP /mcp request URL {mcp_call['url']!r} does not contain session_id {session_id!r}."
        )
        # The Omnigent response's text content must be parsed back to a dict.
        assert result == {"items": []}, f"Expected parsed Omnigent response dict, got {result!r}."
    finally:
        shutil.rmtree(bridge_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_terminal_launch_writes_session_id_into_tool_relay_json(
    relay_env: _RelayEnv,
) -> None:
    """tool_relay.json written by bridge_inject_dir launch contains session_id."""
    resp = await _launch_terminal(relay_env.client, relay_env.session_id, bridge_inject_dir=True)
    assert resp.status_code == 200, f"terminal launch failed: {resp.text}"

    relay_file = relay_env.bridge_dir / _TOOL_RELAY_FILE
    assert relay_file.exists(), "tool_relay.json was not written"
    info = json.loads(relay_file.read_text())
    assert info.get("session_id") == relay_env.session_id, (
        f"session_id missing or wrong in tool_relay.json: {info}"
    )


@pytest.mark.asyncio
async def test_relay_policy_evaluate_proxies_to_server_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Relay POST /policies/evaluate forwards body to server_client and returns verdict."""
    import asyncio

    from omnigent.claude_native_bridge import prepare_bridge_dir as _prep
    from omnigent.claude_native_bridge import start_tool_relay

    monkeypatch.setattr("omnigent.claude_native_bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr("omnigent.claude_native_bridge._BRIDGE_ROOT", tmp_path / "root")

    bridge_dir = _prep("relay-policy-test", workspace=tmp_path)
    session_id = "conv_relay_test"

    captured: dict[str, object] = {}

    class _CapturingServerClient:
        """Fake server_client that records the /policies/evaluate POST."""

        content = b'{"result":"POLICY_ACTION_DENY","reason":"blocked"}'
        status_code = 200
        headers: dict[str, str] = {"Content-Type": "application/json"}

        async def post(self, url: str, **kwargs: object) -> _CapturingServerClient:
            captured["url"] = url
            captured["json"] = kwargs.get("json")
            return self

    server_client = _CapturingServerClient()
    loop = asyncio.get_running_loop()

    relay = start_tool_relay(
        bridge_dir=bridge_dir,
        tools=[],
        tool_executor=lambda name, args: {},  # type: ignore[arg-type]
        loop=loop,
        policy_client=server_client,
        session_id=session_id,
    )
    try:
        relay_info = json.loads((bridge_dir / _TOOL_RELAY_FILE).read_text())
        relay_url = relay_info["url"]
        relay_token = relay_info["token"]

        eval_body = {"event": {"type": "PHASE_TOOL_CALL", "target": "", "data": {"name": "Bash"}}}

        async with httpx.AsyncClient() as c:
            resp = await c.post(
                f"{relay_url}/policies/evaluate",
                json=eval_body,
                headers={"Authorization": f"Bearer {relay_token}"},
                timeout=5.0,
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["result"] == "POLICY_ACTION_DENY"
        # Verify proxy forwarded to server_client at the correct path.
        assert captured.get("url") == f"/v1/sessions/{session_id}/policies/evaluate"
        assert captured.get("json") == eval_body
    finally:
        relay.close()


@pytest.mark.asyncio
async def test_relay_policy_evaluate_rejects_wrong_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Relay /policies/evaluate returns 401 for wrong bearer token."""
    import asyncio

    from omnigent.claude_native_bridge import prepare_bridge_dir as _prep
    from omnigent.claude_native_bridge import start_tool_relay

    monkeypatch.setattr("omnigent.claude_native_bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr("omnigent.claude_native_bridge._BRIDGE_ROOT", tmp_path / "root")

    bridge_dir = _prep("relay-policy-auth-test", workspace=tmp_path)

    class _NeverCalledClient:
        async def post(self, *a: object, **kw: object) -> object:
            raise AssertionError("server_client.post should not be called on auth failure")

    loop = asyncio.get_running_loop()
    relay = start_tool_relay(
        bridge_dir=bridge_dir,
        tools=[],
        tool_executor=lambda name, args: {},  # type: ignore[arg-type]
        loop=loop,
        policy_client=_NeverCalledClient(),
        session_id="conv_auth_test",
    )
    try:
        relay_url = json.loads((bridge_dir / _TOOL_RELAY_FILE).read_text())["url"]

        async with httpx.AsyncClient() as c:
            resp = await c.post(
                f"{relay_url}/policies/evaluate",
                json={"event": {}},
                headers={"Authorization": "Bearer wrong-token"},
                timeout=5.0,
            )

        assert resp.status_code == 401
    finally:
        relay.close()


@pytest.mark.asyncio
async def test_relay_policy_evaluate_surfaces_upstream_error_in_502_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Relay /policies/evaluate returns a 502 whose body names the upstream failure.

    When the refresh-capable server_client raises (e.g. the Databricks token
    could not be refreshed), the relay must surface that reason instead of the
    generic http.server 502 page, so the hook's fail-closed ``Detail:`` is
    actionable rather than an opaque gateway error.
    """
    import asyncio

    from omnigent.claude_native_bridge import prepare_bridge_dir as _prep
    from omnigent.claude_native_bridge import start_tool_relay

    monkeypatch.setattr("omnigent.claude_native_bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr("omnigent.claude_native_bridge._BRIDGE_ROOT", tmp_path / "root")

    bridge_dir = _prep("relay-policy-error-test", workspace=tmp_path)

    class _RaisingServerClient:
        """Fake server_client whose policy POST fails like a lapsed token."""

        async def post(self, *a: object, **kw: object) -> object:
            raise httpx.RequestError("Databricks token refresh returned no token")

    loop = asyncio.get_running_loop()
    relay = start_tool_relay(
        bridge_dir=bridge_dir,
        tools=[],
        tool_executor=lambda name, args: {},  # type: ignore[arg-type]
        loop=loop,
        policy_client=_RaisingServerClient(),
        session_id="conv_err_test",
    )
    try:
        relay_info = json.loads((bridge_dir / _TOOL_RELAY_FILE).read_text())
        relay_url = relay_info["url"]
        relay_token = relay_info["token"]

        async with httpx.AsyncClient() as c:
            resp = await c.post(
                f"{relay_url}/policies/evaluate",
                json={"event": {"type": "PHASE_REQUEST", "target": "", "data": {"text": "hi"}}},
                headers={"Authorization": f"Bearer {relay_token}"},
                timeout=5.0,
            )

        assert resp.status_code == 502
        # The body must name the upstream cause, not the generic HTML page, so
        # the hook's fail-closed reason is actionable.
        assert "Databricks token refresh returned no token" in resp.text
        assert "<html" not in resp.text.lower()
    finally:
        relay.close()


@pytest.mark.asyncio
async def test_relay_policy_evaluate_truncates_long_upstream_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A very long upstream error is truncated with its leading cause intact.

    The detail is capped before the fixed prefix is prepended, so the 502 body
    always starts with the actionable prefix + cause and ends with an ellipsis
    rather than being cut mid-reason by a cap applied to the whole message.
    """
    import asyncio

    from omnigent.claude_native_bridge import prepare_bridge_dir as _prep
    from omnigent.claude_native_bridge import start_tool_relay

    monkeypatch.setattr("omnigent.claude_native_bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr("omnigent.claude_native_bridge._BRIDGE_ROOT", tmp_path / "root")

    bridge_dir = _prep("relay-policy-trunc-test", workspace=tmp_path)

    class _LongRaisingServerClient:
        """Fake server_client whose policy POST fails with a very long reason."""

        async def post(self, *a: object, **kw: object) -> object:
            raise httpx.RequestError("x" * 5000)

    loop = asyncio.get_running_loop()
    relay = start_tool_relay(
        bridge_dir=bridge_dir,
        tools=[],
        tool_executor=lambda name, args: {},  # type: ignore[arg-type]
        loop=loop,
        policy_client=_LongRaisingServerClient(),
        session_id="conv_trunc_test",
    )
    try:
        relay_info = json.loads((bridge_dir / _TOOL_RELAY_FILE).read_text())
        relay_url = relay_info["url"]
        relay_token = relay_info["token"]

        async with httpx.AsyncClient() as c:
            resp = await c.post(
                f"{relay_url}/policies/evaluate",
                json={"event": {}},
                headers={"Authorization": f"Bearer {relay_token}"},
                timeout=5.0,
            )

        assert resp.status_code == 502
        body = resp.text
        # The actionable prefix + leading cause survive; the tail is elided.
        assert body.startswith(
            "omnigent policy-eval proxy could not reach the Omnigent server: RequestError: "
        )
        assert body.endswith("...")
        # Bounded well under the raw 5000-char reason.
        assert len(body) < 500
    finally:
        relay.close()


# ---------------------------------------------------------------------------
# Agent switch: the relay must follow the session's current agent.
#
# The relay advertises a spec-derived tool surface. When the session moves to
# a different agent, the surface the native harness sees has to move with it —
# otherwise the harness keeps calling tools the new agent never granted, and
# same-named tools keep the previous agent's schema.
# ---------------------------------------------------------------------------


class _SwitchableServerClient:
    """Server client stub whose bound agent and bridge id can be reassigned.

    Mutating :attr:`agent_id` / :attr:`bridge_id` between requests simulates
    a server-side agent switch without restarting the runner: the runner
    re-reads both after ``POST /v1/sessions/{id}/reset-state`` drops its
    per-session caches.
    """

    def __init__(self, agent_id: str, bridge_id: str | None = None) -> None:
        """
        Initialize the stub.

        :param agent_id: Agent id reported by ``GET /v1/sessions/{id}``.
        :param bridge_id: Bridge id reported via the session labels
            endpoint. ``None`` omits the label so the runner falls back to
            the session id.
        :returns: None.
        """
        self.agent_id = agent_id
        self.bridge_id = bridge_id

    class _Response:
        """Stub 200 response carrying a caller-supplied JSON body."""

        status_code = 200

        def __init__(self, body: dict[str, Any]) -> None:
            """
            Initialize the response.

            :param body: JSON body to return from :meth:`json`.
            :returns: None.
            """
            self._body = body

        def json(self) -> dict[str, Any]:
            """Return the stub JSON body."""
            return self._body

        def raise_for_status(self) -> None:
            """No-op: stub always succeeds."""

    async def get(self, url: str, **kwargs: Any) -> _Response:
        """Serve the session snapshot and label reads the runner performs.

        :param url: Request URL, e.g. ``"/v1/sessions/conv_x/labels"``.
        :param kwargs: Extra keyword arguments (ignored).
        :returns: Stub 200 response.
        """
        del kwargs
        if url.endswith("/labels"):
            labels = {BRIDGE_ID_LABEL_KEY: self.bridge_id} if self.bridge_id else {}
            return self._Response({"labels": labels})
        return self._Response({"agent_id": self.agent_id})

    async def post(self, url: str, **kwargs: Any) -> _Response:
        """Return an empty 200 for any POST request."""
        del url, kwargs
        return self._Response({})

    async def patch(self, url: str, **kwargs: Any) -> _Response:
        """Return an empty 200 for any PATCH request."""
        del url, kwargs
        return self._Response({})


class _FailingResourceRegistry(_StubResourceRegistry):
    """Resource registry stub whose terminal launch can be made to fail.

    Setting :attr:`fail_launch` makes the next launch raise ``RuntimeError``,
    driving the runner's bridge-injected launch-failure rollback.
    """

    def __init__(self, tmp_path: Path) -> None:
        """
        Initialize the stub.

        :param tmp_path: Temporary directory returned as the default env root.
        :returns: None.
        """
        super().__init__(tmp_path)
        self.fail_launch = False

    async def _launch(self, **kwargs: Any) -> SessionResourceView:
        """Raise when armed, otherwise defer to the non-spawning stub."""
        if self.fail_launch:
            raise RuntimeError("simulated terminal launch failure")
        return await super()._launch(**kwargs)


@pytest.fixture
def terminal_registry_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Install a fresh ``TerminalRegistry`` as the runtime singleton.

    ``ToolManager`` registers ``sys_terminal_*`` for a spec that declares
    ``terminals:``, and looks the registry up via
    :func:`omnigent.runtime.get_terminal_registry`, which raises unless the
    runtime was initialized. These tests never run a real runtime, so they
    install the singleton directly — the same approach
    ``tests/runner/test_runner_dispatch.py`` uses for the relay schema
    builder.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    from omnigent.runtime import _globals as rt_globals
    from omnigent.terminals.registry import TerminalRegistry

    monkeypatch.setattr(rt_globals, "_terminal_registry", TerminalRegistry())


def _spec_with_terminals() -> AgentSpec:
    """Return a spec that grants the ``sys_terminal_*`` family."""
    return AgentSpec(
        spec_version=1,
        name="agent-with-terminals",
        terminals={"bash": TerminalEnvSpec(command="bash")},
    )


def _spec_without_terminals() -> AgentSpec:
    """Return a spec that grants no terminal tools."""
    return AgentSpec(spec_version=1, name="agent-without-terminals")


def _spec_with_sub_agent(sub_agent_name: str) -> AgentSpec:
    """
    Return a spec granting exactly one named sub-agent.

    ``sys_session_send`` derives its ``agent`` enum from this list, so two
    such specs advertise the same tool name with different schemas.

    :param sub_agent_name: Sub-agent name to grant, e.g. ``"alpha"``.
    :returns: Parent spec declaring that single sub-agent.
    """
    return AgentSpec(
        spec_version=1,
        name=f"parent-of-{sub_agent_name}",
        tools=ToolsConfig(agents=[sub_agent_name]),
        sub_agents=[AgentSpec(spec_version=1, name=sub_agent_name)],
    )


def _relay_tool_names(relay_file: Path) -> set[str]:
    """
    Return the tool names advertised in a ``tool_relay.json``.

    :param relay_file: Path to the relay advertisement.
    :returns: Advertised tool names.
    """
    return {t["name"] for t in json.loads(relay_file.read_text())["tools"]}


def _sub_agent_enum(relay_file: Path) -> set[str]:
    """
    Return the ``sys_session_send`` sub-agent enum from a relay file.

    :param relay_file: Path to the relay advertisement.
    :returns: Allowed ``agent`` values, empty when the tool is absent.
    """
    for tool in json.loads(relay_file.read_text())["tools"]:
        if tool["name"] == "sys_session_send":
            agent_prop = tool.get("parameters", {}).get("properties", {}).get("agent", {})
            if enum := agent_prop.get("enum"):
                return set(enum)
            for variant in agent_prop.get("anyOf", []):
                if "enum" in variant:
                    return set(variant["enum"])
    return set()


def _switch_app(
    tmp_path: Path,
    server_client: _SwitchableServerClient,
    specs: dict[str, AgentSpec],
    resource_registry: _StubResourceRegistry | None = None,
) -> FastAPI:
    """
    Build a runner app that resolves each agent id to a distinct spec.

    :param tmp_path: Pytest temp directory used for the stub env root.
    :param server_client: Stub server client reporting the bound agent.
    :param specs: Agent id → spec the resolver hands back.
    :param resource_registry: Registry stub. ``None`` builds the default
        non-spawning one.
    :returns: The runner FastAPI app.
    """

    async def spec_resolver(agent_id: str, session_id: str | None) -> AgentSpec | None:
        del session_id
        return specs.get(agent_id)

    return create_runner_app(
        resource_registry=resource_registry or _StubResourceRegistry(tmp_path),
        server_client=server_client,  # type: ignore[arg-type]  # duck-typed for test
        spec_resolver=spec_resolver,
    )


async def _launch_bridged(client: httpx.AsyncClient, session_id: str) -> None:
    """
    Launch the bridge-injected claude terminal, requiring success.

    :param client: HTTP client bound to the runner app.
    :param session_id: Session/conversation identifier.
    :returns: None.
    """
    resp = await _launch_terminal(client, session_id, bridge_inject_dir=True)
    assert resp.status_code == 200, f"terminal launch failed: {resp.text}"


async def _reset_state(client: httpx.AsyncClient, session_id: str) -> None:
    """
    Drop the runner's per-session caches, as an agent switch does.

    :param client: HTTP client bound to the runner app.
    :param session_id: Session/conversation identifier.
    :returns: None.
    """
    resp = await client.post(f"/v1/sessions/{session_id}/reset-state")
    assert resp.status_code == 200, f"reset-state failed: {resp.text}"


@pytest.mark.asyncio
async def test_agent_switch_replaces_relay_in_same_bridge_dir(
    tmp_path: Path,
    terminal_registry_singleton: None,
) -> None:
    """A switch to an agent without terminals drops the ``sys_terminal_*`` grant.

    Agent A declares ``terminals:``, so the relay advertises the terminal
    family. Agent B declares none. Once the session is switched and its
    caches are reset, the next relay lookup must rebuild the advertisement
    from agent B's spec — keeping agent A's terminal tools would let the
    harness call a family the current agent never granted.
    """
    agent_a, agent_b = "ag_terminals", "ag_plain"
    server_client = _SwitchableServerClient(agent_a)
    app = _switch_app(
        tmp_path,
        server_client,
        {agent_a: _spec_with_terminals(), agent_b: _spec_without_terminals()},
    )

    session_id = f"conv_{uuid.uuid4().hex[:12]}"
    bridge_dir = bridge_dir_for_bridge_id(session_id)
    prepare_bridge_dir(session_id, workspace=tmp_path)
    relay_file = bridge_dir / _TOOL_RELAY_FILE

    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://runner") as c:
            await _launch_bridged(c, session_id)
            before = _relay_tool_names(relay_file)
            # Precondition: agent A's spec gate really did grant the family.
            assert {n for n in before if n.startswith("sys_terminal_")}, (
                f"agent A declares terminals but advertised no sys_terminal_*: {before}"
            )

            server_client.agent_id = agent_b
            await _reset_state(c, session_id)
            await _launch_bridged(c, session_id)

            after = _relay_tool_names(relay_file)
            # The whole point of the fix: a stale relay would still be
            # advertising agent A's terminal family here.
            assert not {n for n in after if n.startswith("sys_terminal_")}, (
                f"terminal tools survived a switch to an agent without terminals: {after}"
            )
            # The rest of the always-on surface must still be advertised —
            # the relay was rebuilt, not torn down.
            assert "list_comments" in after
    finally:
        with contextlib.suppress(httpx.HTTPError):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://runner"
            ) as c:
                await c.delete(f"/v1/sessions/{session_id}")
        shutil.rmtree(bridge_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_agent_switch_moves_relay_to_new_bridge_dir(tmp_path: Path) -> None:
    """A switch that changes the bridge id relays into the new directory.

    Switching between native harness families reassigns the session's
    bridge id. The relay has to follow: the new bridge directory needs an
    advertisement (otherwise the new harness sees no relay tools at all),
    and the old directory's advertisement must be withdrawn.
    """
    agent_a, agent_b = "ag_bridge_a", "ag_bridge_b"
    bridge_a = f"bridge_{uuid.uuid4().hex[:10]}"
    bridge_b = f"bridge_{uuid.uuid4().hex[:10]}"
    server_client = _SwitchableServerClient(agent_a, bridge_id=bridge_a)
    app = _switch_app(
        tmp_path,
        server_client,
        {agent_a: _spec_without_terminals(), agent_b: _spec_without_terminals()},
    )

    session_id = f"conv_{uuid.uuid4().hex[:12]}"
    dir_a = bridge_dir_for_bridge_id(bridge_a)
    dir_b = bridge_dir_for_bridge_id(bridge_b)
    prepare_bridge_dir(bridge_a, workspace=tmp_path)
    prepare_bridge_dir(bridge_b, workspace=tmp_path)

    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://runner") as c:
            await _launch_bridged(c, session_id)
            assert (dir_a / _TOOL_RELAY_FILE).exists(), "no relay written for the first bridge"

            server_client.agent_id = agent_b
            server_client.bridge_id = bridge_b
            await _reset_state(c, session_id)
            await _launch_bridged(c, session_id)

            # Without the fix the cached relay short-circuits before the new
            # bridge dir is computed, so the new harness gets nothing.
            assert (dir_b / _TOOL_RELAY_FILE).exists(), (
                "the new bridge directory received no relay after the switch"
            )
            # The superseded relay is closed, which unlinks its own file.
            assert not (dir_a / _TOOL_RELAY_FILE).exists(), (
                "the previous bridge directory kept a stale relay advertisement"
            )
    finally:
        with contextlib.suppress(httpx.HTTPError):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://runner"
            ) as c:
                await c.delete(f"/v1/sessions/{session_id}")
        for bdir in (dir_a, dir_b):
            shutil.rmtree(bdir, ignore_errors=True)


@pytest.mark.asyncio
async def test_agent_switch_rebuilds_same_named_tool_schema(tmp_path: Path) -> None:
    """Two agents advertising the same tool names get their own schemas.

    Both agents grant ``sys_session_send``, so the advertised name set is
    identical and a name-level comparison would see no change. The schemas
    differ: each enumerates only its own sub-agent. After a switch the
    harness must be handed agent B's contract.
    """
    agent_a, agent_b = "ag_alpha", "ag_beta"
    server_client = _SwitchableServerClient(agent_a)
    app = _switch_app(
        tmp_path,
        server_client,
        {agent_a: _spec_with_sub_agent("alpha"), agent_b: _spec_with_sub_agent("beta")},
    )

    session_id = f"conv_{uuid.uuid4().hex[:12]}"
    bridge_dir = bridge_dir_for_bridge_id(session_id)
    prepare_bridge_dir(session_id, workspace=tmp_path)
    relay_file = bridge_dir / _TOOL_RELAY_FILE

    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://runner") as c:
            await _launch_bridged(c, session_id)
            names_before = _relay_tool_names(relay_file)
            # Precondition: the spec gate produced the sub-agent enum this
            # test discriminates on.
            assert _sub_agent_enum(relay_file) == {"alpha"}

            server_client.agent_id = agent_b
            await _reset_state(c, session_id)
            await _launch_bridged(c, session_id)

            # Same tool names on both sides — only the schema moved, which is
            # exactly the case a name-only refresh check would miss.
            assert _relay_tool_names(relay_file) == names_before
            assert _sub_agent_enum(relay_file) == {"beta"}, (
                "sys_session_send still advertises the previous agent's sub-agents"
            )
    finally:
        with contextlib.suppress(httpx.HTTPError):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://runner"
            ) as c:
                await c.delete(f"/v1/sessions/{session_id}")
        shutil.rmtree(bridge_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_failed_launch_rolls_back_the_relay_it_started(tmp_path: Path) -> None:
    """A failed bridge-injected launch withdraws the relay that launch installed.

    The rollback used to be gated on "was a relay already present", which a
    relay left over from the previous agent makes true — so the relay this
    launch built for the new agent stayed bound and advertised even though
    the terminal never came up. Rollback is keyed on the relay instance
    instead, so what this launch installed is what it removes.
    """
    agent_a, agent_b = "ag_first", "ag_second"
    server_client = _SwitchableServerClient(agent_a)
    registry = _FailingResourceRegistry(tmp_path)
    app = _switch_app(
        tmp_path,
        server_client,
        {agent_a: _spec_without_terminals(), agent_b: _spec_with_sub_agent("beta")},
        resource_registry=registry,
    )

    session_id = f"conv_{uuid.uuid4().hex[:12]}"
    bridge_dir = bridge_dir_for_bridge_id(session_id)
    prepare_bridge_dir(session_id, workspace=tmp_path)
    relay_file = bridge_dir / _TOOL_RELAY_FILE

    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://runner") as c:
            await _launch_bridged(c, session_id)
            assert relay_file.exists()  # precondition: agent A's relay is up

            server_client.agent_id = agent_b
            await _reset_state(c, session_id)

            registry.fail_launch = True
            failed = await _launch_terminal(c, session_id, bridge_inject_dir=True)
            assert failed.status_code == 500

            assert not relay_file.exists(), (
                "the relay this failed launch installed stayed bound and advertised"
            )
    finally:
        with contextlib.suppress(httpx.HTTPError):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://runner"
            ) as c:
                await c.delete(f"/v1/sessions/{session_id}")
        shutil.rmtree(bridge_dir, ignore_errors=True)
