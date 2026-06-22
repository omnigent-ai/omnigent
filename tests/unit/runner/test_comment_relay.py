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

from omnigent.claude_native_bridge import bridge_dir_for_bridge_id, prepare_bridge_dir
from omnigent.entities.session_resources import SessionResourceView, terminal_resource_view
from omnigent.inner.datamodel import TerminalEnvSpec
from omnigent.runner import create_runner_app
from omnigent.terminals import TerminalListEntry
from tests.unit.runner.helpers import NullServerClient, make_test_terminal_instance

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
