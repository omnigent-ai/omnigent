"""Tests for runner app integration with the sessions-native event path.

Verifies the `_stream_message_to_harness` shared helper covers both
``POST /v1/responses`` (legacy) and ``POST /v1/sessions/{conv}/events``
(sessions-native). Both paths must inject MCP schemas, stamp the
``omnigent_runner_dispatched`` marker on intercepted events, and
route MCP dispatch to the runner manager.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shutil
import sys
import threading
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI

from omnigent import (
    claude_native_bridge,
    codex_native_bridge,
    cursor_native_bridge,
    kiro_native_bridge,
)
from omnigent.antigravity_native_bridge import (
    ANTIGRAVITY_NATIVE_BRIDGE_ID_LABEL_KEY,
)
from omnigent.antigravity_native_bridge import (
    is_placeholder_conversation_id as bridge_mod_is_placeholder,
)
from omnigent.claude_native_bridge import (
    BRIDGE_ID_LABEL_KEY,
    bridge_dir_for_bridge_id,
    bridge_dir_for_conversation_id,
    prepare_bridge_dir,
    read_permission_hook_config,
)
from omnigent.entities.session_resources import SessionResourceView, terminal_resource_id
from omnigent.inner.terminal import TerminalInstance
from omnigent.runner import create_runner_app
from omnigent.runner import tool_dispatch as _tool_dispatch
from omnigent.runner.app import (
    _RUNNER_DISPATCHED_FIELD,
    _WAKE_POST_MAX_ATTEMPTS,
    ResolvedSpec,
    _agent_os_env_from_spec,
    _auto_create_antigravity_terminal,
    _auto_create_claude_terminal,
    _auto_create_codex_terminal,
    _auto_create_cursor_terminal,
    _auto_create_kiro_terminal,
    _auto_create_pi_terminal,
    _auto_create_repl_terminal,
    _deliver_subagent_wake_post,
    _KiroNativeLaunchConfig,
    _log_terminal_lookup_miss,
    _PiNativeLaunchConfig,
    _publish_native_terminal_start_error,
    _publish_terminal_pending,
    _resolved_workdir_for_spec,
    _session_labels_for_runner_spawn,
    _terminal_lookup_miss_log_state,
    _wake_post_is_retryable,
)
from omnigent.runner.mcp_manager import McpSchemasResult
from omnigent.runner.resource_registry import (
    ANTIGRAVITY_NATIVE_TERMINAL_ROLE,
    CLAUDE_NATIVE_TERMINAL_ROLE,
    CODEX_NATIVE_TERMINAL_ROLE,
    KIRO_NATIVE_TERMINAL_ROLE,
    OMNIGENT_REPL_TERMINAL_ROLE,
    PI_NATIVE_TERMINAL_ROLE,
    SessionResourceRegistry,
)
from omnigent.spec.types import AgentSpec, ExecutorSpec, LocalToolInfo, MCPServerConfig
from omnigent.terminals import TerminalRegistry
from tests.runner.helpers import NullServerClient

# ── Fakes for the runner's collaborators ──────────────────────────────


class _FakeMcpManager:
    """Stand-in for RunnerMcpManager that returns scripted schemas/names."""

    handles_tool_dispatch = True

    def __init__(self, *, tool_name: str = "jira_search_issues") -> None:
        """Schema set is a single-tool jira fixture."""
        self._tool_name = tool_name
        self.call_tool_invocations: list[tuple[str, dict[str, Any]]] = []

    async def schemas_for(self, spec: AgentSpec) -> McpSchemasResult:
        """Return one MCP schema with the configured tool name."""
        del spec
        schema = {
            "type": "function",
            "name": self._tool_name,
            "description": "fake mcp tool",
            "parameters": {"type": "object", "properties": {}},
        }
        return McpSchemasResult(schemas=[schema], tool_names={self._tool_name}, failures={})

    async def call_tool(
        self,
        spec: AgentSpec,
        tool_name: str,
        arguments: dict[str, Any],
        **_kwargs: Any,
    ) -> str:
        """Record the dispatch + return a fixed reply."""
        del spec
        self.call_tool_invocations.append((tool_name, arguments))
        return f"called {tool_name}"


class _ScriptedHarnessClient:
    """Records every POST body; streams a scripted SSE response on request."""

    def __init__(
        self,
        sse_frames: list[str],
        *,
        stream_finished: asyncio.Event | None = None,
    ) -> None:
        """
        Initialize with the SSE frames to relay.

        :param sse_frames: SSE frames returned by the harness stream.
        :param stream_finished: Optional event set after ``aiter_text``
            exhausts the scripted frames.
        :returns: None.
        """
        self.posted_bodies: list[dict[str, Any]] = []
        self._sse_frames = sse_frames
        self._stream_finished = stream_finished
        self.patched_events: list[dict[str, Any]] = []

    def stream(self, method: str, url: str, *, json: dict[str, Any], timeout: Any) -> Any:
        """Capture body + return a context manager streaming scripted frames."""
        del method, url, timeout
        self.posted_bodies.append(json)
        scripted = self._sse_frames
        stream_finished = self._stream_finished

        class _StreamCtx:
            status_code = 200

            async def __aenter__(self) -> _ScriptedHarnessClient._StreamHandle:
                return _ScriptedHarnessClient._StreamHandle(scripted, stream_finished)

            async def __aexit__(self, *_: Any) -> None:
                return None

        return _StreamCtx()

    class _StreamHandle:
        status_code = 200

        def __init__(
            self,
            frames: list[str],
            stream_finished: asyncio.Event | None,
        ) -> None:
            """
            Initialize a scripted stream handle.

            :param frames: SSE frame strings to yield.
            :param stream_finished: Optional event set after all frames are
                yielded.
            :returns: None.
            """
            self._frames = frames
            self._stream_finished = stream_finished

        async def aiter_text(self) -> AsyncIterator[str]:
            """
            Yield scripted SSE frame text and signal exhaustion.

            :returns: Async iterator of SSE frame text chunks.
            """
            try:
                for frame in self._frames:
                    yield frame
            finally:
                if self._stream_finished is not None:
                    self._stream_finished.set()

    async def post(self, url: str, *, json: dict[str, Any], timeout: Any = None) -> Any:
        """PATCH the result back to the harness — record body and return 200."""
        del url, timeout
        self.patched_events.append(json)

        class _Response:
            status_code = 200
            headers: dict[str, str] = {}
            content = b""

            def raise_for_status(self) -> None:
                pass

        return _Response()


class _FakeProcessManager:
    """ProcessManager stub that returns a single ScriptedHarnessClient."""

    handles_tool_dispatch = True

    def __init__(self, client: _ScriptedHarnessClient) -> None:
        """Wrap *client* so :meth:`get_client` returns it."""
        self._client = client
        self._sessions: set[str] = set()
        self._active_turns: set[str] = set()
        self.released: list[str] = []
        self.cancelled: list[str] = []
        self.get_client_calls: list[tuple[str, str, dict[str, str] | None]] = []

    async def get_client(
        self, conversation_id: str, harness: str, env: Any = None
    ) -> _ScriptedHarnessClient:
        """Return the fixed scripted client."""
        self.get_client_calls.append((conversation_id, harness, env))
        self._sessions.add(conversation_id)
        return self._client

    def has_session(self, conversation_id: str) -> bool:
        """Check if a session was registered via :meth:`get_client`."""
        return conversation_id in self._sessions

    def has_active_turn(self, conversation_id: str) -> bool:
        """Check if a turn is marked active for this conversation."""
        return conversation_id in self._active_turns

    def mark_turn_active(self, conversation_id: str) -> None:
        """Mark a conversation as having an active turn (test helper)."""
        self._active_turns.add(conversation_id)

    async def forward_cancel(self, conversation_id: str) -> bool:
        """Record a cancel and return ``True``."""
        self.cancelled.append(conversation_id)
        return True

    async def release(self, conversation_id: str) -> None:
        """Record a release and remove the session."""
        self.released.append(conversation_id)
        self._sessions.discard(conversation_id)


class _ReadTimeoutTransport(httpx.AsyncBaseTransport):
    """Transport that raises ``ReadTimeout`` for every request."""

    def __init__(self) -> None:
        """
        Initialize request capture.

        :returns: None.
        """
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """
        Record *request* and raise a read timeout.

        :param request: Outbound request from ``httpx.AsyncClient``.
        :returns: Never returns; raises ``httpx.ReadTimeout``.
        :raises httpx.ReadTimeout: Always raised to simulate Omnigent slowness.
        """
        self.requests.append(request)
        raise httpx.ReadTimeout("session lookup timed out", request=request)


async def _spec_resolver_returning(spec: AgentSpec) -> Any:
    """Build an async spec_resolver that always returns *spec*."""

    async def _resolve(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    return _resolve


# ── Shared helpers ────────────────────────────────────────────────────


def _sse(event: dict[str, Any]) -> str:
    """Render one SSE ``data: {json}\\n\\n`` frame from *event*."""
    return f"data: {json.dumps(event)}\n\n"


class _McpToolsListServerClient(NullServerClient):
    """Server client stub that handles MCP tools/list and tools/call requests.

    Returns a scripted tool schema for tools/list calls and records tools/call
    invocations.  All other requests are handled by :class:`NullServerClient`
    (empty 200).  Used in tests that exercise MCP schema injection and tool
    dispatch through :class:`ProxyMcpManager`.
    """

    def __init__(self, tool_name: str) -> None:
        """Configure the tool name returned by tools/list.

        :param tool_name: MCP tool name to advertise, e.g. ``"jira_search_issues"``.
        """
        self._tool_name = tool_name
        self.call_tool_invocations: list[tuple[str, dict[str, Any]]] = []

    async def post(self, url: str, **kwargs: Any) -> NullServerClient._Response:
        """Handle MCP endpoint requests and delegate others to null parent.

        :param url: Request URL. If it ends with ``/mcp``, handles tools/list
            and tools/call JSON-RPC calls.  Otherwise delegates to the null parent.
        :param kwargs: Extra keyword arguments (forwarded for non-MCP calls).
        :returns: Stub 200 response with appropriate payload.
        """
        if url.endswith("/mcp"):
            body = kwargs.get("json", {})
            if isinstance(body, dict) and body.get("method") == "tools/list":

                class _ToolsListResponse(NullServerClient._Response):
                    def __init__(self, tool_name: str) -> None:
                        self._tool_name = tool_name

                    def json(self) -> dict[str, Any]:
                        return {
                            "result": {
                                "tools": [
                                    {
                                        "name": self._tool_name,
                                        "description": "fake mcp tool",
                                        "inputSchema": {
                                            "type": "object",
                                            "properties": {},
                                        },
                                    }
                                ]
                            }
                        }

                return _ToolsListResponse(self._tool_name)  # type: ignore[return-value]

            if isinstance(body, dict) and body.get("method") == "tools/call":
                params = body.get("params", {})
                tool_name = params.get("name", "")
                arguments = params.get("arguments", {})
                self.call_tool_invocations.append((tool_name, arguments))

                class _ToolsCallResponse(NullServerClient._Response):
                    def __init__(self, tn: str) -> None:
                        self._tn = tn

                    def json(self) -> dict[str, Any]:
                        return {
                            "result": {"content": [{"type": "text", "text": f"called {self._tn}"}]}
                        }

                return _ToolsCallResponse(tool_name)  # type: ignore[return-value]

        return await super().post(url, **kwargs)


def _build_app_with_mcp_tool(
    tool_name: str = "jira_search_issues",
) -> tuple[FastAPI, _FakeMcpManager, _ScriptedHarnessClient, _McpToolsListServerClient]:
    """Wire a runner app with the fakes and one mcp tool name.

    :param tool_name: MCP tool name to advertise and dispatch, e.g.
        ``"jira_search_issues"``.
    :returns: ``(app, mcp_manager, harness_client, server_client)`` tuple.
        ``mcp_manager`` is the :class:`_FakeMcpManager` used for the runner's
        ``/mcp/execute`` endpoint (not inline turn dispatch).
        ``server_client`` records tools/call invocations from inline turn dispatch
        via :class:`ProxyMcpManager`.
    """
    spec = AgentSpec(
        spec_version=1,
        name="t",
        mcp_servers=[MCPServerConfig(name="jira", transport="http", url="http://x")],
    )

    sse_frames = [
        _sse({"type": "response.created", "response": {"id": "resp_abc"}}),
        _sse(
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "status": "action_required",
                    "name": tool_name,
                    "call_id": "call_1",
                    "arguments": "{}",
                },
            }
        ),
    ]
    harness_client = _ScriptedHarnessClient(sse_frames)
    mcp_manager = _FakeMcpManager(tool_name=tool_name)
    server_client = _McpToolsListServerClient(tool_name)
    pm = _FakeProcessManager(harness_client)

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server_client,  # type: ignore[arg-type]
        mcp_manager=mcp_manager,
    )
    return app, mcp_manager, harness_client, server_client


@contextlib.asynccontextmanager
async def _runner_client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """ASGI test client for the runner app."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        yield client


@pytest.mark.asyncio
async def test_session_labels_for_runner_spawn_timeout_is_quiet(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    Timed-out optional label resolution returns the spawn fallback quietly.

    Native harness spawn can recover by using the session id when labels
    cannot be fetched. A slow Omnigent session lookup therefore must not emit a
    warning with traceback; that was noisy and misleading for a best-effort
    lookup.

    :param caplog: Pytest log capture fixture.
    :returns: None.
    """
    transport = _ReadTimeoutTransport()
    async with httpx.AsyncClient(transport=transport, base_url="http://ap") as client:
        with caplog.at_level(logging.DEBUG, logger="omnigent.runner.app"):
            labels = await _session_labels_for_runner_spawn(
                server_client=client,
                session_id="conv_slow",
            )

    assert labels == {}
    assert [(request.method, request.url.path) for request in transport.requests] == [
        ("GET", "/v1/sessions/conv_slow/labels")
    ]
    timeout = transport.requests[0].extensions.get("timeout")
    assert isinstance(timeout, dict)
    assert timeout["read"] == 1.0

    timeout_records = [
        record
        for record in caplog.records
        if "Timed out resolving session labels" in record.getMessage()
    ]
    assert len(timeout_records) == 1
    assert timeout_records[0].levelno == logging.DEBUG
    assert timeout_records[0].exc_info is None
    assert "Failed to resolve session labels" not in caplog.text


@pytest.mark.asyncio
async def test_session_labels_for_runner_spawn_empty_200_body_recovers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    A 200 response with an empty (non-JSON) body returns the fallback.

    The Databricks Apps proxy can return HTTP 200 with an empty body
    when the server event loop is starved. Parsing that with
    ``resp.json()`` raises ``JSONDecodeError``; left unguarded it
    propagated out of ``_ensure_comment_relay_started`` and aborted
    every message turn before any LLM call (observed in production:
    "turn setup failed: Expecting value: line 1 column 1 (char 0)").
    Labels are a best-effort spawn hint, so a bad body must degrade to
    ``{}`` like the timeout / non-200 paths — not raise.

    :param caplog: Pytest log capture fixture.
    :returns: None.
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        # 200 with an empty body — the exact proxy-under-load shape.
        return httpx.Response(200, content=b"")

    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://ap") as client:
        with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
            labels = await _session_labels_for_runner_spawn(
                server_client=client,
                session_id="conv_empty",
            )

    # Recovered to the fallback instead of raising JSONDecodeError —
    # if the guard is removed, this call raises and the test errors out.
    assert labels == {}
    # The non-JSON 200 is logged once at WARNING with no traceback;
    # absence of this record would mean the bad body was swallowed
    # silently (or, worse, that the guard never ran).
    json_records = [
        record
        for record in caplog.records
        if "Session labels response was not valid JSON" in record.getMessage()
    ]
    assert len(json_records) == 1
    assert json_records[0].levelno == logging.WARNING


class _FakeFileServerClient:
    """Minimal server client for runner-side file_id resolution tests."""

    def __init__(self) -> None:
        self.get_calls: list[str] = []

    async def get(self, url: str, **kwargs: Any) -> Any:
        del kwargs
        self.get_calls.append(url)

        class _Response:
            def __init__(
                self, *, body: bytes = b"", payload: dict[str, Any] | None = None
            ) -> None:
                self.content = body
                self._payload = payload or {}
                self.headers = {"content-type": self._payload.get("content_type", "image/png")}
                self.status_code = 200

            def json(self) -> dict[str, Any]:
                return self._payload

            def raise_for_status(self) -> None:
                return None

        if url.endswith("/content"):
            return _Response(body=b"png-bytes")
        return _Response(
            payload={"id": "file_img", "filename": "photo.png", "content_type": "image/png"}
        )


@pytest.mark.asyncio
async def test_sessions_native_resolves_file_id_before_harness() -> None:
    """Remote runner resolves raw AP-web ``file_id`` blocks before harness input."""
    harness_client = _ScriptedHarnessClient(
        [_sse({"type": "response.completed", "response": {"id": "resp_1"}})]
    )
    pm = _FakeProcessManager(harness_client)
    server_client = _FakeFileServerClient()
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=server_client,  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions/conv_file/events",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "ag_abc",
                "model": "test-agent",
                "content": [
                    {"type": "input_image", "file_id": "file_img", "filename": "photo.png"},
                    {"type": "input_text", "text": "what is this?"},
                ],
            },
        )

    assert resp.status_code == 202
    assert server_client.get_calls == [
        # file_id blocks are resolved first (before the harness sees them)...
        "/v1/sessions/conv_file/resources/files/file_img",
        "/v1/sessions/conv_file/resources/files/file_img/content",
        # ...then the cold in-memory cache is rehydrated from the store
        # (empty here) before the turn is dispatched.
        "/v1/sessions/conv_file/items",
    ]
    for _ in range(20):
        if harness_client.posted_bodies:
            break
        await asyncio.sleep(0.05)
    posted = harness_client.posted_bodies[0]
    image_block = posted["content"][0]["content"][0]
    assert image_block == {
        "type": "input_image",
        "filename": "photo.png",
        "image_url": "data:image/png;base64,cG5nLWJ5dGVz",
    }
    assert "file_id" not in image_block


# ── Tests ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_runner_session_tool_schemas_use_resolved_bundle_workdir(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    tool_dir = bundle_dir / "tools" / "python"
    tool_dir.mkdir(parents=True)
    (tool_dir / "bundle_tool.py").write_text(
        "from omnigent_client.tools import tool\n\n"
        "@tool\n"
        "def bundle_tool(text: str) -> str:\n"
        "    return text\n"
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    spec = AgentSpec(
        spec_version=1,
        name="bundle-agent",
        local_tools=[
            LocalToolInfo(
                name="bundle_tool",
                path="tools/python/bundle_tool.py",
                language="python",
            )
        ],
    )
    sse_frames = [
        _sse({"type": "response.created", "response": {"id": "resp_1"}}),
        _sse(
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "status": "action_required",
                    "name": "bundle_tool",
                    "call_id": "call_bundle",
                    "arguments": json.dumps({"text": "from-bundle"}),
                },
            }
        ),
        _sse({"type": "response.completed", "response": {"id": "resp_1"}}),
    ]
    harness_client = _ScriptedHarnessClient(sse_frames)
    pm = _FakeProcessManager(harness_client)

    async def _resolver(agent_id: str, session_id: str | None = None) -> ResolvedSpec:
        del agent_id, session_id
        return ResolvedSpec(spec=spec, workdir=bundle_dir)

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
        runner_workspace=workspace,
    )
    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions/conv_bundle/events",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "ag_bundle",
                "model": "bundle-agent",
                "content": [{"type": "input_text", "text": "hi"}],
                "harness": "openai-agents",
            },
        )
        assert resp.status_code == 202
        for _ in range(20):
            if harness_client.posted_bodies:
                break
            await asyncio.sleep(0.05)
        for _ in range(100):
            if harness_client.patched_events:
                break
            await asyncio.sleep(0.05)

    assert harness_client.posted_bodies, "harness must receive the turn"
    schemas = harness_client.posted_bodies[0].get("tools") or []
    assert any(s.get("function", {}).get("name") == "bundle_tool" for s in schemas), (
        f"expected bundled local tool schema, got {schemas}"
    )


def test_resolved_workdir_for_spec_prefers_bundle_workdir(tmp_path: Path) -> None:
    """``_resolved_workdir_for_spec`` uses ``ResolvedSpec.workdir`` over fallback.

    Bundle-deployed agents carry their own workdir (where
    ``tools/python/*.py`` live). The dispatch path must thread that
    workdir into ``dispatch_tool_locally`` so native python tools are
    found at call time — not the generic ``runner_workspace``.
    """
    bundle_dir = tmp_path / "bundle"
    runner_workspace = tmp_path / "workspace"
    spec = AgentSpec(spec_version=1, name="bundle-agent")
    entry = ResolvedSpec(spec=spec, workdir=bundle_dir)

    assert _resolved_workdir_for_spec(entry, runner_workspace) == bundle_dir


def test_resolved_workdir_for_spec_falls_back_without_bundle(tmp_path: Path) -> None:
    """Non-bundle specs fall back to ``runner_workspace`` (prior behavior).

    A bare ``AgentSpec`` (no ResolvedSpec wrapper) or a ``ResolvedSpec``
    with ``workdir=None`` carries no bundle dir, so dispatch must keep
    using the CLI launch workspace exactly as base did.
    """
    runner_workspace = tmp_path / "workspace"
    bare_spec = AgentSpec(spec_version=1, name="plain-agent")

    # Unwrapped spec → no workdir → fallback.
    assert _resolved_workdir_for_spec(bare_spec, runner_workspace) == runner_workspace
    # ResolvedSpec with no workdir → fallback.
    wrapped_no_workdir = ResolvedSpec(spec=bare_spec, workdir=None)
    assert _resolved_workdir_for_spec(wrapped_no_workdir, runner_workspace) == runner_workspace
    # Missing fallback stays None (don't fabricate a path).
    assert _resolved_workdir_for_spec(bare_spec, None) is None


@pytest.mark.asyncio
async def test_sessions_native_dispatches_native_tool_with_bundle_workdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bundle agent's native python tool dispatches against the bundle workdir.

    End-to-end through ``POST /v1/sessions/{conv}/events`` (no live LLM):
    the scripted harness emits an ``action_required`` for a spec-declared
    python tool, and the runner must dispatch it locally with
    ``runner_workspace`` set to the resolved ``ResolvedSpec.workdir`` (the
    bundle dir), not the generic CLI ``runner_workspace``. This is the
    dispatch-time counterpart to
    :func:`test_runner_session_tool_schemas_use_resolved_bundle_workdir`,
    which only proved schema generation used the bundle workdir.
    """
    bundle_dir = tmp_path / "bundle"
    tool_dir = bundle_dir / "tools" / "python"
    tool_dir.mkdir(parents=True)
    (tool_dir / "bundle_tool.py").write_text(
        "from omnigent_client.tools import tool\n\n"
        "@tool\n"
        "def bundle_tool(text: str) -> str:\n"
        "    return text\n"
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    spec = AgentSpec(
        spec_version=1,
        name="bundle-agent",
        local_tools=[
            LocalToolInfo(
                name="bundle_tool",
                path="tools/python/bundle_tool.py",
                language="python",
            )
        ],
    )

    captured_workspaces: list[Path | None] = []

    async def _fake_dispatch(*, runner_workspace: Path | None = None, **kwargs: Any) -> str:
        captured_workspaces.append(runner_workspace)
        return "ok"

    monkeypatch.setattr(_tool_dispatch, "dispatch_tool_locally", _fake_dispatch)

    sse_frames = [
        _sse({"type": "response.created", "response": {"id": "resp_1"}}),
        _sse(
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "status": "action_required",
                    "name": "bundle_tool",
                    "call_id": "call_bundle",
                    "arguments": json.dumps({"text": "from-bundle"}),
                },
            }
        ),
        _sse({"type": "response.completed", "response": {"id": "resp_1"}}),
    ]
    harness_client = _ScriptedHarnessClient(sse_frames)
    pm = _FakeProcessManager(harness_client)

    async def _resolver(agent_id: str, session_id: str | None = None) -> ResolvedSpec:
        del agent_id, session_id
        return ResolvedSpec(spec=spec, workdir=bundle_dir)

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
        runner_workspace=workspace,
    )
    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions/conv_bundle/events",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "ag_bundle",
                "model": "bundle-agent",
                "content": [{"type": "input_text", "text": "hi"}],
                "harness": "openai-agents",
            },
        )
        assert resp.status_code == 202
        for _ in range(100):
            if captured_workspaces:
                break
            await asyncio.sleep(0.05)

    assert captured_workspaces, "native tool must be dispatched locally"
    assert captured_workspaces[0] == bundle_dir, (
        "dispatch must use the resolved bundle workdir, not runner_workspace "
        f"({workspace!r}); got {captured_workspaces[0]!r}"
    )


@pytest.mark.asyncio
async def test_sessions_native_dispatches_builtin_tool_with_runner_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bundle agent's builtin OS-env tool dispatches in runner_workspace.

    Bundle workdirs are only for spec-local native python tools. Builtins
    such as ``sys_os_write`` run in the caller process and must keep the
    original runner workspace even when the agent spec was resolved from an
    extracted bundle directory.
    """
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    spec = AgentSpec(spec_version=1, name="bundle-agent")

    captured_workspaces: list[Path | None] = []

    async def _fake_dispatch(*, runner_workspace: Path | None = None, **kwargs: Any) -> str:
        captured_workspaces.append(runner_workspace)
        return "ok"

    monkeypatch.setattr(_tool_dispatch, "dispatch_tool_locally", _fake_dispatch)

    sse_frames = [
        _sse({"type": "response.created", "response": {"id": "resp_1"}}),
        _sse(
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "status": "action_required",
                    "name": "sys_os_write",
                    "call_id": "call_write",
                    "arguments": json.dumps(
                        {"path": "created-by-tool.txt", "content": "from workspace"}
                    ),
                },
            }
        ),
        _sse({"type": "response.completed", "response": {"id": "resp_1"}}),
    ]
    harness_client = _ScriptedHarnessClient(sse_frames)
    pm = _FakeProcessManager(harness_client)

    async def _resolver(agent_id: str, session_id: str | None = None) -> ResolvedSpec:
        del agent_id, session_id
        return ResolvedSpec(spec=spec, workdir=bundle_dir)

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
        runner_workspace=workspace,
    )
    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions/conv_builtin/events",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "ag_bundle",
                "model": "bundle-agent",
                "content": [{"type": "input_text", "text": "write a file"}],
                "harness": "openai-agents",
            },
        )
        assert resp.status_code == 202
        for _ in range(100):
            if captured_workspaces:
                break
            await asyncio.sleep(0.05)

    assert captured_workspaces, "builtin tool must be dispatched locally"
    assert captured_workspaces[0] == workspace, (
        "builtin OS-env dispatch must use runner_workspace, not the bundle workdir "
        f"({bundle_dir!r}); got {captured_workspaces[0]!r}"
    )


@pytest.mark.asyncio
async def test_mcp_execute_dispatches_builtin_tool_with_runner_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``/mcp/execute`` also keeps builtin OS-env tools in runner_workspace."""
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    spec = AgentSpec(spec_version=1, name="bundle-agent")

    captured_workspaces: list[Path | None] = []

    async def _fake_execute_tool(*, runner_workspace: Path | None = None, **kwargs: Any) -> str:
        captured_workspaces.append(runner_workspace)
        return "ok"

    monkeypatch.setattr(_tool_dispatch, "execute_tool", _fake_execute_tool)

    harness_client = _ScriptedHarnessClient(
        [_sse({"type": "response.completed", "response": {"id": "resp_1"}})]
    )
    pm = _FakeProcessManager(harness_client)

    async def _resolver(agent_id: str, session_id: str | None = None) -> ResolvedSpec:
        del agent_id, session_id
        return ResolvedSpec(spec=spec, workdir=bundle_dir)

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
        runner_workspace=workspace,
    )
    async with _runner_client(app) as client:
        seed_resp = await client.post(
            "/v1/sessions/conv_execute_builtin/events",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "ag_bundle",
                "model": "bundle-agent",
                "content": [{"type": "input_text", "text": "seed"}],
                "harness": "openai-agents",
            },
        )
        assert seed_resp.status_code == 202
        for _ in range(100):
            if harness_client.posted_bodies:
                break
            await asyncio.sleep(0.05)

        execute_resp = await client.post(
            "/v1/sessions/conv_execute_builtin/mcp/execute",
            json={
                "method": "tools/call",
                "params": {
                    "name": "sys_os_write",
                    "arguments": {"path": "created-by-tool.txt", "content": "from workspace"},
                },
            },
        )

    assert execute_resp.status_code == 200
    assert execute_resp.json() == {"result": {"output": "ok"}}
    assert captured_workspaces == [workspace]


@pytest.mark.asyncio
async def test_mcp_execute_dispatches_full_namespaced_mcp_tool_name() -> None:
    """``/mcp/execute`` must not strip the MCP server prefix before dispatch."""
    app, mcp_manager, _harness_client, _server_client = _build_app_with_mcp_tool(
        tool_name="jira__search_issues"
    )
    async with _runner_client(app) as client:
        seed_resp = await client.post(
            "/v1/sessions",
            json={"session_id": "conv_execute_mcp", "agent_id": "ag_abc"},
        )
        assert seed_resp.status_code == 201, seed_resp.text

        execute_resp = await client.post(
            "/v1/sessions/conv_execute_mcp/mcp/execute",
            json={
                "method": "tools/call",
                "params": {
                    "name": "jira__search_issues",
                    "arguments": {"query": "asyncio"},
                },
            },
        )

    assert execute_resp.status_code == 200
    assert execute_resp.json() == {"result": {"output": "called jira__search_issues"}}
    assert mcp_manager.call_tool_invocations == [("jira__search_issues", {"query": "asyncio"})]


@pytest.mark.asyncio
async def test_sessions_native_path_injects_mcp_schemas() -> None:
    """``POST /v1/sessions/{conv}/events`` with a message body injects MCP schemas.

    Sessions-native clients must get the same MCP injection that the
    legacy ``/v1/responses`` path provides.
    """
    app, _mcp_manager, harness_client, _server_client = _build_app_with_mcp_tool()
    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions/conv_abc/events",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "ag_abc",
                "model": "test-agent",
                "input": [{"type": "input_text", "text": "hi"}],
                "harness": "openai-agents",
                "has_mcp_servers": True,
            },
        )
        # Sessions-native POST returns 202; the turn runs as a
        # background task. Wait for the background turn to complete.
        assert resp.status_code == 202
        await asyncio.sleep(0.1)

    assert harness_client.posted_bodies, "harness must receive at least one event"
    body = harness_client.posted_bodies[0]
    schemas = body.get("tools") or []
    assert any(s.get("name") == "jira_search_issues" for s in schemas), (
        f"MCP schema must be injected on sessions-native path; got {schemas}"
    )


@pytest.mark.asyncio
async def test_action_required_marker_round_trips_to_relayed_frame() -> None:
    """The runner stamps ``omnigent_runner_dispatched`` on action_required frames.

    The Omnigent executor's ``_runner_dispatches`` predicate reads this marker
    to skip server-side dispatch. Without the stamp it'd race the
    runner's dispatch and return "unknown server-side tool."
    """
    app, _mcp_manager, _client, server_client = _build_app_with_mcp_tool()
    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions/conv_abc/events?stream=true",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "ag_abc",
                "model": "test-agent",
                "content": [{"type": "input_text", "text": "hi"}],
                "harness": "openai-agents",
                "has_mcp_servers": True,
            },
        )
        relayed = []
        async for chunk in resp.aiter_text():
            relayed.append(chunk)
    stream_text = "".join(relayed)

    # The relayed action_required frame must carry the marker.
    assert f'"{_RUNNER_DISPATCHED_FIELD}": true' in stream_text, (
        f"action_required event must be stamped with the dispatch marker; "
        f"stream text was {stream_text!r}"
    )
    # Runner dispatched the MCP tool through the Omnigent server proxy (AP mode).
    assert server_client.call_tool_invocations == [("jira_search_issues", {})], (
        f"runner must dispatch the MCP tool via ProxyMcpManager (AP server); "
        f"got {server_client.call_tool_invocations}"
    )


# ── Session lifecycle endpoint tests (Step 3) ────────────────────────


def _build_lifecycle_app() -> tuple[FastAPI, _FakeProcessManager, _ScriptedHarnessClient]:
    """Wire a runner app for session lifecycle testing.

    :returns: ``(app, process_manager, harness_client)`` tuple.
    """
    spec = AgentSpec(spec_version=1, name="t")
    sse_frames = [
        _sse({"type": "response.created", "response": {"id": "resp_1"}}),
        _sse({"type": "response.output_text.delta", "delta": "hi"}),
        _sse({"type": "response.completed", "response": {"id": "resp_1"}}),
    ]
    harness_client = _ScriptedHarnessClient(sse_frames)
    pm = _FakeProcessManager(harness_client)

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    return app, pm, harness_client


@pytest.mark.asyncio
async def test_create_session_threads_resolved_bundle_dir_to_codex_spawn_env(
    tmp_path: Path,
) -> None:
    """Session pre-spawn must include bundle-dir env for Codex skills.

    The real e2e flow creates the session before the first turn.
    ``HarnessProcessManager`` fixes env on first spawn and ignores env
    on later cache hits, so dropping the resolved bundle workdir here
    means the later turn cannot recover ``HARNESS_CODEX_BUNDLE_DIR``.
    Codex then only sees host/default skills, not bundled fixture
    skills.
    """
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    spec = AgentSpec(
        spec_version=1,
        name="codex-bundle-agent",
        skills_filter=["codex_e2e_xyz_greet_a3f9c2"],
        executor=ExecutorSpec(
            config={"harness": "codex", "profile": "test-profile"},
            model="databricks-gpt-5-4-mini",
        ),
    )
    harness_client = _ScriptedHarnessClient([])
    pm = _FakeProcessManager(harness_client)

    async def _resolver(agent_id: str, session_id: str | None = None) -> ResolvedSpec:
        del agent_id, session_id
        return ResolvedSpec(spec=spec, workdir=bundle_dir)

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions",
            json={"session_id": "conv_codex", "agent_id": "ag_codex"},
        )

    assert resp.status_code == 201
    assert pm.get_client_calls
    conversation_id, harness, env = pm.get_client_calls[-1]
    assert conversation_id == "conv_codex"
    assert harness == "codex"
    assert env is not None
    assert env["HARNESS_CODEX_BUNDLE_DIR"] == str(bundle_dir)
    assert env["HARNESS_CODEX_SKILLS_FILTER"] == '["codex_e2e_xyz_greet_a3f9c2"]'


@pytest.mark.asyncio
async def test_create_session_threads_cursor_bridge_dir_without_dead_guard_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cursor-native session pre-spawn emits only the bridge dir env.

    This locks the runner boundary, not just the bridge helper: the
    session-creation route must pass a cursor-native bridge dir into the
    harness process manager, while omitting the unread request-session-id
    guard env. Inject/stop/interrupt paths use the bridge dir and tmux target;
    none consume an active-session guard.
    """
    monkeypatch.setattr(cursor_native_bridge, "_BRIDGE_ROOT", tmp_path / "cursor-bridge")
    spec = AgentSpec(
        spec_version=1,
        name="cursor-native-agent",
        executor=ExecutorSpec(
            config={"harness": "cursor-native", "model": "cursor-default"},
        ),
    )
    harness_client = _ScriptedHarnessClient([])
    pm = _FakeProcessManager(harness_client)

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions",
            json={"session_id": "conv_cursor", "agent_id": "ag_cursor"},
        )

    assert resp.status_code == 201
    assert pm.get_client_calls
    conversation_id, harness, env = pm.get_client_calls[-1]
    assert conversation_id == "conv_cursor"
    assert harness == "cursor-native"
    assert env == {
        cursor_native_bridge.BRIDGE_DIR_ENV_VAR: str(
            cursor_native_bridge.bridge_dir_for_session_id("conv_cursor")
        )
    }
    assert "HARNESS_CURSOR_NATIVE_REQUEST_SESSION_ID" not in env


@pytest.mark.asyncio
async def test_create_session_threads_kiro_bridge_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kiro-native session pre-spawn emits the Kiro bridge dir env."""
    monkeypatch.setattr(kiro_native_bridge, "_BRIDGE_ROOT", tmp_path / "kiro-bridge")
    spec = AgentSpec(
        spec_version=1,
        name="kiro-native-agent",
        executor=ExecutorSpec(
            config={"harness": "kiro-native", "model": "auto"},
        ),
    )
    harness_client = _ScriptedHarnessClient([])
    pm = _FakeProcessManager(harness_client)

    async def _resolver(agent_id: str, session_id: str | None = None) -> ResolvedSpec:
        del agent_id, session_id
        return ResolvedSpec(spec=spec, workdir=tmp_path)

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions",
            json={"session_id": "conv_kiro", "agent_id": "ag_kiro"},
        )

    assert resp.status_code == 201
    assert pm.get_client_calls
    conversation_id, harness, env = pm.get_client_calls[-1]
    assert conversation_id == "conv_kiro"
    assert harness == "kiro-native"
    assert env == {
        kiro_native_bridge.KIRO_NATIVE_BRIDGE_DIR_ENV_VAR: str(
            kiro_native_bridge.bridge_dir_for_session_id("conv_kiro")
        )
    }


@pytest.mark.asyncio
async def test_create_session_threads_workspace_to_pi_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pi pre-spawn receives the session workspace, not the bundle dir."""
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path / "config-home"))
    session_id = "conv_pi_worktree"
    runner_workspace = tmp_path / "runner-workspace"
    runner_workspace.mkdir()
    bundle_dir = tmp_path / "runner-specs" / "ag_pi-v1"
    bundle_dir.mkdir(parents=True)
    worktree = tmp_path / "repo-worktrees" / "feature-x"
    worktree.mkdir(parents=True)
    spec = AgentSpec(
        spec_version=1,
        name="pi-worktree-agent",
        skills_filter="none",
        executor=ExecutorSpec(config={"harness": "pi"}),
    )
    harness_client = _ScriptedHarnessClient([])
    pm = _FakeProcessManager(harness_client)

    async def _resolver(agent_id: str, session_id: str | None = None) -> ResolvedSpec:
        del agent_id, session_id
        return ResolvedSpec(spec=spec, workdir=bundle_dir)

    class _SessionWorkspaceClient(NullServerClient):
        async def get(self, url: str, **kwargs: Any) -> httpx.Response:
            del kwargs
            if url == f"/v1/sessions/{session_id}":
                return httpx.Response(
                    200,
                    json={
                        "id": session_id,
                        "agent_id": "ag_pi",
                        "created_at": 1.0,
                        "workspace": str(worktree),
                    },
                    request=httpx.Request("GET", url),
                )
            return await super().get(url)

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=_SessionWorkspaceClient(),  # type: ignore[arg-type]
        runner_workspace=runner_workspace,
    )

    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions",
            json={"session_id": session_id, "agent_id": "ag_pi"},
        )

    assert resp.status_code == 201
    assert pm.get_client_calls
    conversation_id, harness, env = pm.get_client_calls[-1]
    assert conversation_id == session_id
    assert harness == "pi"
    assert env is not None
    assert env["HARNESS_PI_CWD"] == str(worktree.resolve())
    assert env["HARNESS_PI_BUNDLE_DIR"] == str(bundle_dir)


@pytest.mark.parametrize("workspace_value", [None, "   "])
@pytest.mark.asyncio
async def test_create_session_threads_runner_workspace_to_pi_cwd_when_session_workspace_missing(
    workspace_value: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pi pre-spawn falls back to runner workspace when session workspace is empty."""
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path / "config-home"))
    session_id = "conv_pi_runner_workspace"
    runner_workspace = tmp_path / "runner-workspace"
    runner_workspace.mkdir()
    bundle_dir = tmp_path / "runner-specs" / "ag_pi-v1"
    bundle_dir.mkdir(parents=True)
    spec = AgentSpec(
        spec_version=1,
        name="pi-runner-workspace-agent",
        skills_filter="none",
        executor=ExecutorSpec(config={"harness": "pi"}),
    )
    harness_client = _ScriptedHarnessClient([])
    pm = _FakeProcessManager(harness_client)

    async def _resolver(agent_id: str, session_id: str | None = None) -> ResolvedSpec:
        del agent_id, session_id
        return ResolvedSpec(spec=spec, workdir=bundle_dir)

    class _SessionWorkspaceClient(NullServerClient):
        async def get(self, url: str, **kwargs: Any) -> httpx.Response:
            del kwargs
            if url == f"/v1/sessions/{session_id}":
                return httpx.Response(
                    200,
                    json={
                        "id": session_id,
                        "agent_id": "ag_pi",
                        "created_at": 1.0,
                        "workspace": workspace_value,
                    },
                    request=httpx.Request("GET", url),
                )
            return await super().get(url)

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=_SessionWorkspaceClient(),  # type: ignore[arg-type]
        runner_workspace=runner_workspace,
    )

    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions",
            json={"session_id": session_id, "agent_id": "ag_pi"},
        )

    assert resp.status_code == 201
    assert pm.get_client_calls
    conversation_id, harness, env = pm.get_client_calls[-1]
    assert conversation_id == session_id
    assert harness == "pi"
    assert env is not None
    assert env["HARNESS_PI_CWD"] == str(runner_workspace.resolve())
    assert env["HARNESS_PI_BUNDLE_DIR"] == str(bundle_dir)


@pytest.mark.parametrize(
    ("session_json", "expected"),
    [
        # Host-spawned (web UI): bound to a host -> auto-create.
        ({"id": "conv_x", "host_id": "host_abc"}, True),
        # Top-level CLI session: no host_id and no parent, but the runner
        # still owns the Codex app-server and terminal.
        ({"id": "conv_x", "host_id": None}, True),
        ({"id": "conv_x"}, True),
    ],
)
@pytest.mark.asyncio
async def test_codex_top_level_session_needs_runner_terminal_for_all_session_shapes(
    session_json: dict[str, object], expected: bool
) -> None:
    """
    Codex-native terminal auto-create is runner-owned for every session.

    A top-level CLI session has no ``host_id`` and no parent, but the
    runner must still create the app-server and TUI terminal. If the old
    host-id gate returns ``False`` here, ``omnigent codex`` falls back to
    a CLI-owned app-server.
    """
    from omnigent.runner.app import _codex_session_needs_runner_terminal

    class _Client:
        async def get(self, url: str, *, timeout: float) -> httpx.Response:
            return httpx.Response(200, json=session_json, request=httpx.Request("GET", url))

    assert await _codex_session_needs_runner_terminal(_Client(), "conv_x") is expected


@pytest.mark.asyncio
async def test_auto_create_codex_terminal_uses_persisted_resume_launch_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Runner-owned Codex launch consumes persisted args and thread id.

    The CLI now persists launch intent and asks the runner to ensure the
    terminal. This test exercises the runner helper directly: it must read
    ``terminal_launch_args`` and ``external_session_id`` from the Omnigent snapshot,
    start the app-server itself, launch the TUI as ``codex ... resume
    --remote <runner-ws> <thread>``, and run the known-thread forwarder. If
    this regresses, the CLI falls back into split ownership or loses user
    pass-through flags on resume.

    :param tmp_path: Temporary directory for isolated bridge state.
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    import omnigent.codex_native_app_server as codex_app_mod
    import omnigent.runner.app as runner_app_mod

    session_id = "conv_codex_resume"
    thread_id = "019e96aa-0be2-7343-8d3b-6f914d60936b"
    monkeypatch.setattr(codex_native_bridge, "_BRIDGE_ROOT", tmp_path / "codex-bridge")
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://ap.example")
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
    monkeypatch.setattr("omnigent.runner._entry._make_auth_token_factory", lambda: None)
    bridge_dir = codex_native_bridge.bridge_dir_for_bridge_id(session_id)
    codex_native_bridge.write_bridge_state(
        bridge_dir,
        codex_native_bridge.CodexNativeBridgeState(
            session_id=session_id,
            socket_path="ws://127.0.0.1:1",
            thread_id="019e96aa-1111-7222-8333-444455556666",
            codex_home=str(tmp_path / "stale-codex-home"),
        ),
    )

    class _SnapshotServerClient:
        """Server client that returns the persisted Codex launch config."""

        async def get(self, url: str, **kwargs: Any) -> httpx.Response:
            """
            Return the session snapshot consumed by the runner helper.

            :param url: Request path, e.g.
                ``"/v1/sessions/conv_codex_resume"``.
            :param kwargs: Request keyword arguments such as
                ``{"timeout": 10.0}`` or ``{"params": {"limit": 1000}}``.
            :returns: HTTP 200 response carrying launch config.
            """
            if url == f"/v1/sessions/{session_id}/items":
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "id": "msg_user_1",
                                "response_id": "codex_turn_1",
                                "type": "message",
                                "role": "user",
                                "content": [{"type": "input_text", "text": "remember this"}],
                            }
                        ],
                        "has_more": False,
                    },
                    request=httpx.Request("GET", url),
                )
            assert url == f"/v1/sessions/{session_id}", kwargs
            return httpx.Response(
                200,
                json={
                    "terminal_launch_args": [
                        "--config",
                        "approval_policy=on-request",
                    ],
                    "model_override": "gpt-5.4-mini",
                    "external_session_id": thread_id,
                },
                request=httpx.Request("GET", url),
            )

    class _FakeCodexAppServer:
        """Minimal app-server object used by ``codex_terminal_env``."""

        codex_path = "/opt/codex/bin/codex"

        def __init__(self) -> None:
            """:returns: None."""
            self.env = {"OPENAI_API_KEY": "sk-test", "IGNORED": "nope"}
            self.codex_home = tmp_path / "unconfigured-codex-home"
            self.listen_url: str | None = None
            self.started = False
            # Provider/model -c overrides the runner forwards to the
            # --remote TUI; empty here (no profile in this test).
            self.config_overrides: list[str] = []

        async def start(self) -> None:
            """:returns: None."""
            assert list(self.codex_home.glob(f"sessions/**/rollout-*-{thread_id}.jsonl")), (
                "Codex resume rollout must be synthesized in app-server CODEX_HOME "
                "before app-server start"
            )
            self.started = True

        async def close(self) -> None:
            """:returns: None."""

    app_server = _FakeCodexAppServer()
    build_calls: list[dict[str, Any]] = []

    def _fake_build_codex_native_server(**kwargs: Any) -> _FakeCodexAppServer:
        """
        Capture app-server construction.

        :param kwargs: Keyword arguments passed by the runner helper.
        :returns: Fake app-server.
        """
        build_calls.append(kwargs)
        app_server.codex_home = kwargs["codex_home"]
        return app_server

    class _UnexpectedDiscoveryClient:
        """
        App-server client that must not connect on a known-thread resume.

        Fresh sessions connect this listener to discover ``thread/started``.
        Resume sessions already have ``external_session_id`` and should go
        straight to the known-thread forwarder.
        """

        def __init__(self, *, ws_url: str, client_name: str) -> None:
            """
            :param ws_url: App-server WebSocket URL.
            :param client_name: JSON-RPC client name.
            """
            self.ws_url = ws_url
            self.client_name = client_name

        async def connect(self) -> None:
            """
            Fail if the resume path tries to discover a fresh thread.

            :returns: None.
            """
            raise AssertionError("resume path must not connect discovery client")

        async def close(self) -> None:
            """:returns: None."""

    launched_specs: list[Any] = []

    class _FakeResourceRegistry:
        """Resource registry that records the launched terminal spec."""

        async def launch_auxiliary_terminal(
            self,
            *,
            session_id: str,
            terminal_name: str,
            session_key: str,
            spec: Any,
            resource_role: str | None = None,
            parent_os_env: Any = None,
        ) -> SessionResourceView:
            """
            Record the terminal launch request.

            :param session_id: Session id being launched.
            :param terminal_name: Terminal name, e.g. ``"codex"``.
            :param session_key: Terminal session key, e.g. ``"main"``.
            :param spec: Terminal launch spec.
            :param resource_role: Private runner resource marker.
            :returns: Terminal resource view.
            """
            assert session_id == "conv_codex_resume"
            assert terminal_name == "codex"
            assert session_key == "main"
            assert resource_role == CODEX_NATIVE_TERMINAL_ROLE
            launched_specs.append(spec)
            return SessionResourceView(
                id="terminal_codex_main",
                type="terminal",
                session_id=session_id,
                name="Codex",
            )

    published_events: list[dict[str, Any]] = []
    forward_calls: list[dict[str, Any]] = []
    preload_calls: list[tuple[str, str]] = []

    async def _fake_preload_thread(transport: str, loaded_thread_id: str) -> None:
        """
        Record preloading of the known Codex thread.

        :param transport: App-server transport URL.
        :param loaded_thread_id: Thread id passed to ``thread/resume``.
        :returns: None.
        """
        assert codex_native_bridge.read_bridge_state(bridge_dir) is None, (
            "stale bridge state must be cleared until the new app-server has "
            "loaded the resume thread"
        )
        preload_calls.append((transport, loaded_thread_id))

    async def _fake_forward_known_thread(**kwargs: Any) -> None:
        """
        Record the known-thread forwarder invocation.

        :param kwargs: Forwarder keyword arguments.
        :returns: None.
        """
        forward_calls.append(kwargs)

    monkeypatch.setattr(
        codex_app_mod,
        "build_codex_native_server",
        _fake_build_codex_native_server,
    )
    monkeypatch.setattr(codex_app_mod, "CodexAppServerClient", _UnexpectedDiscoveryClient)
    monkeypatch.setattr(codex_app_mod, "preload_codex_thread_for_resume", _fake_preload_thread)
    monkeypatch.setattr(runner_app_mod, "_codex_forward_known_thread", _fake_forward_known_thread)

    agent_spec = AgentSpec(
        spec_version=1,
        name="codex",
        executor=ExecutorSpec(
            type="omnigent",
            config={"harness": "codex-native", "model": "gpt-5-default"},
        ),
    )

    try:
        terminal_view = await _auto_create_codex_terminal(
            session_id,
            _FakeResourceRegistry(),  # type: ignore[arg-type]
            lambda _sid, event: published_events.append(event),
            agent_spec=agent_spec,
            server_client=_SnapshotServerClient(),  # type: ignore[arg-type]
        )
        await asyncio.sleep(0)
    finally:
        runner_app_mod._AUTO_CODEX_APP_SERVERS.pop(session_id, None)

    assert terminal_view.id == "terminal_codex_main"
    assert app_server.started is True
    expected_codex_home = codex_native_bridge.codex_home_for_bridge_dir(
        codex_native_bridge.bridge_dir_for_bridge_id(session_id)
    )
    assert app_server.codex_home == expected_codex_home
    assert build_calls[0]["model"] == "gpt-5.4-mini"
    assert build_calls[0]["cwd"] == tmp_path / "workspace"
    assert len(launched_specs) == 1
    launched = launched_specs[0]
    assert launched.command == "/opt/codex/bin/codex"
    assert launched.args[:3] == [
        "--config",
        "approval_policy=on-request",
        "resume",
    ]
    assert launched.args[3] == "--remote"
    assert launched.args[4].startswith("ws://127.0.0.1:")
    assert launched.args[5] == thread_id
    assert launched.env["OPENAI_API_KEY"] == "sk-test"
    assert "IGNORED" not in launched.env
    assert launched.env["CODEX_HOME"] == str(app_server.codex_home)
    assert launched.tmux_start_on_attach is False
    assert launched.tmux_allow_passthrough is True
    assert preload_calls == [(app_server.listen_url, thread_id)]
    assert published_events[0]["type"] == "session.resource.created"
    assert forward_calls == [
        {
            "session_id": session_id,
            "bridge_dir": bridge_dir,
            "codex_ws_url": app_server.listen_url,
            "thread_id": thread_id,
        }
    ]
    bridge_state = codex_native_bridge.read_bridge_state(bridge_dir)
    assert bridge_state is not None
    assert bridge_state.thread_id == thread_id
    assert bridge_state.socket_path == app_server.listen_url


@pytest.mark.asyncio
async def test_auto_create_codex_terminal_fork_clones_rollout_and_resumes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A forked codex clone clones the source rollout and resumes its copy.

    When the clone has no ``external_session_id`` but carries the fork
    labels, the runner must clone the SOURCE's rollout into the clone's
    own ``CODEX_HOME`` under a freshly minted thread id, pre-set that id
    on the Omnigent session, and launch ``codex resume <minted_id>`` (not the
    source thread). A regression launches fresh (no ``resume`` subcommand)
    and the clone loses the source's Codex history.

    :param tmp_path: Temporary directory for isolated bridge state.
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    import omnigent.codex_native_app_server as codex_app_mod
    import omnigent.runner.app as runner_app_mod
    from omnigent import codex_native
    from omnigent.codex_native_bridge import bridge_dir_for_bridge_id, codex_home_for_bridge_dir
    from omnigent.stores.conversation_store import (
        FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY,
        FORK_SOURCE_LABEL_KEY,
    )

    session_id = "conv_codex_clone"
    source_id = "conv_codex_source"
    source_thread = "019e96aa-0be2-7343-8d3b-6f914d60936b"
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    monkeypatch.setattr(codex_native_bridge, "_BRIDGE_ROOT", tmp_path / "codex-bridge")
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(workspace))
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://ap.example")
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
    monkeypatch.setattr("omnigent.runner._entry._make_auth_token_factory", lambda: None)
    bridge_dir = bridge_dir_for_bridge_id(session_id)
    codex_native_bridge.write_bridge_state(
        bridge_dir,
        codex_native_bridge.CodexNativeBridgeState(
            session_id=session_id,
            socket_path="ws://127.0.0.1:1",
            thread_id="019e96aa-1111-7222-8333-444455556666",
            codex_home=str(tmp_path / "stale-codex-home"),
        ),
    )

    # Seed the SOURCE rollout in the source session's CODEX_HOME so the
    # fork branch finds something to clone.
    source_home = codex_home_for_bridge_dir(bridge_dir_for_bridge_id(source_id))
    source_rollout_dir = source_home / "sessions" / "2026" / "06" / "05"
    source_rollout_dir.mkdir(parents=True)
    (source_rollout_dir / f"rollout-2026-06-05T15-23-07-{source_thread}.jsonl").write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": source_thread, "cwd": "/old/source/dir"},
            }
        )
        + "\n"
    )

    patched_external_ids: list[str] = []

    class _ForkSnapshotClient:
        """Server client returning a forked clone snapshot (no thread id)."""

        async def get(self, url: str, *, timeout: float) -> httpx.Response:
            """
            Return the clone's snapshot carrying fork labels but no thread id.

            :param url: Request path, e.g. ``"/v1/sessions/conv_codex_clone"``.
            :param timeout: Request timeout in seconds.
            :returns: HTTP 200 response with fork labels.
            """
            del timeout
            assert url == f"/v1/sessions/{session_id}"
            return httpx.Response(
                200,
                json={
                    "external_session_id": None,
                    "labels": {
                        FORK_SOURCE_LABEL_KEY: source_id,
                        FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY: source_thread,
                    },
                },
                request=httpx.Request("GET", url),
            )

        async def patch(self, url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
            """
            Record the pre-set external_session_id PATCH.

            :param url: Request path.
            :param json: PATCH body, e.g. ``{"external_session_id": "..."}``.
            :param timeout: Request timeout in seconds.
            :returns: HTTP 200 response.
            """
            del timeout
            patched_external_ids.append(json["external_session_id"])
            return httpx.Response(200, json={}, request=httpx.Request("PATCH", url))

    class _FakeCodexAppServer:
        """Minimal app-server object used by ``codex_terminal_env``."""

        codex_path = "/opt/codex/bin/codex"

        def __init__(self) -> None:
            """:returns: None."""
            self.env = {"OPENAI_API_KEY": "sk-test"}
            self.codex_home = codex_home_for_bridge_dir(bridge_dir_for_bridge_id(session_id))
            self.listen_url: str | None = None
            self.started = False
            # Provider/model -c overrides forwarded to the --remote TUI.
            self.config_overrides: list[str] = []

        async def start(self) -> None:
            """:returns: None."""
            self.started = True

        async def close(self) -> None:
            """:returns: None."""

    app_server = _FakeCodexAppServer()

    def _fake_build_codex_native_server(**kwargs: Any) -> _FakeCodexAppServer:
        """
        Return the fake app-server.

        :param kwargs: Construction kwargs (ignored).
        :returns: Fake app-server.
        """
        del kwargs
        return app_server

    class _UnexpectedDiscoveryClient:
        """Discovery client that must not connect on the resume path."""

        def __init__(self, *, ws_url: str, client_name: str) -> None:
            """
            :param ws_url: App-server WebSocket URL.
            :param client_name: JSON-RPC client name.
            """
            self.ws_url = ws_url
            self.client_name = client_name

        async def connect(self) -> None:
            """:raises AssertionError: Always — the fork resumes a known thread."""
            raise AssertionError("fork resume path must not connect discovery client")

        async def close(self) -> None:
            """:returns: None."""

    launched_specs: list[Any] = []

    class _FakeResourceRegistry:
        """Resource registry that records the launched terminal spec."""

        async def launch_auxiliary_terminal(
            self,
            *,
            session_id: str,
            terminal_name: str,
            session_key: str,
            spec: Any,
            resource_role: str | None = None,
            parent_os_env: Any = None,
        ) -> SessionResourceView:
            """
            Record the terminal launch request.

            :param session_id: Session id being launched.
            :param terminal_name: Terminal name, e.g. ``"codex"``.
            :param session_key: Terminal session key, e.g. ``"main"``.
            :param spec: Terminal launch spec.
            :param resource_role: Private runner resource marker.
            :returns: Terminal resource view.
            """
            del terminal_name, session_key, resource_role
            launched_specs.append(spec)
            return SessionResourceView(
                id="terminal_codex_main",
                type="terminal",
                session_id=session_id,
                name="Codex",
            )

    forward_calls: list[dict[str, Any]] = []
    preload_calls: list[tuple[str, str]] = []

    async def _fake_preload_thread(transport: str, loaded_thread_id: str) -> None:
        """
        Record preloading of the cloned Codex thread.

        :param transport: App-server transport URL.
        :param loaded_thread_id: Thread id passed to ``thread/resume``.
        :returns: None.
        """
        assert codex_native_bridge.read_bridge_state(bridge_dir) is None, (
            "fork-resume must not expose stale bridge state before preload"
        )
        preload_calls.append((transport, loaded_thread_id))

    async def _fake_forward_known_thread(**kwargs: Any) -> None:
        """
        Record the known-thread forwarder invocation.

        :param kwargs: Forwarder keyword arguments.
        :returns: None.
        """
        forward_calls.append(kwargs)

    monkeypatch.setattr(
        codex_app_mod, "build_codex_native_server", _fake_build_codex_native_server
    )
    monkeypatch.setattr(codex_app_mod, "CodexAppServerClient", _UnexpectedDiscoveryClient)
    monkeypatch.setattr(codex_app_mod, "preload_codex_thread_for_resume", _fake_preload_thread)
    monkeypatch.setattr(runner_app_mod, "_codex_forward_known_thread", _fake_forward_known_thread)

    agent_spec = AgentSpec(
        spec_version=1,
        name="codex",
        executor=ExecutorSpec(
            type="omnigent",
            config={"harness": "codex-native", "model": "gpt-5-default"},
        ),
    )

    try:
        await _auto_create_codex_terminal(
            session_id,
            _FakeResourceRegistry(),  # type: ignore[arg-type]
            lambda _sid, event: None,
            agent_spec=agent_spec,
            server_client=_ForkSnapshotClient(),  # type: ignore[arg-type]
        )
        await asyncio.sleep(0)
    finally:
        runner_app_mod._AUTO_CODEX_APP_SERVERS.pop(session_id, None)

    # A thread id was minted (uuidv7), pre-set on AP, and used for resume —
    # never the source thread id.
    assert len(patched_external_ids) == 1
    minted = patched_external_ids[0]
    assert minted != source_thread
    assert codex_native._CODEX_THREAD_ID_RE.fullmatch(minted)
    assert preload_calls == [(app_server.listen_url, minted)]
    assert forward_calls and forward_calls[0]["thread_id"] == minted

    launched = launched_specs[0]
    assert "resume" in launched.args
    assert launched.args[-1] == minted, (
        f"resume must target the minted thread id, got {launched.args}"
    )

    # The cloned rollout exists in the CLONE's CODEX_HOME under the minted
    # id, with session_meta.id and cwd rewritten.
    clone_home = codex_home_for_bridge_dir(bridge_dir_for_bridge_id(session_id))
    cloned = list(clone_home.glob(f"sessions/**/rollout-*-{minted}.jsonl"))
    assert len(cloned) == 1, f"expected one cloned rollout under {clone_home}, found {cloned}"
    meta = json.loads(cloned[0].read_text().splitlines()[0])["payload"]
    assert meta["id"] == minted
    assert meta["cwd"] == str(workspace.resolve())


@pytest.mark.asyncio
async def test_auto_create_codex_terminal_fork_builds_rollout_from_items_and_resumes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A forked codex clone from an SDK source builds its rollout from items.

    When the clone carries the carry-history directive but has NO source
    Codex thread to clone (an SDK source, so no rollout on disk), the runner
    must build the clone's rollout from its OWN copied Omnigent items under a
    freshly minted thread id, pre-set that id on the Omnigent session, and launch
    ``codex resume <minted_id>``. A regression launches fresh and the clone
    loses the SDK source's conversation history.

    :param tmp_path: Temporary directory for isolated bridge state.
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    import omnigent.codex_native_app_server as codex_app_mod
    import omnigent.runner.app as runner_app_mod
    from omnigent import codex_native
    from omnigent.codex_native_bridge import bridge_dir_for_bridge_id, codex_home_for_bridge_dir
    from omnigent.stores.conversation_store import (
        FORK_CARRY_HISTORY_LABEL_KEY,
        FORK_SOURCE_LABEL_KEY,
    )

    session_id = "conv_codex_sdkfork"
    source_id = "conv_sdk_source"
    codeword = "swordfish-7281"
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    monkeypatch.setattr(codex_native_bridge, "_BRIDGE_ROOT", tmp_path / "codex-bridge")
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(workspace))
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://ap.example")
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
    monkeypatch.setattr("omnigent.runner._entry._make_auth_token_factory", lambda: None)

    patched_external_ids: list[str] = []

    class _ItemsForkSnapshotClient:
        """Server client: clone snapshot (carry-history, no source thread)
        plus the copied Omnigent items the rollout is built from."""

        async def get(
            self,
            url: str,
            *,
            timeout: float | None = None,
            params: dict[str, Any] | None = None,
        ) -> httpx.Response:
            """
            Serve the clone snapshot and its copied items.

            :param url: Request path — the session snapshot or its items.
            :param timeout: Request timeout (snapshot fetch).
            :param params: Query params (items fetch pagination).
            :returns: HTTP 200 response.
            """
            del timeout
            if url == f"/v1/sessions/{session_id}":
                # SDK source: carry-history set, but NO source external
                # session id → the runner must build from items, not clone.
                return httpx.Response(
                    200,
                    json={
                        "external_session_id": None,
                        "labels": {
                            FORK_SOURCE_LABEL_KEY: source_id,
                            FORK_CARRY_HISTORY_LABEL_KEY: "1",
                        },
                    },
                    request=httpx.Request("GET", url),
                )
            assert url == f"/v1/sessions/{session_id}/items"
            assert params is not None and params.get("order") == "asc"
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": f"codeword {codeword}"}],
                        }
                    ],
                    "has_more": False,
                },
                request=httpx.Request("GET", url),
            )

        async def patch(self, url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
            """
            Record the pre-set external_session_id PATCH.

            :param url: Request path.
            :param json: PATCH body, e.g. ``{"external_session_id": "..."}``.
            :param timeout: Request timeout in seconds.
            :returns: HTTP 200 response.
            """
            del timeout
            patched_external_ids.append(json["external_session_id"])
            return httpx.Response(200, json={}, request=httpx.Request("PATCH", url))

    class _FakeCodexAppServer:
        """Minimal app-server object used by ``codex_terminal_env``."""

        codex_path = "/opt/codex/bin/codex"

        def __init__(self) -> None:
            """:returns: None."""
            self.env = {"OPENAI_API_KEY": "sk-test"}
            self.codex_home = codex_home_for_bridge_dir(bridge_dir_for_bridge_id(session_id))
            self.listen_url: str | None = None
            self.started = False
            self.config_overrides: list[str] = []

        async def start(self) -> None:
            """:returns: None."""
            self.started = True

        async def close(self) -> None:
            """:returns: None."""

    app_server = _FakeCodexAppServer()

    def _fake_build_codex_native_server(**kwargs: Any) -> _FakeCodexAppServer:
        """:param kwargs: Construction kwargs (ignored). :returns: Fake server."""
        del kwargs
        return app_server

    class _UnexpectedDiscoveryClient:
        """Discovery client that must not connect on the resume path."""

        def __init__(self, *, ws_url: str, client_name: str) -> None:
            """:param ws_url: App-server URL. :param client_name: RPC client name."""
            self.ws_url = ws_url
            self.client_name = client_name

        async def connect(self) -> None:
            """:raises AssertionError: Always — the fork resumes a known thread."""
            raise AssertionError("fork resume path must not connect discovery client")

        async def close(self) -> None:
            """:returns: None."""

    launched_specs: list[Any] = []

    class _FakeResourceRegistry:
        """Resource registry that records the launched terminal spec."""

        async def launch_auxiliary_terminal(
            self,
            *,
            session_id: str,
            terminal_name: str,
            session_key: str,
            spec: Any,
            resource_role: str | None = None,
            parent_os_env: Any = None,
        ) -> SessionResourceView:
            """
            Record the terminal launch request.

            :param session_id: Session id being launched.
            :param terminal_name: Terminal name, e.g. ``"codex"``.
            :param session_key: Terminal session key.
            :param spec: Terminal launch spec.
            :param resource_role: Private runner resource marker.
            :returns: Terminal resource view.
            """
            del terminal_name, session_key, resource_role
            launched_specs.append(spec)
            return SessionResourceView(
                id="terminal_codex_main",
                type="terminal",
                session_id=session_id,
                name="Codex",
            )

    forward_calls: list[dict[str, Any]] = []
    preload_calls: list[tuple[str, str]] = []

    async def _fake_preload_thread(transport: str, loaded_thread_id: str) -> None:
        """:param transport: App-server URL. :param loaded_thread_id: Resumed thread."""
        preload_calls.append((transport, loaded_thread_id))

    async def _fake_forward_known_thread(**kwargs: Any) -> None:
        """:param kwargs: Forwarder keyword arguments. :returns: None."""
        forward_calls.append(kwargs)

    monkeypatch.setattr(
        codex_app_mod, "build_codex_native_server", _fake_build_codex_native_server
    )
    monkeypatch.setattr(codex_app_mod, "CodexAppServerClient", _UnexpectedDiscoveryClient)
    monkeypatch.setattr(codex_app_mod, "preload_codex_thread_for_resume", _fake_preload_thread)
    monkeypatch.setattr(runner_app_mod, "_codex_forward_known_thread", _fake_forward_known_thread)

    agent_spec = AgentSpec(
        spec_version=1,
        name="codex",
        executor=ExecutorSpec(
            type="omnigent",
            config={"harness": "codex-native", "model": "gpt-5-default"},
        ),
    )

    try:
        await _auto_create_codex_terminal(
            session_id,
            _FakeResourceRegistry(),  # type: ignore[arg-type]
            lambda _sid, event: None,
            agent_spec=agent_spec,
            server_client=_ItemsForkSnapshotClient(),  # type: ignore[arg-type]
        )
        await asyncio.sleep(0)
    finally:
        runner_app_mod._AUTO_CODEX_APP_SERVERS.pop(session_id, None)

    # A thread id was minted, pre-set on AP, and used for resume.
    assert len(patched_external_ids) == 1
    minted = patched_external_ids[0]
    assert codex_native._CODEX_THREAD_ID_RE.fullmatch(minted)
    assert preload_calls == [(app_server.listen_url, minted)]
    assert forward_calls and forward_calls[0]["thread_id"] == minted

    launched = launched_specs[0]
    assert "resume" in launched.args
    assert launched.args[-1] == minted, (
        f"resume must target the minted thread id, got {launched.args}"
    )

    # The rollout was BUILT (not cloned) in the clone's CODEX_HOME under the
    # minted id, carrying the source conversation's codeword — proving the
    # copied Omnigent items, not a source rollout, seeded the history.
    clone_home = codex_home_for_bridge_dir(bridge_dir_for_bridge_id(session_id))
    built = list(clone_home.glob(f"sessions/**/rollout-*-{minted}.jsonl"))
    assert len(built) == 1, f"expected one built rollout under {clone_home}, found {built}"
    body = built[0].read_text()
    meta = json.loads(body.splitlines()[0])["payload"]
    assert meta["id"] == minted
    assert meta["cwd"] == str(workspace.resolve())
    assert codeword in body, (
        "Built rollout must carry the source conversation's text from the "
        "copied Omnigent items; missing it means history was not seeded."
    )


@pytest.mark.asyncio
async def test_auto_create_codex_terminal_uses_worktree_workspace_not_bundle_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Codex-native launches in the session worktree, not the bundle dir.

    Regression for the worktree bug: ``_codex_workspace_from_spec_or_env``
    preferred ``ResolvedSpec.workdir`` (the runner's spec-bundle
    extraction dir) over the session workspace, stranding Codex in a temp
    dir with no ``.git`` and ignoring the worktree entirely. The fix reads
    the workspace from the session snapshot (the worktree path), matching
    claude-native and the per-session filesystem registry.

    This test sets up the adversarial case the bug got wrong: the snapshot
    reports a worktree workspace that differs from BOTH the runner env var
    and the ResolvedSpec bundle dir. The launched Codex app-server's
    ``cwd`` must be the worktree. If reverted, ``cwd`` would be the bundle
    dir and the ``cwd == worktree`` assertion fails.

    :param tmp_path: Temporary directory for isolated bridge/workspace state.
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    import omnigent.codex_native_app_server as codex_app_mod
    import omnigent.runner.app as runner_app_mod
    from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec

    session_id = "conv_codex_worktree"
    # Three distinct dirs so the assertion can only pass for the worktree:
    #   runner_env  — OMNIGENT_RUNNER_WORKSPACE (claude-native's source)
    #   bundle_dir  — ResolvedSpec.workdir (what the bug used)
    #   worktree    — the session's stored workspace (correct answer)
    runner_env = tmp_path / "runner_workspace"
    runner_env.mkdir()
    bundle_dir = tmp_path / "runner-specs" / f"{session_id}-v1"
    bundle_dir.mkdir(parents=True)
    worktree = tmp_path / "repo-worktrees" / "feature-x"
    worktree.mkdir(parents=True)

    monkeypatch.setattr(codex_native_bridge, "_BRIDGE_ROOT", tmp_path / "codex-bridge")
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(runner_env))
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://ap.example")
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
    monkeypatch.setattr("omnigent.runner._entry._make_auth_token_factory", lambda: None)
    bridge_dir = codex_native_bridge.bridge_dir_for_bridge_id(session_id)
    codex_native_bridge.write_bridge_state(
        bridge_dir,
        codex_native_bridge.CodexNativeBridgeState(
            session_id=session_id,
            socket_path="ws://127.0.0.1:1",
            thread_id="019e96aa-1111-7222-8333-444455556666",
            codex_home=str(tmp_path / "stale-codex-home"),
        ),
    )

    class _WorktreeSnapshotClient:
        """Server client whose session snapshot carries a worktree workspace."""

        async def get(self, url: str, *, timeout: float) -> httpx.Response:
            """
            Return the session snapshot with a worktree ``workspace``.

            :param url: Request path, e.g.
                ``"/v1/sessions/conv_codex_worktree"``.
            :param timeout: Request timeout in seconds.
            :returns: HTTP 200 response carrying the worktree workspace.
            """
            del timeout
            assert url == f"/v1/sessions/{session_id}"
            return httpx.Response(
                200,
                json={
                    "workspace": str(worktree),
                    "terminal_launch_args": None,
                    "model_override": None,
                    "external_session_id": None,
                },
                request=httpx.Request("GET", url),
            )

    class _FakeCodexAppServer:
        """Minimal app-server object used by ``codex_terminal_env``."""

        codex_path = "/opt/codex/bin/codex"

        def __init__(self) -> None:
            """:returns: None."""
            self.env = {"OPENAI_API_KEY": "sk-test"}
            self.codex_home = tmp_path / "codex-home"
            self.listen_url: str | None = None
            self.started = False
            # Provider/model -c overrides forwarded to the --remote TUI.
            self.config_overrides: list[str] = []

        async def start(self) -> None:
            """:returns: None."""
            self.started = True

        async def close(self) -> None:
            """:returns: None."""

    app_server = _FakeCodexAppServer()
    build_calls: list[dict[str, Any]] = []

    def _fake_build_codex_native_server(**kwargs: Any) -> _FakeCodexAppServer:
        """
        Capture app-server construction.

        :param kwargs: Keyword arguments passed by the runner helper.
        :returns: Fake app-server.
        """
        build_calls.append(kwargs)
        return app_server

    class _FakeDiscoveryClient:
        """App-server client for the fresh-thread discovery path."""

        def __init__(self, *, ws_url: str, client_name: str) -> None:
            """
            :param ws_url: App-server WebSocket URL.
            :param client_name: JSON-RPC client name.
            """
            self.ws_url = ws_url
            self.client_name = client_name

        async def connect(self) -> None:
            """:returns: None."""

        async def close(self) -> None:
            """:returns: None."""

    async def _fake_discover_thread_and_forward(**kwargs: Any) -> None:
        """
        Stand in for the fresh-session discovery forwarder.

        :param kwargs: Forwarder keyword arguments.
        :returns: None.
        """
        assert kwargs["bridge_dir"] == bridge_dir
        assert codex_native_bridge.read_bridge_state(bridge_dir) is None, (
            "fresh Codex launch must clear stale bridge state before the "
            "discovery forwarder publishes the new thread"
        )

    launch_captured: dict[str, Any] = {}

    class _FakeResourceRegistry:
        """Resource registry that records the launched terminal spec."""

        async def launch_auxiliary_terminal(
            self,
            *,
            session_id: str,
            terminal_name: str,
            session_key: str,
            spec: Any,
            resource_role: str | None = None,
            parent_os_env: Any = None,
        ) -> SessionResourceView:
            """
            Record the terminal launch request.

            :param session_id: Session id being launched.
            :param terminal_name: Terminal name, e.g. ``"codex"``.
            :param session_key: Terminal session key, e.g. ``"main"``.
            :param spec: Terminal launch spec.
            :param resource_role: Private runner resource marker.
            :param parent_os_env: Agent os_env threaded as the inheritance
                parent so the agent's sandbox / egress / passthrough apply.
            :returns: Terminal resource view.
            """
            del session_key, resource_role
            launch_captured["spec"] = spec
            launch_captured["parent_os_env"] = parent_os_env
            return SessionResourceView(
                id="terminal_codex_main",
                type="terminal",
                session_id=session_id,
                name="Codex",
            )

    monkeypatch.setattr(
        codex_app_mod,
        "build_codex_native_server",
        _fake_build_codex_native_server,
    )
    monkeypatch.setattr(codex_app_mod, "CodexAppServerClient", _FakeDiscoveryClient)
    monkeypatch.setattr(
        runner_app_mod,
        "_codex_discover_thread_and_forward",
        _fake_discover_thread_and_forward,
    )

    # agent_spec is a ResolvedSpec whose workdir is the bundle dir — the
    # exact value the old code wrongly used as the cwd. Its os_env declares
    # sandbox: none, so the launched terminal must inherit that (not the
    # platform default) — see the sandbox-override regression note below.
    codex_os_env = OSEnvSpec(
        type="caller_process",
        cwd=".",
        sandbox=OSEnvSandboxSpec(type="none"),
    )
    agent_spec = ResolvedSpec(
        spec=AgentSpec(
            spec_version=1,
            name="codex",
            executor=ExecutorSpec(
                type="omnigent",
                config={"harness": "codex-native", "model": "gpt-5-default"},
            ),
            os_env=codex_os_env,
        ),
        workdir=bundle_dir,
    )

    try:
        await _auto_create_codex_terminal(
            session_id,
            _FakeResourceRegistry(),  # type: ignore[arg-type]
            lambda _sid, event: None,
            agent_spec=agent_spec,
            server_client=_WorktreeSnapshotClient(),  # type: ignore[arg-type]
        )
        await asyncio.sleep(0)
    finally:
        runner_app_mod._AUTO_CODEX_APP_SERVERS.pop(session_id, None)

    # The Codex app-server cwd must be the worktree (resolved — the launch
    # config normalizes with expanduser().resolve()). A failure here means
    # the workspace resolution regressed: the bundle dir means the old
    # ResolvedSpec.workdir bug is back; the runner env dir means the
    # snapshot workspace was ignored.
    assert build_calls[0]["cwd"] == worktree.resolve(), (
        f"Codex launched in {build_calls[0]['cwd']!r}; expected the worktree "
        f"{worktree.resolve()!r}. bundle_dir={bundle_dir!r} would mean the "
        f"ResolvedSpec.workdir bug regressed; runner_env={runner_env!r} would "
        "mean the session snapshot workspace was ignored."
    )
    assert build_calls[0]["cwd"] != bundle_dir.resolve()  # never the spec-bundle dir

    # Sandbox-override regression: the launched Codex terminal must inherit
    # the agent's sandbox: none rather than falling back to the platform
    # default (bwrap). Without it, a codex-native agent declaring
    # ``os_env.sandbox.type: none`` is wrongly forced into bwrap.
    launched_sandbox = launch_captured["spec"].os_env.sandbox
    assert launched_sandbox is not None and launched_sandbox.type == "none"
    assert launch_captured["parent_os_env"] is codex_os_env


@pytest.mark.asyncio
async def test_auto_create_codex_terminal_starts_relay_at_session_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tool relay is started at session creation, non-blocking.

    Root-cause fix for the ~30s first-turn stall: the relay (which writes
    ``tool_relay.json`` codex reads via serve-mcp) must be started when the
    session is created, into the same bridge dir codex uses, and WITHOUT
    awaiting the tools/list_changed notification. Previously the relay was
    only started on the first turn with ``await_notify=True``, which blocked
    on codex's MCP bridge ``server.json`` — a file that only appears once
    codex runs the turn — until ``post_tools_changed``'s 30s timeout.

    Asserts ``_auto_create_codex_terminal`` invokes the injected
    ``ensure_comment_relay`` exactly once, for this session's bridge dir,
    with ``await_notify=False``. If the call regressed to the first-turn
    path the spy would never fire here; if it regressed to
    ``await_notify=True`` the assertion on that kwarg would fail.

    :param tmp_path: Temporary directory for isolated bridge state.
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    import omnigent.codex_native_app_server as codex_app_mod
    import omnigent.runner.app as runner_app_mod

    session_id = "conv_codex_relay_start"
    monkeypatch.setattr(codex_native_bridge, "_BRIDGE_ROOT", tmp_path / "codex-bridge")
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://ap.example")
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
    monkeypatch.setattr("omnigent.runner._entry._make_auth_token_factory", lambda: None)

    class _SnapshotClient:
        """Fresh-session snapshot (no external thread → discovery path)."""

        async def get(self, url: str, *, timeout: float) -> httpx.Response:
            """:returns: HTTP 200 fresh-session snapshot."""
            del timeout, url
            return httpx.Response(
                200,
                json={
                    "workspace": str(tmp_path / "workspace"),
                    "terminal_launch_args": None,
                    "model_override": None,
                    "external_session_id": None,
                },
                request=httpx.Request("GET", f"/v1/sessions/{session_id}"),
            )

    class _FakeCodexAppServer:
        """Minimal app-server object."""

        codex_path = "/opt/codex/bin/codex"

        def __init__(self) -> None:
            """:returns: None."""
            self.env = {"OPENAI_API_KEY": "sk-test"}
            self.codex_home = tmp_path / "codex-home"
            self.listen_url: str | None = None
            # Provider/model -c overrides forwarded to the --remote TUI.
            self.config_overrides: list[str] = []

        async def start(self) -> None:
            """:returns: None."""

        async def close(self) -> None:
            """:returns: None."""

    class _FakeDiscoveryClient:
        """No-op app-server client for the discovery path."""

        def __init__(self, *, ws_url: str, client_name: str) -> None:
            """:param ws_url: ws url. :param client_name: client name."""
            del ws_url, client_name

        async def connect(self) -> None:
            """:returns: None."""

        async def close(self) -> None:
            """:returns: None."""

    class _FakeResourceRegistry:
        """Resource registry returning a fixed terminal view."""

        async def launch_auxiliary_terminal(
            self,
            *,
            session_id: str,
            terminal_name: str,
            session_key: str,
            spec: Any,
            resource_role: str | None = None,
            parent_os_env: Any = None,
        ) -> SessionResourceView:
            """:returns: A fixed terminal resource view."""
            del terminal_name, session_key, spec, resource_role
            return SessionResourceView(
                id="terminal_codex_main",
                type="terminal",
                session_id=session_id,
                name="Codex",
            )

    async def _fake_discover(**kwargs: Any) -> None:
        """:returns: None — stands in for the discovery forwarder."""
        del kwargs

    monkeypatch.setattr(
        codex_app_mod, "build_codex_native_server", lambda **k: _FakeCodexAppServer()
    )
    monkeypatch.setattr(codex_app_mod, "CodexAppServerClient", _FakeDiscoveryClient)
    monkeypatch.setattr(runner_app_mod, "_codex_discover_thread_and_forward", _fake_discover)

    relay_calls: list[dict[str, Any]] = []

    async def _spy_ensure_relay(sid: str, **kwargs: Any) -> None:
        """Record the relay-start invocation."""
        relay_calls.append({"session_id": sid, **kwargs})

    agent_spec = AgentSpec(
        spec_version=1,
        name="codex",
        executor=ExecutorSpec(
            type="omnigent",
            config={"harness": "codex-native", "model": "gpt-5-default"},
        ),
    )

    try:
        await _auto_create_codex_terminal(
            session_id,
            _FakeResourceRegistry(),  # type: ignore[arg-type]
            lambda _sid, event: None,
            agent_spec=agent_spec,
            server_client=_SnapshotClient(),  # type: ignore[arg-type]
            ensure_comment_relay=_spy_ensure_relay,
        )
        await asyncio.sleep(0)
    finally:
        runner_app_mod._AUTO_CODEX_APP_SERVERS.pop(session_id, None)

    # Exactly one relay start, at session creation, for this session's bridge
    # dir, and non-blocking (await_notify=False) — the crux of the fix.
    assert len(relay_calls) == 1, relay_calls
    assert relay_calls[0]["session_id"] == session_id
    assert relay_calls[0]["explicit_bridge_dir"] == codex_native_bridge.bridge_dir_for_bridge_id(
        session_id
    )
    assert relay_calls[0]["await_notify"] is False


@pytest.mark.asyncio
async def test_claude_native_first_turn_not_blocked_by_cold_bridge_notify(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First claude-native turn dispatches without waiting on a cold bridge.

    A UI-launched (never pre-warmed) claude-native session starts the comment
    relay lazily on the first turn. The ``tools/list_changed`` delivery
    (``post_tools_changed``) blocks until the bridge publishes ``server.json``
    — up to ``_TOOLS_CHANGED_READY_TIMEOUT_S`` (30s) on a still-cold bridge.
    The turn must NOT be gated on that: the claude-native first-turn caller
    passes ``await_notify=False``, so the relay starts and the notification is
    fired in a background task while the turn dispatches immediately.

    This holds ``post_tools_changed`` open on a never-released event (a cold
    bridge that never publishes ``server.json``) and asserts:

    (a) the notification was actually attempted — the relay genuinely started
        and reached the delivery step. Without this, (b) passes vacuously: a
        relay that bailed early (failed socket bind, unresolved spec) never
        blocks, so the turn was never at risk.
    (b) the harness still received the turn while the notification is blocked.

    A regression to ``await_notify=True`` would await ``post_tools_changed``
    inline, parking ``_run_turn_bg`` at the relay-start step until the event
    is released, so the harness would never see the turn within the poll
    budget and (b) fails.

    :param tmp_path: Temp dir backing the runner workspace (the bridge tree
        itself must live under the real ``/tmp`` trusted parent —
        ``_ensure_secure_dir`` rejects a bridge dir anywhere else, so the
        bridge root is NOT redirected into ``tmp_path``).
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    session_id = f"conv_{uuid.uuid4().hex[:12]}"
    # claude-native pins the bridge tree under /tmp (see _ensure_secure_dir);
    # use the real per-user bridge dir like tests/runner/test_comment_relay.py
    # and rmtree it on teardown rather than redirecting _BRIDGE_ROOT.
    bridge_dir = bridge_dir_for_bridge_id(session_id)
    # start_tool_relay writes tool_relay.json INTO the bridge dir but does not
    # create it — mirror the client's prepare_bridge_dir before launch.
    prepare_bridge_dir(session_id, workspace=tmp_path)

    notify_started = threading.Event()
    notify_release = threading.Event()

    def _blocking_post_tools_changed(*args: Any, **kwargs: Any) -> None:
        """Stand in for a cold bridge: signal entry, then block until released.

        Runs in the default thread-pool executor (``post_tools_changed`` is
        synchronous), so a threading.Event is the right primitive.

        :param args: Positional args from the call site (``bridge_dir``).
        :param kwargs: Keyword args (none expected).
        :returns: None.
        """
        del args, kwargs
        notify_started.set()
        notify_release.wait()

    # The runner imports post_tools_changed from this module at call time, so
    # patching the module attribute is picked up by _ensure_comment_relay_started.
    monkeypatch.setattr(
        "omnigent.claude_native_bridge.post_tools_changed",
        _blocking_post_tools_changed,
    )

    spec = AgentSpec(
        spec_version=1,
        name="claude",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Resolve every request to the claude-native spec under test."""
        del agent_id, session_id
        return spec

    hc = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_1"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_1"}}),
        ]
    )
    pm = _FakeProcessManager(hc)
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    try:
        async with _runner_client(app) as client:
            resp = await client.post(
                f"/v1/sessions/{session_id}/events",
                json={
                    "type": "message",
                    "role": "user",
                    "model": "claude-agent",
                    "agent_id": "ag_claude",
                    "content": [{"type": "input_text", "text": "hi"}],
                },
            )
            assert resp.status_code == 202, f"{resp.status_code} {resp.text}"

            # (a) The relay started and reached the notification: post_tools_changed
            # is now parked on notify_release. If start_tool_relay or the spec
            # resolve had bailed, this never fires — making (b) vacuous.
            for _ in range(300):
                if notify_started.is_set():
                    break
                await asyncio.sleep(0.01)
            assert notify_started.is_set(), (
                "post_tools_changed was never invoked: the relay did not start or "
                "did not reach the notify step, so the no-block assertion below "
                "would be vacuous."
            )

            # (b) The harness received the turn even though post_tools_changed is
            # still blocked (notify_release is NOT set). An unbounded await on the
            # notification would park _run_turn_bg at relay-start, leaving
            # posted_bodies empty until release — that is the ~15-30s first-turn
            # stall this change removes.
            for _ in range(300):
                if hc.posted_bodies:
                    break
                await asyncio.sleep(0.01)
            assert hc.posted_bodies, (
                "claude-native first turn never reached the harness while the "
                "tools/list_changed delivery was blocked — the turn is gated on a "
                "cold-bridge notification."
            )
            # Sanity: we never unblocked delivery, so (b) proves a bounded wait,
            # not that the bridge came up.
            assert not notify_release.is_set()
    finally:
        # Unblock the parked executor thread BEFORE teardown so the loop's
        # shutdown_default_executor(wait=True) does not hang joining it, then
        # close the relay socket/thread by deleting the session.
        notify_release.set()
        with contextlib.suppress(httpx.HTTPError):
            async with _runner_client(app) as cleanup_client:
                await cleanup_client.delete(f"/v1/sessions/{session_id}")
        shutil.rmtree(bridge_dir, ignore_errors=True)


async def _run_antigravity_auto_create(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    session_id: str,
    snapshot: dict[str, Any],
    candidate_ports: list[int],
    pane: tuple[Path, str] | None = None,
    pane_scoped_port: int | None = None,
    pane_agy_found: bool = True,
) -> tuple[Any, list[tuple[int, str]], list[dict[str, Any]], list[tuple[str, dict[str, Any]]]]:
    """
    Drive ``_auto_create_antigravity_terminal`` with every live collaborator faked.

    No real agy is launched: ``build_agy_launch`` is stubbed to a no-op argv, the
    onboarding seed is a no-op, the resource registry records the launched spec,
    the forwarder is a counting stub, and the connect-RPC layer
    (``_candidate_agy_rpc_ports`` / ``resolve_pane_agy_rpc_port_state`` /
    ``start_cascade``) is mocked so the cold-start bootstrap runs without a socket.

    The cold-start exercises the REAL ``resolve_cold_start_agy_rpc_port`` dispatch:
    with no ``pane`` the pane is absent (``_terminal_tmux_pane`` → ``(None, None)``)
    and it falls back to the candidate scan; with a ``pane`` the pane-scoped
    resolver's 3-state result (driven by ``pane_scoped_port`` + ``pane_agy_found``)
    is consulted first.

    :param tmp_path: Temporary directory for isolated bridge state.
    :param monkeypatch: Pytest monkeypatch fixture.
    :param session_id: Session/conversation id under test.
    :param snapshot: The Omnigent session snapshot the helper should read.
    :param candidate_ports: Ports ``_candidate_agy_rpc_ports`` yields (``[]`` →
        the bootstrap never finds a candidate port).
    :param pane: ``(tmux_socket, tmux_target)`` ``_terminal_tmux_pane`` returns,
        or ``None`` (the default) → ``(None, None)`` (no local pane).
    :param pane_scoped_port: The port the pane-scoped resolver reports (``None`` →
        no port; combined with ``pane_agy_found`` to pick state 1/2/3).
    :param pane_agy_found: Whether the pane-scoped resolver found our agy in the
        pane subtree. ``True`` + a port → scoped (state 1); ``True`` + no port →
        candidate fallback (state 2); ``False`` → keep polling (state 3).
    :returns: ``(bridge_state_after, start_cascade_calls, reader_calls,
        external_session_id_patch_calls)``.
    """
    import omnigent.antigravity_native_bridge as bridge_mod
    import omnigent.antigravity_native_launch as launch_mod
    import omnigent.antigravity_native_reader as reader_mod
    import omnigent.antigravity_native_rpc as rpc_mod
    import omnigent.runner.app as runner_app_mod

    monkeypatch.setattr(bridge_mod, "_BRIDGE_ROOT", tmp_path / "antigravity-native")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://ap.example")
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(tmp_path / "workspace"))
    (tmp_path / "workspace").mkdir(parents=True, exist_ok=True)
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
    monkeypatch.setattr("omnigent.runner._entry._make_auth_token_factory", lambda: None)

    # No-op the launch builder + onboarding seed so nothing tries to find agy.
    monkeypatch.setattr(
        launch_mod, "build_agy_launch", lambda **_kwargs: (("agy",), {"AGY_ENV": "1"})
    )
    monkeypatch.setattr(bridge_mod, "ensure_agy_onboarding_complete", lambda: None)
    # Auto-create now spawns the RPC reader (NOT the transcript forwarder); stub
    # ``supervise_reader`` at its definition module (the helper imports it lazily)
    # so the test does not start a real one. The reader is wrapped in
    # ``_run_antigravity_reader``, which still opens (and, on teardown, closes) a
    # real Omnigent client around this stub — fine, since nothing posts here.
    reader_calls: list[dict[str, Any]] = []

    def _counting_reader(*args: Any, **kwargs: Any) -> Any:
        reader_calls.append(kwargs)

        async def _runner() -> None:
            await asyncio.Event().wait()

        return _runner()

    monkeypatch.setattr(reader_mod, "supervise_reader", _counting_reader)
    # The pane the runner resolves for cold-start scoping + the tmux advertise.
    # Default ``None`` → no local pane (``(None, None)``), so the cold-start uses
    # the candidate-scan fallback. A provided pane lets the test assert the
    # pane-scoped port path.
    resolved_pane = (None, None) if pane is None else pane
    monkeypatch.setattr(runner_app_mod, "_terminal_tmux_pane", lambda *_a, **_k: resolved_pane)
    # The pane-scoped resolver the REAL ``resolve_cold_start_agy_rpc_port``
    # consults first when a pane is present — returns the 3-state result.
    pane_resolution = rpc_mod.PaneAgyResolution(agy_found=pane_agy_found, port=pane_scoped_port)
    monkeypatch.setattr(
        rpc_mod, "resolve_pane_agy_rpc_port_state", lambda _sock, _tgt: pane_resolution
    )

    # Mock the connect-RPC cold-start surface. Collapse the port-poll budget +
    # backoff so the no-port case bails immediately instead of waiting the real
    # 20s (and the success case still finds its port on the first probe).
    monkeypatch.setattr(runner_app_mod, "_AGY_COLD_START_PORT_TIMEOUT_S", 0.0)

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(runner_app_mod, "_agy_cold_start_poll_sleep", _no_sleep)
    monkeypatch.setattr(rpc_mod, "_candidate_agy_rpc_ports", lambda: list(candidate_ports))
    start_cascade_calls: list[tuple[int, str]] = []

    def _fake_start_cascade(port: int, cascade_id: str, **_kwargs: Any) -> None:
        start_cascade_calls.append((port, cascade_id))

    monkeypatch.setattr(rpc_mod, "start_cascade", _fake_start_cascade)

    patch_calls: list[tuple[str, dict[str, Any]]] = []

    class _SnapshotServerClient:
        """Server client returning the snapshot + recording external_session_id PATCHes."""

        async def get(self, url: str, **_kwargs: Any) -> httpx.Response:
            assert url == f"/v1/sessions/{session_id}"
            return httpx.Response(200, json=snapshot, request=httpx.Request("GET", url))

        async def patch(self, url: str, *, json: dict[str, Any], **_kwargs: Any) -> httpx.Response:
            patch_calls.append((url, json))
            return httpx.Response(200, json={}, request=httpx.Request("PATCH", url))

    class _FakeResourceRegistry:
        """Resource registry that records the required-terminal launch."""

        terminal_registry = None

        async def launch_required_terminal(
            self,
            session_id: str,
            terminal_name: str,
            session_key: str,
            spec: Any,
            *,
            resource_role: str | None = None,
            **_kwargs: Any,
        ) -> SessionResourceView:
            assert terminal_name == "antigravity"
            assert session_key == "main"
            assert resource_role == ANTIGRAVITY_NATIVE_TERMINAL_ROLE
            return SessionResourceView(
                id="terminal_antigravity_main",
                type="terminal",
                session_id=session_id,
                name="Antigravity",
            )

    try:
        await _auto_create_antigravity_terminal(
            session_id,
            cast(SessionResourceRegistry, _FakeResourceRegistry()),
            lambda _sid, _event: None,
            server_client=cast(httpx.AsyncClient, _SnapshotServerClient()),
        )
        await asyncio.sleep(0)
    finally:
        await runner_app_mod._cancel_auto_forwarder_task(session_id)

    bridge_dir = bridge_mod.bridge_dir_for_bridge_id(session_id)
    return (
        bridge_mod.read_bridge_state(bridge_dir),
        start_cascade_calls,
        reader_calls,
        patch_calls,
    )


@pytest.mark.asyncio
async def test_auto_create_antigravity_cold_starts_real_conversation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A fresh runner launch cold-starts the agy conversation over RPC.

    The runner mints the cascade over ``StartCascade`` (no send-keys / no waiting
    for the TUI to lazily create it) so the executor's turn-1 has a real cascade
    id. This asserts the load-bearing integration: after the agy terminal launches
    and the connect-RPC port answers, the runner calls ``start_cascade`` with a
    runner-generated id, writes THAT real id into bridge state — NOT the
    ``agy_conv_*`` placeholder ``read_bridge_state`` would otherwise return — and
    PATCHes it onto the session as ``external_session_id`` so a later ``--resume``
    continues it.
    """
    session_id = "conv_agy_coldstart"
    state, start_cascade_calls, reader_calls, patch_calls = await _run_antigravity_auto_create(
        tmp_path,
        monkeypatch,
        session_id=session_id,
        snapshot={},  # fresh: no external_session_id
        candidate_ports=[52548],
    )
    # start_cascade was called once, on the discovered port, with a real id.
    assert len(start_cascade_calls) == 1
    called_port, called_id = start_cascade_calls[0]
    assert called_port == 52548
    assert not bridge_mod_is_placeholder(called_id)
    # The real cold-started id is what reaches bridge state (no placeholder).
    assert state is not None
    assert state.conversation_id == called_id
    assert not bridge_mod_is_placeholder(state.conversation_id)
    # The same real id is PATCHed onto the session as external_session_id so a
    # later --resume continues agy's actual conversation (the read-path
    # replacement for the retired forwarder's _patch_external_session_id).
    assert patch_calls == []  # cold-start no longer records the phantom cascade (#2 data-loss)
    # The RPC reader spawns (it replaced the transcript forwarder).
    assert len(reader_calls) == 1


@pytest.mark.asyncio
async def test_auto_create_antigravity_cold_start_scopes_to_pane_agy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    With several agy candidates, cold-start binds THIS session's pane agy.

    The cross-bind fix: on a host running several agy instances (sub-agent
    fan-out / shared runner), ``StartCascade`` must target the agy actually
    running under this session's tmux pane — NOT the lowest Heartbeat-answering
    candidate, which could be a FOREIGN agy and permanently cross-bind the
    session. With a resolvable pane the cold-start uses the pane-scoped port
    (61000) even though a lower foreign candidate (52548) exists.
    """
    session_id = "conv_agy_paneScoped"
    state, start_cascade_calls, _reader_calls, patch_calls = await _run_antigravity_auto_create(
        tmp_path,
        monkeypatch,
        session_id=session_id,
        snapshot={},
        # Several agy ports on the host; 52548 is the lowest (a FOREIGN agy).
        candidate_ports=[52548, 61000],
        # This session's pane resolves to a DIFFERENT (higher) agy's port.
        pane=(tmp_path / "agy.sock", "main"),
        pane_scoped_port=61000,
    )
    # StartCascade fired on the PANE-SCOPED port, NOT candidates[0] (52548).
    assert len(start_cascade_calls) == 1
    called_port, called_id = start_cascade_calls[0]
    assert called_port == 61000
    assert not bridge_mod_is_placeholder(called_id)
    assert state is not None
    assert state.conversation_id == called_id
    assert patch_calls == []  # cold-start no longer records the phantom cascade (#2 data-loss)


@pytest.mark.asyncio
async def test_auto_create_antigravity_cold_start_falls_back_when_no_pane(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    No local pane (remote runner) → cold-start uses the lowest candidate port.

    Preserves the current behavior on single-agy hosts and remote runners: when
    ``_terminal_tmux_pane`` yields no socket/target the pane cannot be scoped, so
    the cold-start falls back to ``_candidate_agy_rpc_ports()[0]``.
    """
    session_id = "conv_agy_noPaneFallback"
    state, start_cascade_calls, _reader_calls, patch_calls = await _run_antigravity_auto_create(
        tmp_path,
        monkeypatch,
        session_id=session_id,
        snapshot={},
        candidate_ports=[52548, 61000],
        pane=None,  # remote runner / no local pane
        pane_scoped_port=99999,  # must be ignored — there is no pane to scope to
    )
    assert len(start_cascade_calls) == 1
    called_port, _called_id = start_cascade_calls[0]
    assert called_port == 52548  # the lowest candidate, NOT the (ignored) pane port
    assert state is not None
    assert patch_calls == []  # cold-start no longer records the phantom cascade (#2 data-loss)


@pytest.mark.asyncio
async def test_auto_create_antigravity_cold_start_waits_when_pane_agy_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Pane present, our agy NOT up yet, FOREIGN candidate present → no cold-start.

    The cross-bind guard: with a local pane whose agy has not appeared yet
    (``agy_found=False``) and a foreign agy as the only candidate, the cold-start
    must NOT bind the foreign candidate — it keeps polling until its (collapsed)
    deadline, leaving the placeholder for the reader to bind later. No
    ``StartCascade``, no ``external_session_id`` PATCH.
    """
    session_id = "conv_agy_paneEarlyPoll"
    state, start_cascade_calls, _reader_calls, patch_calls = await _run_antigravity_auto_create(
        tmp_path,
        monkeypatch,
        session_id=session_id,
        snapshot={},
        candidate_ports=[52548],  # a FOREIGN agy is the only candidate
        pane=(tmp_path / "agy.sock", "main"),
        pane_agy_found=False,  # our agy not exec'd into the pane yet
        pane_scoped_port=None,
    )
    # Never cold-started onto the foreign candidate; placeholder stands.
    assert start_cascade_calls == []
    assert patch_calls == []
    assert state is not None
    assert bridge_mod_is_placeholder(state.conversation_id)


@pytest.mark.asyncio
async def test_auto_create_antigravity_cold_start_falls_back_when_port_unattributable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Pane present, our agy found, port not lsof-attributable → candidate fallback.

    The restricted-/proc one-agy-per-pod case: our agy IS up in the pane
    (``agy_found=True``) but lsof cannot attribute its listener, so the scoped
    port is ``None``. Since agy exists here, the lone candidate is ours and the
    candidate fallback is safe.
    """
    session_id = "conv_agy_paneNoPort"
    state, start_cascade_calls, _reader_calls, patch_calls = await _run_antigravity_auto_create(
        tmp_path,
        monkeypatch,
        session_id=session_id,
        snapshot={},
        candidate_ports=[52548],  # one-agy-per-pod → the lone candidate is ours
        pane=(tmp_path / "agy.sock", "main"),
        pane_agy_found=True,  # our agy IS up...
        pane_scoped_port=None,  # ...but its port is not lsof-attributable
    )
    assert len(start_cascade_calls) == 1
    assert start_cascade_calls[0][0] == 52548  # safe candidate fallback
    assert state is not None
    assert patch_calls == []  # cold-start no longer records the phantom cascade (#2 data-loss)


@pytest.mark.asyncio
async def test_auto_create_antigravity_resume_skips_cold_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A resume launch does NOT cold-start — the conversation already exists.

    On resume the snapshot carries agy's real ``external_session_id`` (persisted
    by a prior run), so the conversation already exists and ``StartCascade`` must
    not be issued (it would create a second, empty one). Bridge state keeps the
    resume id verbatim, and — since no cold-start runs — no ``external_session_id``
    PATCH is issued (it already holds the resume id).
    """
    session_id = "conv_agy_resume"
    resume_id = "68caaeac-2eaf-4e2c-9b95-721b022f4903"
    state, start_cascade_calls, _reader_calls, patch_calls = await _run_antigravity_auto_create(
        tmp_path,
        monkeypatch,
        session_id=session_id,
        snapshot={"external_session_id": resume_id},
        candidate_ports=[52548],
    )
    assert start_cascade_calls == []
    assert state is not None
    assert state.conversation_id == resume_id
    assert patch_calls == []


@pytest.mark.asyncio
async def test_cold_start_agy_conversation_returns_early_on_real_id_in_bridge_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The runner cold-start refuses to run when bridge state already holds a real id.

    Defense-in-depth mirroring the CLI cold-start (``antigravity_native.py``): the
    caller only invokes this on a fresh launch (``if not resume:``), but if bridge
    state already names a NON-placeholder conversation id, cold-starting would
    create a second empty conversation and clobber the real id. The guard must
    early-return BEFORE probing for a port or calling ``StartCascade`` — so even a
    future caller that forgets the resume gate cannot cold-start over a real id.
    """
    import omnigent.antigravity_native_bridge as bridge_mod
    import omnigent.antigravity_native_rpc as rpc_mod
    import omnigent.runner.app as runner_app_mod

    monkeypatch.setattr(bridge_mod, "_BRIDGE_ROOT", tmp_path / "antigravity-native")
    session_id = "conv_agy_guard"
    real_id = "68caaeac-2eaf-4e2c-9b95-721b022f4903"  # NOT an agy_conv_* placeholder
    assert not bridge_mod.is_placeholder_conversation_id(real_id)

    bridge_dir = bridge_mod.prepare_bridge_dir(session_id)
    bridge_mod.write_bridge_state(
        bridge_dir,
        bridge_mod.AntigravityNativeBridgeState(
            session_id=session_id,
            conversation_id=real_id,
        ),
    )

    # The cold-start must touch NEITHER the port-scan NOR StartCascade.
    def _no_ports() -> list[int]:
        raise AssertionError("cold-start must not probe for a port when the id is real")

    start_cascade_calls: list[tuple[int, str]] = []

    def _fake_start_cascade(port: int, cascade_id: str, **_kwargs: Any) -> None:
        start_cascade_calls.append((port, cascade_id))

    monkeypatch.setattr(rpc_mod, "_candidate_agy_rpc_ports", _no_ports)
    monkeypatch.setattr(rpc_mod, "start_cascade", _fake_start_cascade)

    patch_calls: list[tuple[str, dict[str, Any]]] = []

    class _RecordingServerClient:
        async def patch(self, url: str, *, json: dict[str, Any], **_kwargs: Any) -> httpx.Response:
            patch_calls.append((url, json))
            return httpx.Response(200, json={}, request=httpx.Request("PATCH", url))

    result = await runner_app_mod._cold_start_agy_conversation(
        bridge_dir,
        session_id,
        server_client=cast(httpx.AsyncClient, _RecordingServerClient()),
    )

    # Returns the existing real id, and never cold-started or re-PATCHed.
    assert result == real_id
    assert start_cascade_calls == []
    assert patch_calls == []
    # Bridge state is untouched (still the real id, not a fresh cold-start id).
    state = bridge_mod.read_bridge_state(bridge_dir)
    assert state is not None
    assert state.conversation_id == real_id


@pytest.mark.asyncio
async def test_auto_create_antigravity_cold_start_port_timeout_keeps_placeholder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    When no connect-RPC port answers, the cold-start is best-effort: the launch
    still completes and leaves the placeholder id for the reader to bind.

    The cold-start must NOT abort the launch (which would leave a registered
    terminal with no reader, never self-healing). With no port, ``start_cascade``
    is never called, bridge state retains the ``agy_conv_*`` placeholder, and no
    ``external_session_id`` PATCH is issued (there is no real id to record).
    """
    session_id = "conv_agy_noport"
    state, start_cascade_calls, reader_calls, patch_calls = await _run_antigravity_auto_create(
        tmp_path,
        monkeypatch,
        session_id=session_id,
        snapshot={},
        candidate_ports=[],  # port never comes up within the bounded poll
    )
    assert start_cascade_calls == []
    assert state is not None
    assert bridge_mod_is_placeholder(state.conversation_id)
    assert patch_calls == []
    # The RPC reader still spawns regardless of the cold-start outcome.
    assert len(reader_calls) == 1


@pytest.mark.asyncio
async def test_auto_create_antigravity_wires_reader_task_and_interaction_bridge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auto-create spawns the RPC reader task and wires its interaction bridge.

    Asserts the Task 11b integration linchpin end-to-end against fakes:

    * the background task is the ``antigravity-reader-{session_id}`` reader (NOT
      the transcript forwarder), registered in the single-instance task slot;
    * the reader is wired with an ``on_pending_interaction`` that, when a WAITING
      interaction is handed to it, POSTs the Task 9 antigravity-elicitation hook
      with ``{elicitation_id, params}``, then — on the human verdict — delivers
      the answer to agy via ``handle_user_interaction`` (the bridge default).
    """
    import omnigent.antigravity_native_bridge as bridge_mod
    import omnigent.antigravity_native_interactions as interactions_mod
    import omnigent.antigravity_native_launch as launch_mod
    import omnigent.antigravity_native_reader as reader_mod
    import omnigent.antigravity_native_rpc as rpc_mod
    import omnigent.runner.app as runner_app_mod
    from omnigent.antigravity_native_interactions import agy_elicitation_id
    from omnigent.antigravity_native_steps import pending_interaction

    session_id = "conv_agy_wiring"
    monkeypatch.setattr(bridge_mod, "_BRIDGE_ROOT", tmp_path / "antigravity-native")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://ap.example")
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(tmp_path / "workspace"))
    (tmp_path / "workspace").mkdir(parents=True, exist_ok=True)
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
    monkeypatch.setattr("omnigent.runner._entry._make_auth_token_factory", lambda: None)
    monkeypatch.setattr(
        launch_mod, "build_agy_launch", lambda **_kwargs: (("agy",), {"AGY_ENV": "1"})
    )
    monkeypatch.setattr(bridge_mod, "ensure_agy_onboarding_complete", lambda: None)
    monkeypatch.setattr(runner_app_mod, "_terminal_tmux_pane", lambda *_a, **_k: (None, None))
    # Skip the 11a cold-start network work (resume launch → no StartCascade).
    resume_id = "efb134b2-d69f-43de-bb54-c9ece346d8a3"
    monkeypatch.setattr(rpc_mod, "_candidate_agy_rpc_ports", list)

    # Capture the reader's wiring (client + on_pending_interaction) and park so the
    # owning ``_run_antigravity_reader`` keeps its client open while we drive the
    # callback. ``supervise_reader`` is patched at its definition module (the
    # helper imports it lazily).
    captured: dict[str, Any] = {}
    wired = asyncio.Event()

    def _capturing_reader(*_args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        wired.set()

        async def _runner() -> None:
            await asyncio.Event().wait()

        return _runner()

    monkeypatch.setattr(reader_mod, "supervise_reader", _capturing_reader)

    # Control the reader's Omnigent client transport: record the elicitation hook
    # POST and return the human's ACCEPT verdict as an ElicitationResult body.
    hook_posts: list[tuple[str, dict[str, Any]]] = []

    def _handle(request: httpx.Request) -> httpx.Response:
        hook_posts.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"action": "accept", "content": {}})

    real_async_client = httpx.AsyncClient

    def _mock_client(**kwargs: Any) -> httpx.AsyncClient:
        kwargs.pop("transport", None)
        return real_async_client(transport=httpx.MockTransport(_handle), **kwargs)

    # ``_run_antigravity_reader`` builds its client via ``httpx.AsyncClient``; patch
    # the httpx module itself (the reader is the only AsyncClient built on this
    # auto-create path — the snapshot client is a hand-rolled fake) so its POSTs hit
    # the MockTransport above instead of the network.
    monkeypatch.setattr(httpx, "AsyncClient", _mock_client)

    # The WAITING (permission) step the bridge re-reads at delivery time, and the
    # ``handle_user_interaction`` delivery sink (the bridge's default ``deliver``).
    waiting_step = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "fixtures"
            / "antigravity"
            / "steps"
            / "run_command_waiting.json"
        ).read_text()
    )
    # The shared ``run_reader_with_bridge`` helper's ``_get_steps`` closure binds
    # ``get_trajectory_steps`` from the reader module's top-level import, so patch
    # it there (matching how the reader's own poll-loop tests patch it).
    monkeypatch.setattr(reader_mod, "get_trajectory_steps", lambda _port, _cid: [waiting_step])
    delivered: list[dict[str, Any]] = []

    def _fake_deliver(
        port: int, cascade_id: str, *, trajectory_id: str, step_index: int, payload: Any
    ) -> None:
        delivered.append(
            {
                "port": port,
                "cascade_id": cascade_id,
                "trajectory_id": trajectory_id,
                "step_index": step_index,
                "payload": payload,
            }
        )

    monkeypatch.setattr(interactions_mod, "handle_user_interaction", _fake_deliver)

    class _SnapshotServerClient:
        async def get(self, url: str, **_kwargs: Any) -> httpx.Response:
            assert url == f"/v1/sessions/{session_id}"
            return httpx.Response(
                200,
                json={"external_session_id": resume_id},
                request=httpx.Request("GET", url),
            )

    class _FakeResourceRegistry:
        terminal_registry = None

        async def launch_required_terminal(
            self,
            session_id: str,
            terminal_name: str,
            session_key: str,
            spec: Any,
            *,
            resource_role: str | None = None,
            **_kwargs: Any,
        ) -> SessionResourceView:
            return SessionResourceView(
                id="terminal_antigravity_main",
                type="terminal",
                session_id=session_id,
                name="Antigravity",
            )

    try:
        await _auto_create_antigravity_terminal(
            session_id,
            cast(SessionResourceRegistry, _FakeResourceRegistry()),
            lambda _sid, _event: None,
            server_client=cast(httpx.AsyncClient, _SnapshotServerClient()),
        )
        await asyncio.wait_for(wired.wait(), timeout=5.0)

        # The single-instance task slot holds the reader task, named for the reader.
        task = runner_app_mod._AUTO_FORWARDER_TASKS[session_id]
        assert task.get_name() == f"antigravity-reader-{session_id}"

        # Drive the captured wiring with a WAITING (permission) interaction, as the
        # reader would when it observes one. Use the SAME cascade id + port the
        # callback contract threads through.
        port = 52548
        pending = pending_interaction(waiting_step)
        assert pending is not None
        await captured["on_pending_interaction"](resume_id, port, pending)

        # 1) It POSTed the antigravity-elicitation hook with {elicitation_id, params}.
        assert len(hook_posts) == 1
        path, body = hook_posts[0]
        assert path == f"/v1/sessions/{session_id}/hooks/antigravity-elicitation-request"
        assert body["elicitation_id"] == agy_elicitation_id(
            resume_id, pending["trajectory_id"], pending["step_index"]
        )
        assert isinstance(body["params"], dict)

        # 2) On the ACCEPT verdict it delivered the answer to agy via the bridge.
        assert len(delivered) == 1
        assert delivered[0]["cascade_id"] == resume_id
        assert delivered[0]["port"] == port
        assert delivered[0]["trajectory_id"] == pending["trajectory_id"]
        assert delivered[0]["step_index"] == pending["step_index"]
        assert delivered[0]["payload"] == {"permission": {"allow": True}}
    finally:
        await runner_app_mod._cancel_auto_forwarder_task(session_id)


@pytest.mark.asyncio
async def test_auto_create_antigravity_wires_omnigent_mcp_relay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auto-create wires the Omnigent MCP relay so agy gets the sys_* tools (#1194).

    Asserts the three wiring points end-to-end against fakes:

    * the relay starter (``ensure_comment_relay``) is invoked for THIS session's
      bridge dir before launch, so its ``tool_relay.json`` is on disk when agy
      first scans the MCP server;
    * the relay ``mcp_config.json`` is written into the per-session isolated agy
      Gemini dir (``<bridge_dir>/agy-home/.gemini/config``), NOT the user's real
      ``~/.gemini`` — the config-scoping footgun the design avoids;
    * the launch args carry ``--gemini_dir=<isolated .gemini>`` while the launch
      env does not override ``HOME``, so agy keeps platform auth such as macOS
      Keychain but loads the bridge-scoped config.
    """
    import omnigent.antigravity_native_bridge as bridge_mod
    import omnigent.antigravity_native_launch as launch_mod
    import omnigent.antigravity_native_reader as reader_mod
    import omnigent.antigravity_native_rpc as rpc_mod
    import omnigent.runner.app as runner_app_mod

    session_id = "conv_agy_mcp"
    monkeypatch.setattr(bridge_mod, "_BRIDGE_ROOT", tmp_path / "antigravity-native")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://ap.example")
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(tmp_path / "workspace"))
    (tmp_path / "workspace").mkdir(parents=True, exist_ok=True)
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
    monkeypatch.setattr("omnigent.runner._entry._make_auth_token_factory", lambda: None)
    monkeypatch.setattr(bridge_mod, "ensure_agy_onboarding_complete", lambda: None)
    monkeypatch.setattr(runner_app_mod, "_terminal_tmux_pane", lambda *_a, **_k: (None, None))
    monkeypatch.setattr(rpc_mod, "_candidate_agy_rpc_ports", list)

    # Capture the env build_agy_launch starts from so we can assert it is preserved
    # without a HOME override (the launch env is the captured spec's ``env`` below).
    monkeypatch.setattr(
        launch_mod, "build_agy_launch", lambda **_kwargs: (("agy",), {"AGY_ENV": "1"})
    )

    def _noop_reader(*_args: Any, **_kwargs: Any) -> Any:
        async def _runner() -> None:
            await asyncio.Event().wait()

        return _runner()

    monkeypatch.setattr(reader_mod, "supervise_reader", _noop_reader)

    relay_calls: list[dict[str, Any]] = []

    async def _recording_relay(session_id_arg: str, **kwargs: Any) -> None:
        relay_calls.append({"session_id": session_id_arg, **kwargs})

    captured_spec: dict[str, Any] = {}

    snapshot = {"workspace": str(tmp_path / "workspace")}

    class _SnapshotServerClient:
        async def get(self, url: str, **_kwargs: Any) -> httpx.Response:
            assert url == f"/v1/sessions/{session_id}"
            return httpx.Response(200, json=snapshot, request=httpx.Request("GET", url))

        async def patch(self, url: str, *, json: dict[str, Any], **_kwargs: Any) -> httpx.Response:
            del json
            return httpx.Response(200, json={}, request=httpx.Request("PATCH", url))

    class _FakeResourceRegistry:
        terminal_registry = None

        async def launch_required_terminal(
            self,
            session_id: str,
            terminal_name: str,
            session_key: str,
            spec: Any,
            *,
            resource_role: str | None = None,
            **_kwargs: Any,
        ) -> SessionResourceView:
            captured_spec["args"] = list(spec.args)
            captured_spec["env"] = dict(spec.env)
            return SessionResourceView(
                id="terminal_antigravity_main",
                type="terminal",
                session_id=session_id,
                name="Antigravity",
            )

    try:
        await _auto_create_antigravity_terminal(
            session_id,
            cast(SessionResourceRegistry, _FakeResourceRegistry()),
            lambda _sid, _event: None,
            server_client=cast(httpx.AsyncClient, _SnapshotServerClient()),
            ensure_comment_relay=_recording_relay,
        )
        await asyncio.sleep(0)
    finally:
        await runner_app_mod._cancel_auto_forwarder_task(session_id)

    bridge_dir = bridge_mod.bridge_dir_for_bridge_id(session_id)
    iso_home = bridge_mod.agy_home_dir(bridge_dir)
    iso_gemini = bridge_mod.agy_gemini_dir(bridge_dir)

    # 1) The relay starter was invoked for this session's bridge dir.
    assert len(relay_calls) == 1
    assert relay_calls[0]["session_id"] == session_id
    assert relay_calls[0]["explicit_bridge_dir"] == bridge_dir
    assert relay_calls[0]["await_notify"] is False

    # 2) The relay mcp_config.json landed in the ISOLATED agy HOME, not ~/.gemini.
    mcp_config = iso_home / ".gemini" / "config" / "mcp_config.json"
    assert mcp_config.is_file()
    payload = json.loads(mcp_config.read_text(encoding="utf-8"))
    server = payload["mcpServers"]["omnigent"]
    assert server["args"][:4] == ["-I", "-m", "omnigent.claude_native_bridge", "serve-mcp"]
    assert str(bridge_dir) in server["args"]
    assert "sys_session_create" in server["enabledTools"]
    # The bridge token the shared relay needs was written into the bridge dir.
    assert (bridge_dir / "bridge.json").is_file()

    # 3) The launch args point agy at the isolated Gemini dir, while HOME stays
    #    real so platform auth such as macOS Keychain keeps working.
    assert captured_spec["args"] == [f"--gemini_dir={iso_gemini}"]
    assert "HOME" not in captured_spec["env"]
    assert captured_spec["env"]["AGY_ENV"] == "1"


@pytest.mark.parametrize("parent_host_id", ["host_parent", None])
@pytest.mark.asyncio
async def test_codex_subagent_always_needs_runner_terminal(
    parent_host_id: str | None,
) -> None:
    """
    Codex-native sub-agent children always need a runner-created terminal.

    A sub-agent child (created via ``sys_session_send``) carries a
    ``parent_session_id`` but no ``host_id`` of its own, and no CLI ever
    manages its terminal. The gate must therefore return ``True`` regardless
    of whether the PARENT was host-spawned (``host_id`` present) or CLI-driven
    (``host_id`` None).

    The ``parent_host_id=None`` case is the regression: gating the child
    on the parent's ``host_id`` made codex-native sub-agents under a CLI-driven
    parent (e.g. nessie run via ``omnigent run --server``) silently never get
    a terminal, so ``sys_session_send`` dispatch no-op'd. If that case returns
    ``False``, the regression has reappeared.

    :param parent_host_id: The parent session's ``host_id`` value to simulate;
        ``"host_parent"`` (web-UI parent) or ``None`` (CLI-driven parent).
    """
    from omnigent.runner.app import _codex_session_needs_runner_terminal

    class _Client:
        async def get(self, url: str, *, timeout: float) -> httpx.Response:
            """
            Return child then parent session snapshots.

            :param url: Omnigent session snapshot URL.
            :param timeout: HTTP timeout in seconds.
            :returns: Fake Omnigent session response.
            """
            del timeout
            if url.endswith("/conv_child"):
                return httpx.Response(
                    200,
                    json={
                        "id": "conv_child",
                        "parent_session_id": "conv_parent",
                        "host_id": None,
                    },
                    request=httpx.Request("GET", url),
                )
            if url.endswith("/conv_parent"):
                return httpx.Response(
                    200,
                    json={"id": "conv_parent", "host_id": parent_host_id},
                    request=httpx.Request("GET", url),
                )
            return httpx.Response(404, request=httpx.Request("GET", url))

    # True for both parent host_ids; a False for the None parent = regressed.
    assert await _codex_session_needs_runner_terminal(_Client(), "conv_child") is True


@pytest.mark.asyncio
async def test_codex_session_needs_runner_terminal_false_without_client() -> None:
    """
    With no server client (embedded/test runner) the gate cannot confirm a
    host-spawned or sub-agent session, so it returns ``False`` — skipping
    auto-create rather than risking a competing setup.
    """
    from omnigent.runner.app import _codex_session_needs_runner_terminal

    assert await _codex_session_needs_runner_terminal(None, "conv_x") is False


@pytest.mark.asyncio
async def test_codex_discover_thread_and_forward_cleans_up_on_discovery_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    When the fresh TUI never starts a thread, the background task must close
    the listener AND the per-session app-server and drop it from the registry.
    Otherwise each failed host-spawned codex session orphans an app-server
    subprocess (and a dangling listener) for the runner's lifetime.
    """
    from omnigent import codex_native_forwarder
    from omnigent.runner.app import (
        _AUTO_CODEX_APP_SERVERS,
        _codex_discover_thread_and_forward,
    )

    closed = {"client": False, "app_server": False}

    class _Client:
        async def close(self) -> None:
            closed["client"] = True

    class _AppServer:
        async def close(self) -> None:
            closed["app_server"] = True

    async def _raise_no_thread(*_args: object, **_kwargs: object) -> str:
        raise TimeoutError("no thread/started observed")

    # The helper lazily imports wait_for_thread_started from the forwarder
    # module on each call, so patching the module attribute takes effect.
    monkeypatch.setattr(codex_native_forwarder, "wait_for_thread_started", _raise_no_thread)

    session_id = "conv_codex_cleanup_test"
    _AUTO_CODEX_APP_SERVERS[session_id] = _AppServer()
    try:
        await _codex_discover_thread_and_forward(
            session_id=session_id,
            bridge_dir=tmp_path,
            codex_ws_url="ws://127.0.0.1:1",
            codex_home=tmp_path / "codex-home",
            event_client=_Client(),  # type: ignore[arg-type]
        )
    finally:
        _AUTO_CODEX_APP_SERVERS.pop(session_id, None)

    # client closed = no dangling reader task/socket; app_server closed = no
    # orphaned subprocess; dropped from registry = no leaked dict reference.
    assert closed["client"] is True
    assert closed["app_server"] is True
    assert session_id not in _AUTO_CODEX_APP_SERVERS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc", "expected_cause"),
    [
        (TimeoutError("no thread/started observed"), "startup timed out"),
        (
            RuntimeError("event stream ended"),
            "event stream ended before a thread was created",
        ),
    ],
)
async def test_codex_discover_thread_and_forward_records_accurate_startup_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    exc: Exception,
    expected_cause: str,
) -> None:
    """
    The startup breadcrumb must describe the actual failure mode: a timeout
    reads as "startup timed out", while a RuntimeError (TUI exited / event
    stream ended) must NOT be mislabeled as a timeout.
    """
    from omnigent import codex_native_forwarder
    from omnigent.codex_native_bridge import read_bridge_startup_error
    from omnigent.runner.app import (
        _AUTO_CODEX_APP_SERVERS,
        _codex_discover_thread_and_forward,
    )

    class _Client:
        async def close(self) -> None:
            return None

    class _AppServer:
        async def close(self) -> None:
            return None

    async def _raise(*_args: object, **_kwargs: object) -> str:
        raise exc

    monkeypatch.setattr(codex_native_forwarder, "wait_for_thread_started", _raise)

    session_id = "conv_codex_startup_error_test"
    _AUTO_CODEX_APP_SERVERS[session_id] = _AppServer()
    try:
        await _codex_discover_thread_and_forward(
            session_id=session_id,
            bridge_dir=tmp_path,
            codex_ws_url="ws://127.0.0.1:1",
            codex_home=tmp_path / "codex-home",
            event_client=_Client(),  # type: ignore[arg-type]
        )
    finally:
        _AUTO_CODEX_APP_SERVERS.pop(session_id, None)

    recorded = read_bridge_startup_error(tmp_path)
    assert recorded is not None
    assert expected_cause in recorded
    assert type(exc).__name__ in recorded
    # A RuntimeError must never be described as a timeout.
    if not isinstance(exc, TimeoutError):
        assert "timed out" not in recorded


@pytest.mark.asyncio
async def test_create_session() -> None:
    """``POST /v1/sessions`` spawns harness and returns SessionResponse shape."""
    app, pm, _hc = _build_lifecycle_app()
    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions",
            json={"session_id": "conv_1", "agent_id": "ag_1"},
        )
    assert resp.status_code == 201
    body = resp.json()
    assert body["id"] == "conv_1"
    assert body["agent_id"] == "ag_1"
    assert body["status"] == "idle"
    assert "created_at" in body
    assert body["items"] == []
    assert pm.has_session("conv_1")


@pytest.mark.asyncio
async def test_create_session_preserves_existing_event_queue() -> None:
    """Session init must not orphan a stream subscriber's event queue.

    The Omnigent relay's ``GET /stream`` lazily creates the per-session event
    queue when it connects before ``POST /v1/sessions`` runs (the relay
    can race ahead of init). Init used to *unconditionally replace* that
    queue, orphaning the relay on the now-dead object: ``_publish_event``
    then enqueued onto the new queue while the relay's generator blocked
    forever on the old one, so later events never reached the server. For
    claude-native that dropped the PTY-watcher ``idle`` edge (emitted
    asynchronously after the turn), stranding the session's web status at
    "working". Init must PRESERVE an existing queue — assert the
    pre-attached queue object survives init unchanged.
    """
    from omnigent.runner.app import _session_event_queues_ref

    app, _pm, _hc = _build_lifecycle_app()
    # Simulate the relay's GET /stream having already attached (lazily
    # created the queue) before init runs.
    sentinel: asyncio.Queue[Any] = asyncio.Queue()
    _session_event_queues_ref["conv_pre"] = sentinel
    try:
        async with _runner_client(app) as client:
            resp = await client.post(
                "/v1/sessions",
                json={"session_id": "conv_pre", "agent_id": "ag_1"},
            )
        assert resp.status_code == 201
        # Same object → a relay already blocked on it keeps receiving
        # events that ``_publish_event`` enqueues after init.
        assert _session_event_queues_ref.get("conv_pre") is sentinel
    finally:
        _session_event_queues_ref.pop("conv_pre", None)


@pytest.mark.asyncio
async def test_has_active_work_reports_process_manager_turns() -> None:
    """The runner idle watchdog sees active harness turns.

    :returns: None.
    """
    app, pm, _hc = _build_lifecycle_app()
    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions",
            json={"session_id": "conv_1", "agent_id": "ag_1"},
        )

    assert resp.status_code == 201
    assert app.state.has_active_work() is False

    pm.mark_turn_active("conv_1")

    assert app.state.has_active_work() is True


@pytest.mark.asyncio
async def test_create_session_missing_fields() -> None:
    """``POST /v1/sessions`` with missing fields returns 400."""
    app, _pm, _hc = _build_lifecycle_app()
    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions",
            json={"session_id": "conv_1"},
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_create_session_scaffold_mode() -> None:
    """``POST /v1/sessions`` returns 501 when process_manager is None."""
    app = create_runner_app(server_client=NullServerClient())  # type: ignore[arg-type]
    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions",
            json={"session_id": "conv_1", "agent_id": "ag_1"},
        )
    assert resp.status_code == 501


@pytest.mark.asyncio
async def test_get_session_status_idle() -> None:
    """``GET /v1/sessions/{id}`` returns idle after session creation."""
    app, _pm, _hc = _build_lifecycle_app()
    async with _runner_client(app) as client:
        await client.post(
            "/v1/sessions",
            json={"session_id": "conv_1", "agent_id": "ag_1"},
        )
        resp = await client.get("/v1/sessions/conv_1")
    assert resp.status_code == 200
    assert resp.json()["status"] == "idle"


@pytest.mark.asyncio
async def test_get_session_status_running() -> None:
    """``GET /v1/sessions/{id}`` returns running when a turn is active."""
    app, pm, _hc = _build_lifecycle_app()
    async with _runner_client(app) as client:
        await client.post(
            "/v1/sessions",
            json={"session_id": "conv_1", "agent_id": "ag_1"},
        )
        pm.mark_turn_active("conv_1")
        resp = await client.get("/v1/sessions/conv_1")
    assert resp.status_code == 200
    assert resp.json()["status"] == "running"


@pytest.mark.asyncio
async def test_get_session_unknown() -> None:
    """``GET /v1/sessions/{id}`` returns 404 for unknown session."""
    app, _pm, _hc = _build_lifecycle_app()
    async with _runner_client(app) as client:
        resp = await client.get("/v1/sessions/nonexistent")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_session() -> None:
    """``DELETE /v1/sessions/{id}`` releases harness and cleans caches."""
    app, pm, _hc = _build_lifecycle_app()
    async with _runner_client(app) as client:
        await client.post(
            "/v1/sessions",
            json={"session_id": "conv_1", "agent_id": "ag_1"},
        )
        resp = await client.delete("/v1/sessions/conv_1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted"] is True
    assert body["session_id"] == "conv_1"
    assert "conv_1" in pm.released
    assert not pm.has_session("conv_1")


@pytest.mark.asyncio
async def test_delete_session_with_active_turn() -> None:
    """``DELETE /v1/sessions/{id}`` cancels active turn before release."""
    app, pm, _hc = _build_lifecycle_app()
    async with _runner_client(app) as client:
        await client.post(
            "/v1/sessions",
            json={"session_id": "conv_1", "agent_id": "ag_1"},
        )
        pm.mark_turn_active("conv_1")
        resp = await client.delete("/v1/sessions/conv_1")
    assert resp.status_code == 200
    assert "conv_1" in pm.cancelled
    assert "conv_1" in pm.released


@pytest.mark.asyncio
async def test_session_stream_receives_events() -> None:
    """``GET /v1/sessions/{id}/stream`` yields events published by proxy_stream."""
    app, _pm, _hc = _build_lifecycle_app()

    async with _runner_client(app) as client:
        # Create the session first.
        await client.post(
            "/v1/sessions",
            json={"session_id": "conv_s", "agent_id": "ag_1"},
        )

        import asyncio

        collected: list[dict[str, Any]] = []

        async def _subscribe() -> None:
            """Subscribe to SSE and collect events until [DONE]."""
            async with client.stream("GET", "/v1/sessions/conv_s/stream") as stream:
                async for line in stream.aiter_lines():
                    if line.startswith("data: "):
                        payload = line[6:]
                        if payload == "[DONE]":
                            return
                        collected.append(json.loads(payload))

        sub_task = asyncio.create_task(_subscribe())
        await asyncio.sleep(0.05)

        # Trigger a turn — proxy_stream publishes events via
        # session_stream. The stream stays open across turns;
        # deleting the session sends [DONE].
        resp = await client.post(
            "/v1/sessions/conv_s/events",
            json={
                "type": "message",
                "role": "user",
                "model": "test-agent",
                "content": [{"type": "input_text", "text": "hi"}],
                "harness": "openai-agents",
            },
        )
        async for _ in resp.aiter_text():
            pass

        # Allow turn-end bookkeeping to run.
        await asyncio.sleep(0.05)

        # Delete the session to close the stream ([DONE]).
        await client.delete("/v1/sessions/conv_s")

        await asyncio.wait_for(sub_task, timeout=5.0)

    # session.status=running + harness frames + session.status=idle.
    statuses = [e.get("status") for e in collected if e.get("type") == "session.status"]
    assert "running" in statuses, f"session.status=running must appear, got statuses: {statuses}"
    assert statuses[-1] in ("idle", "failed"), (
        f"last session.status must be idle or failed, got statuses: {statuses}"
    )
    harness_events = [e for e in collected if e.get("type") != "session.status"]
    assert len(harness_events) >= 2, (
        f"Expected at least 2 harness events, got {len(harness_events)}: {harness_events}"
    )


@pytest.mark.asyncio
async def test_session_stream_emits_heartbeat_on_idle() -> None:
    """The session stream emits an immediate and idle ``session.heartbeat``."""
    import omnigent.runner.app as runner_app_module

    original = runner_app_module._SESSION_STREAM_HEARTBEAT_S
    runner_app_module._SESSION_STREAM_HEARTBEAT_S = 0.05
    try:
        app, _pm, _hc = _build_lifecycle_app()
        async with _runner_client(app) as client:
            await client.post(
                "/v1/sessions",
                json={"session_id": "conv_hb", "agent_id": "ag_1"},
            )
            collected: list[dict[str, Any]] = []

            async def _subscribe() -> None:
                async with client.stream("GET", "/v1/sessions/conv_hb/stream") as stream:
                    async for line in stream.aiter_lines():
                        if line.startswith("data: "):
                            payload = line[6:]
                            if payload == "[DONE]":
                                return
                            collected.append(json.loads(payload))

            sub_task = asyncio.create_task(_subscribe())
            await asyncio.sleep(0.2)
            await client.delete("/v1/sessions/conv_hb")
            await asyncio.wait_for(sub_task, timeout=5.0)

        heartbeats = [e for e in collected if e.get("type") == "session.heartbeat"]
        assert len(heartbeats) >= 1, f"Expected at least 1 session.heartbeat, got {collected}"
        assert collected[0] == {"type": "session.heartbeat"}, (
            "The first stream frame must be the ready heartbeat. Omnigent waits "
            "for this before forwarding fast no-replay user input."
        )
    finally:
        runner_app_module._SESSION_STREAM_HEARTBEAT_S = original


# ── Turn sequencing tests (Step 5) ──────────────────────────────────


class _BlockingHarnessClient(_ScriptedHarnessClient):
    """Harness that blocks mid-stream until an event is set."""

    def __init__(
        self,
        sse_frames: list[str],
        gate: asyncio.Event,
    ) -> None:
        """
        Wrap scripted frames with a gate that pauses mid-stream.

        :param sse_frames: SSE frames returned by the harness stream.
        :param gate: Event that releases the stream after the first frame.
        """
        super().__init__(sse_frames)
        self._gate = gate
        self.post_seen: asyncio.Event = asyncio.Event()

    def stream(self, method: str, url: str, *, json: dict[str, Any], timeout: Any) -> Any:
        """Stream that blocks after the first frame until gate is set."""
        del method, url, timeout
        self.posted_bodies.append(json)
        self.post_seen.set()
        frames = self._sse_frames
        gate = self._gate

        class _BlockingCtx:
            status_code = 200

            async def __aenter__(self) -> _BlockingHarnessClient._BlockingHandle:
                return _BlockingHarnessClient._BlockingHandle(frames, gate)

            async def __aexit__(self, *_: Any) -> None:
                return None

        return _BlockingCtx()

    class _BlockingHandle:
        """Stream handle that pauses after the first frame."""

        status_code = 200

        def __init__(
            self,
            frames: list[str],
            gate: asyncio.Event,
        ) -> None:
            """Initialize with frames and gate."""
            self._frames = frames
            self._gate = gate

        async def aiter_text(self) -> AsyncIterator[str]:
            """Yield first frame, then wait for gate before rest."""
            for i, frame in enumerate(self._frames):
                if i == 1:
                    await self._gate.wait()
                yield frame


def _build_blocking_app(
    gate: asyncio.Event,
) -> tuple[FastAPI, _FakeProcessManager, _BlockingHarnessClient]:
    """Build a runner app with a blocking harness for concurrency tests.

    :param gate: Event that unblocks the harness mid-stream.
    :returns: ``(app, process_manager, harness_client)`` tuple.
    """
    spec = AgentSpec(spec_version=1, name="t")
    sse_frames = [
        _sse({"type": "response.created", "response": {"id": "resp_1"}}),
        _sse({"type": "response.output_text.delta", "delta": "hi"}),
        _sse({"type": "response.completed", "response": {"id": "resp_1"}}),
    ]
    harness_client = _BlockingHarnessClient(sse_frames, gate)
    pm = _FakeProcessManager(harness_client)

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    return app, pm, harness_client


@pytest.mark.asyncio
async def test_turn_sequencing_buffers_concurrent_message() -> None:
    """Second message during an active turn returns 202 (buffered)."""
    import asyncio as _aio

    gate = _aio.Event()
    app, _pm, _hc = _build_blocking_app(gate)

    async with _runner_client(app) as client:
        await client.post(
            "/v1/sessions",
            json={"session_id": "conv_buf", "agent_id": "ag_1"},
        )

        async def _run_first_turn() -> None:
            """Start the first turn and drain its response."""
            resp = await client.post(
                "/v1/sessions/conv_buf/events",
                json={
                    "type": "message",
                    "role": "user",
                    "model": "test-agent",
                    "content": [
                        {"type": "input_text", "text": "first"},
                    ],
                    "harness": "openai-agents",
                },
            )
            async for _ in resp.aiter_text():
                pass

        # Start the first turn as a background task — it will
        # block inside the harness stream until gate is set.
        turn_task = _aio.create_task(_run_first_turn())
        await _aio.sleep(0.05)

        # Second message while turn active → 202 buffered.
        resp2 = await client.post(
            "/v1/sessions/conv_buf/events",
            json={
                "type": "message",
                "role": "user",
                "model": "test-agent",
                "content": [
                    {"type": "input_text", "text": "second"},
                ],
                "harness": "openai-agents",
            },
        )
        assert resp2.status_code == 202, (
            f"Expected 202 buffered, got {resp2.status_code}: {resp2.text}"
        )
        assert resp2.json()["status"] == "buffered"

        # Unblock the harness and let the first turn complete.
        gate.set()
        await _aio.wait_for(turn_task, timeout=5.0)


@pytest.mark.asyncio
async def test_turn_lifecycle_events() -> None:
    """Turn start/complete lifecycle events appear on the session stream."""
    app, _pm, _hc = _build_lifecycle_app()

    async with _runner_client(app) as client:
        await client.post(
            "/v1/sessions",
            json={"session_id": "conv_lc", "agent_id": "ag_1"},
        )

        import asyncio

        collected: list[dict[str, Any]] = []

        async def _sub() -> None:
            """Collect events until [DONE]."""
            async with client.stream("GET", "/v1/sessions/conv_lc/stream") as stream:
                async for line in stream.aiter_lines():
                    if line.startswith("data: "):
                        payload = line[6:]
                        if payload == "[DONE]":
                            return
                        collected.append(json.loads(payload))

        task = asyncio.create_task(_sub())
        await asyncio.sleep(0.05)

        resp = await client.post(
            "/v1/sessions/conv_lc/events",
            json={
                "type": "message",
                "role": "user",
                "model": "test-agent",
                "content": [{"type": "input_text", "text": "hi"}],
                "harness": "openai-agents",
            },
        )
        async for _ in resp.aiter_text():
            pass
        await asyncio.sleep(0.05)

        await client.delete("/v1/sessions/conv_lc")
        await asyncio.wait_for(task, timeout=5.0)

    lifecycle_events = [e for e in collected if e.get("type") != "session.heartbeat"]
    types = [e.get("type") for e in lifecycle_events]
    assert lifecycle_events, (
        f"Expected turn lifecycle events after ready heartbeat, got {collected}"
    )
    # session.status=running must be the first non-heartbeat event.
    assert types[0] == "session.status", (
        f"First non-heartbeat event must be session.status, got {types[0]}"
    )
    assert lifecycle_events[0].get("status") == "running", (
        f"First session.status must be running, got {lifecycle_events[0].get('status')}"
    )
    # session.status=idle must appear after harness events.
    statuses = [e.get("status") for e in collected if e.get("type") == "session.status"]
    assert statuses[-1] in ("idle", "failed"), (
        f"last session.status must be idle or failed, got statuses: {statuses}"
    )


@pytest.mark.asyncio
async def test_delete_during_active_turn_cleans_state() -> None:
    """DELETE cancels the active turn and clears buffers."""
    app, pm, _hc = _build_lifecycle_app()
    async with _runner_client(app) as client:
        await client.post(
            "/v1/sessions",
            json={"session_id": "conv_del", "agent_id": "ag_1"},
        )
        # Start a turn (don't drain — turn stays active).
        await client.post(
            "/v1/sessions/conv_del/events",
            json={
                "type": "message",
                "role": "user",
                "model": "test-agent",
                "content": [{"type": "input_text", "text": "hi"}],
                "harness": "openai-agents",
            },
        )

        # Buffer a second message.
        await client.post(
            "/v1/sessions/conv_del/events",
            json={
                "type": "message",
                "role": "user",
                "model": "test-agent",
                "content": [{"type": "input_text", "text": "bye"}],
                "harness": "openai-agents",
            },
        )

        # DELETE while turn active.
        del_resp = await client.delete("/v1/sessions/conv_del")
        assert del_resp.status_code == 200
        assert "conv_del" in pm.released


@pytest.mark.asyncio
async def test_post_turn_continuation() -> None:
    """Buffered messages are drained and sent to the harness after the first turn."""
    import asyncio as _aio

    gate = _aio.Event()
    app, _pm, hc = _build_blocking_app(gate)

    async with _runner_client(app) as client:
        await client.post(
            "/v1/sessions",
            json={"session_id": "conv_cont", "agent_id": "ag_1"},
        )

        async def _first() -> None:
            """Run and drain the first turn."""
            resp = await client.post(
                "/v1/sessions/conv_cont/events",
                json={
                    "type": "message",
                    "role": "user",
                    "model": "test-agent",
                    "content": [{"type": "input_text", "text": "first"}],
                    "harness": "openai-agents",
                },
            )
            async for _ in resp.aiter_text():
                pass

        task = _aio.create_task(_first())
        await _aio.sleep(0.05)

        # Buffer a second message while the first turn is active.
        resp2 = await client.post(
            "/v1/sessions/conv_cont/events",
            json={
                "type": "message",
                "role": "user",
                "model": "test-agent",
                "content": [{"type": "input_text", "text": "second"}],
                "harness": "openai-agents",
            },
        )
        assert resp2.status_code == 202

        # Unblock the first turn and wait for it to complete.
        gate.set()
        await _aio.wait_for(task, timeout=5.0)

        # Allow post-turn continuation to run (drains buffer,
        # starts background turn for the second message).
        await _aio.sleep(0.2)

    # The harness should have received both messages: the first
    # from the initial turn, the second from the continuation.
    # Each proxy_stream call posts one body to the harness.
    assert len(hc.posted_bodies) >= 2, (
        f"Expected harness to receive 2 messages (initial + "
        f"continuation), got {len(hc.posted_bodies)}"
    )


def _body_contains_text(body: dict[str, Any], needle: str) -> bool:
    """Return whether *needle* appears in any ``input_text`` block of *body*.

    The runner posts user text to the harness in two different content
    shapes: a flat list of content blocks (mid-turn injection forwards)
    and a nested list of ``message`` history items (turn-start streams).
    This walks both so a message is detected regardless of the channel
    that carried it.

    :param body: A harness request body (from ``posted_bodies`` or
        ``patched_events``).
    :param needle: Substring to search for in ``input_text`` blocks.
    :returns: ``True`` if any ``input_text`` block contains *needle*.
    """

    def _walk(node: Any) -> bool:
        if isinstance(node, dict):
            if node.get("type") == "input_text" and needle in (node.get("text") or ""):
                return True
            return any(_walk(v) for v in node.values())
        if isinstance(node, list):
            return any(_walk(v) for v in node)
        return False

    return _walk(body)


def _ordered_user_texts(body: dict[str, Any]) -> list[str]:
    """Return the ``input_text`` strings of *body*'s user messages, in order.

    Handles both content shapes the runner posts: nested ``message``
    history items (turn-start streams) and flat content blocks. Only
    user-role text is collected, so the result is the sequence of user
    inputs the harness sees for that turn — used to assert submission
    ordering.

    :param body: A harness request body (from ``posted_bodies``).
    :returns: User ``input_text`` values in document order.
    """
    texts: list[str] = []
    for item in body.get("content", []):
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message" and item.get("role") == "user":
            for block in item.get("content", []):
                if isinstance(block, dict) and block.get("type") == "input_text":
                    texts.append(block.get("text", ""))
        elif item.get("type") == "input_text":
            texts.append(item.get("text", ""))
    return texts


class _HandshakeHarnessClient(_ScriptedHarnessClient):
    """Blocking harness fake that emits ``injection.consumed`` for forwards.

    Simulates the real consumed-handshake (RUNNER_MESSAGE_INGEST.md
    Part B): when the runner forwards a mid-turn injection via ``post``,
    this captures the injection_id the runner stamped, and the active
    turn's stream emits a matching ``injection.consumed`` frame after the
    gate releases — exactly what the executor adapter emits on a real
    harness once it drains the injection into the running turn.
    """

    def __init__(self, gate: asyncio.Event) -> None:
        """Initialize with the gate that unblocks the turn-1 stream."""
        super().__init__([])
        self._gate = gate
        self._consumed_ids: list[str] = []

    def stream(self, method: str, url: str, *, json: dict[str, Any], timeout: Any) -> Any:
        """Turn-1 stream: created → (gate) → consumed markers → completed."""
        del method, url, timeout
        self.posted_bodies.append(json)
        gate = self._gate
        consumed_ids = self._consumed_ids

        class _Ctx:
            status_code = 200

            async def __aenter__(self) -> Any:
                return _Handle()

            async def __aexit__(self, *_: Any) -> None:
                return None

        class _Handle:
            status_code = 200

            async def aiter_text(self) -> AsyncIterator[str]:
                yield _sse({"type": "response.created", "response": {"id": "resp_1"}})
                await gate.wait()
                # Mirror the executor adapter: once injections are consumed
                # into the running turn, echo each correlation id back.
                for inj_id in list(consumed_ids):
                    yield _sse({"type": "injection.consumed", "injection_id": inj_id})
                yield _sse({"type": "response.completed", "response": {"id": "resp_1"}})

        return _Ctx()

    async def post(self, url: str, *, json: dict[str, Any], timeout: Any = None) -> Any:
        """Record a forwarded injection + capture its injection_id."""
        del url, timeout
        self.patched_events.append(json)
        inj_id = json.get("injection_id")
        if isinstance(inj_id, str) and inj_id:
            self._consumed_ids.append(inj_id)

        class _Response:
            status_code = 200
            headers: dict[str, str] = {}
            content = b""

            def raise_for_status(self) -> None:
                pass

        return _Response()


def _build_handshake_app(
    gate: asyncio.Event,
) -> tuple[FastAPI, _FakeProcessManager, _HandshakeHarnessClient]:
    """Build a runner app whose harness emits the consumed-handshake.

    :param gate: Event that unblocks the turn-1 stream (after which the
        ``injection.consumed`` markers and ``response.completed`` flow).
    :returns: ``(app, process_manager, harness_client)``.
    """
    spec = AgentSpec(spec_version=1, name="t")
    harness_client = _HandshakeHarnessClient(gate)
    pm = _FakeProcessManager(harness_client)

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    return app, pm, harness_client


@pytest.mark.asyncio
async def test_midturn_message_not_double_delivered_to_harness() -> None:
    """A message sent during an active turn must reach the harness once.

    Covers the web→TUI / claude-native duplication fix. When a user
    message arrives while a turn is in flight, ``post_session_events``
    forwards it as a live mid-turn injection (recorded in
    ``patched_events``) AND buffers it. With the consumed-handshake
    (RUNNER_MESSAGE_INGEST.md Part B), the harness echoes an
    ``injection.consumed`` marker once it consumes the injection, and the
    runner drops the buffered copy — so the message is NOT re-delivered in
    a continuation turn. Exactly-once: forwarded once, no continuation.

    The handshake harness here emits that marker on the turn-1 stream,
    mirroring the real executor adapter. Without the runner's dedup (the
    bug), the buffered "second" would still drain into a continuation
    turn — ``posted_bodies`` would grow to 2 and the assertions fail.
    """
    import asyncio as _aio

    gate = _aio.Event()
    app, _pm, hc = _build_handshake_app(gate)

    async with _runner_client(app) as client:
        await client.post(
            "/v1/sessions",
            json={"session_id": "conv_dup", "agent_id": "ag_1"},
        )

        # Turn 1 starts fire-and-forget (202) and its background task
        # blocks inside the harness stream on `gate`. The 0.05s yield lets
        # the runner mark the turn active before the second message lands.
        resp1 = await client.post(
            "/v1/sessions/conv_dup/events",
            json={
                "type": "message",
                "role": "user",
                "model": "test-agent",
                "content": [{"type": "input_text", "text": "first"}],
                "harness": "openai-agents",
            },
        )
        assert resp1.status_code == 202
        await _aio.sleep(0.05)

        # "second" arrives while turn 1 is provably still active (blocked
        # on the gate) → the runner buffers it AND forwards it as a live
        # mid-turn injection with a correlation id.
        resp2 = await client.post(
            "/v1/sessions/conv_dup/events",
            json={
                "type": "message",
                "role": "user",
                "model": "test-agent",
                "content": [{"type": "input_text", "text": "second"}],
                "harness": "openai-agents",
            },
        )
        # 202 "buffered" confirms turn 1 was still active — the precondition
        # for the handshake path. A 200/stream here would mean the race
        # window never opened and the test below would be vacuous.
        assert resp2.status_code == 202, (
            f"Expected 'second' to be buffered against the active turn, "
            f"got {resp2.status_code}: {resp2.text}"
        )
        assert resp2.json()["status"] == "buffered"

        # Release turn 1; the stream then emits injection.consumed (for
        # "second") and response.completed. The runner drops the buffered
        # copy, so no continuation turn starts. Poll a bounded window: if a
        # continuation were (incorrectly) going to start, posted_bodies
        # would reach 2 within it.
        gate.set()
        for _ in range(100):
            if len(hc.posted_bodies) >= 2:
                break
            await _aio.sleep(0.01)

    # "second" was forwarded as a live injection (channel 1)...
    midturn_injections = [b for b in hc.patched_events if _body_contains_text(b, "second")]
    assert len(midturn_injections) == 1, (
        f"'second' should be forwarded as exactly one mid-turn injection; "
        f"got {len(midturn_injections)} ({hc.patched_events})"
    )
    # ...and must NOT also be re-sent in a continuation turn (channel 2).
    # Exactly one harness turn stream means no continuation was started.
    assert len(hc.posted_bodies) == 1, (
        f"'second' was double-delivered: a continuation turn started after "
        f"the injection was consumed. The runner must drop the buffered "
        f"copy on injection.consumed.\nposted_bodies={hc.posted_bodies}"
    )
    continuation_has_second = any(_body_contains_text(b, "second") for b in hc.posted_bodies[1:])
    assert not continuation_has_second


class _NativeBlockingHarnessClient(_ScriptedHarnessClient):
    """Native-style harness fake: first turn blocks; later turns complete.

    Models a claude-native harness for the runner's native delivery path
    (RUNNER_MESSAGE_INGEST.md Part C): turn 0 blocks on a gate (so the
    test can buffer messages behind it), and every continuation turn
    completes immediately (mirroring claude-native's instant ``run_turn``
    that just types the latest user message and returns).
    """

    def __init__(self, gate: asyncio.Event) -> None:
        """Initialize with the gate that holds the first turn open."""
        super().__init__([])
        self._gate = gate
        self._stream_count = 0
        # Snapshot of each turn's latest user text, captured at stream time.
        # ``posted_bodies[i]["content"]`` aliases the live history list (the
        # runner assigns it by reference), which later drains mutate — so we
        # must extract the latest text NOW, not at assertion time.
        self.turn_latest_texts: list[str] = []

    def stream(self, method: str, url: str, *, json: dict[str, Any], timeout: Any) -> Any:
        """Record the turn body; block only the first turn on the gate."""
        del method, url, timeout
        self.posted_bodies.append(json)
        _texts = _ordered_user_texts(json)
        self.turn_latest_texts.append(_texts[-1] if _texts else "")
        n = self._stream_count
        self._stream_count += 1
        gate = self._gate

        class _Ctx:
            status_code = 200

            async def __aenter__(self) -> Any:
                return _Handle()

            async def __aexit__(self, *_: Any) -> None:
                return None

        class _Handle:
            status_code = 200

            async def aiter_text(self) -> AsyncIterator[str]:
                yield _sse({"type": "response.created", "response": {"id": f"resp_{n}"}})
                if n == 0:
                    await gate.wait()
                yield _sse({"type": "response.completed", "response": {"id": f"resp_{n}"}})

        return _Ctx()


def _build_native_app(
    gate: asyncio.Event,
) -> tuple[FastAPI, _FakeProcessManager, _NativeBlockingHarnessClient]:
    """Build a runner app whose session resolves to a claude-native harness.

    The spec_resolver returns a spec whose executor harness is
    ``codex-native``; the first turn's ``_run_turn_bg`` caches it (before
    streaming), so subsequent buffer decisions take the native path. We use
    ``codex-native`` rather than ``claude-native`` because both share the
    identical runner-side ordering path (``_is_native_harness`` covers
    both), but claude-native's turn additionally awaits a live MCP
    comment-tool relay (``_ensure_comment_relay_started``) that a fake
    harness can't satisfy — orthogonal to message ordering.

    :param gate: Event that unblocks the first turn.
    :returns: ``(app, process_manager, harness_client)``.
    """
    spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "codex-native"}),
    )
    harness_client = _NativeBlockingHarnessClient(gate)
    pm = _FakeProcessManager(harness_client)

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    return app, pm, harness_client


@pytest.mark.asyncio
async def test_native_buffered_messages_each_delivered_once_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """claude-native: every buffered message is delivered once, in order.

    Repro for the observed ``1 2 4 4 5 6 7 8 9 0`` corruption. claude-native
    turns are instant and ``run_turn`` types only the *latest* user message,
    so the runner's LLM-oriented machinery mis-delivers for native sessions:
    the collapse-batch continuation (``next_body = all_bodies[-1]``) types
    only the last buffered message (dropping the rest), and the mid-turn
    forward races the instant turn's teardown (duplicating).

    The native path (RUNNER_MESSAGE_INGEST.md Part C) instead skips the
    forward and drains the buffer ONE message at a time, so each buffered
    message gets its own continuation turn — typed exactly once, in order.

    This test buffers 2, 3, 4 behind a blocked first turn and asserts:
    (a) no mid-turn forward POSTs happen (native skips them), and
    (b) the continuation starts a turn per buffered message carrying 2, 3,
    4 as the latest user text, in order — not a single collapsed "4" turn.
    """
    import asyncio as _aio

    def _skip_tools_changed_notification(*args: object, **kwargs: object) -> None:
        """
        Skip MCP tools/list notification in this fake native harness test.

        :param args: Positional notification arguments.
        :param kwargs: Keyword notification arguments.
        :returns: None.
        """
        del args, kwargs

    monkeypatch.setattr(
        claude_native_bridge,
        "post_tools_changed",
        _skip_tools_changed_notification,
    )

    gate = _aio.Event()
    app, _pm, hc = _build_native_app(gate)

    async def _post(text: str) -> httpx.Response:
        """POST one user message carrying an agent_id (drives spec resolve)."""
        return await client.post(
            "/v1/sessions/conv_nat/events",
            json={
                "type": "message",
                "role": "user",
                "model": "test-agent",
                "agent_id": "ag_native",
                "content": [{"type": "input_text", "text": text}],
            },
        )

    async with _runner_client(app) as client:
        # Message "1" starts turn 0; its _run_turn_bg resolves + caches the
        # claude-native spec (before streaming), then blocks on the gate.
        assert (await _post("1")).status_code == 202
        # Wait until turn 0 has streamed (spec is cached → native detected)
        # and is now blocked on the gate, i.e. provably active.
        for _ in range(200):
            if hc.posted_bodies:
                break
            await _aio.sleep(0.01)

        # 2, 3, 4 arrive while turn 0 is active → buffered (native: no forward).
        for text in ("2", "3", "4"):
            resp = await _post(text)
            assert resp.status_code == 202, f"{text!r}: {resp.status_code} {resp.text}"
            assert resp.json()["status"] == "buffered"

        # Release turn 0; the buffer drains one-at-a-time. Each continuation
        # turn completes immediately, re-entering the drain for the next.
        gate.set()
        for _ in range(300):
            if len(hc.posted_bodies) >= 4:
                break
            await _aio.sleep(0.01)

    # (a) No mid-turn forward for a native session — the forward is the
    # unreliable injection race we removed for native harnesses.
    assert hc.patched_events == [], (
        f"native sessions must not forward mid-turn injections; got {hc.patched_events}"
    )
    # (b) One continuation turn per buffered message, each typing 2, 3, 4
    # as its latest user text, in order (snapshotted at stream time — see
    # turn_latest_texts). Collapse (the bug) would yield a single
    # continuation whose latest text is "4", dropping 2 and 3.
    continuation_latest = hc.turn_latest_texts[1:]
    assert continuation_latest == ["2", "3", "4"], (
        f"expected one continuation turn per buffered message delivering "
        f"2, 3, 4 in order; got {continuation_latest}. A collapsed ['4'] "
        f"means intermediate messages were dropped from the terminal.\n"
        f"turn_latest_texts={hc.turn_latest_texts}"
    )


class _GatedFileServerClient:
    """Server client that parks the gated file fetch until released.

    ``_resolve_forwarded_message_content`` awaits two GETs per
    ``file_id`` block (metadata, then content). Blocking the metadata
    GET parks the message that carries that block *inside* content
    resolution — before it reaches ``post_session_events``' turn-vs-buffer
    gate — so a later, plain-text message can claim the turn first. This
    is the deterministic trigger for the runner's arrival-order vs
    resolution-order defect.
    """

    def __init__(self) -> None:
        """Initialize the gate events and the call log."""
        self.meta_fetch_started = asyncio.Event()
        self.release = asyncio.Event()
        self.get_calls: list[str] = []

    async def get(self, url: str, **kwargs: Any) -> Any:
        """Return a file response; park on the gated file's metadata GET."""
        del kwargs
        self.get_calls.append(url)
        if url.endswith("/content"):
            return _GatedFileServerClient._Resp(body=b"png-bytes")
        # Metadata GET for the gated file: signal that the caller is now
        # parked inside resolution, then block until the test releases it.
        self.meta_fetch_started.set()
        await self.release.wait()
        return _GatedFileServerClient._Resp(
            payload={"id": "file_gated", "filename": "a.png", "content_type": "image/png"}
        )

    class _Resp:
        """Minimal httpx-Response stand-in for file metadata/content."""

        def __init__(self, *, body: bytes = b"", payload: dict[str, Any] | None = None) -> None:
            """Hold either raw bytes (content) or a metadata payload."""
            self.content = body
            self._payload = payload or {}
            self.headers = {"content-type": self._payload.get("content_type", "image/png")}
            self.status_code = 200

        def json(self) -> dict[str, Any]:
            """Return the metadata payload."""
            return self._payload

        def raise_for_status(self) -> None:
            """No-op: the gated client never returns error statuses."""
            return


@pytest.mark.asyncio
async def test_messages_reach_harness_in_submission_order() -> None:
    """Two messages must reach the harness in the order they were sent.

    Repro for the web→TUI / claude-native out-of-order symptom. In
    ``post_session_events`` (omnigent/runner/app.py) the turn-vs-buffer
    decision (the ``if conversation_id in _active_turns`` check at ~4237)
    runs *after* ``await _resolve_forwarded_message_content`` (~4230).
    A message with slow content resolution (e.g. a remote runner inlining
    an uploaded image) is therefore parked before it can claim the turn,
    letting a later plain-text message overtake it and start the first
    turn. The runner orders turns by resolution-completion, not arrival.

    The test makes this deterministic: "alpha-first" (submitted first,
    carries a gated image) is held inside resolution while "bravo-second"
    (submitted second, plain text) races ahead. The invariant under test
    is that the FIRST turn the harness sees carries the FIRST-submitted
    message.
    """
    hc = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_1"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_1"}}),
        ]
    )
    pm = _FakeProcessManager(hc)
    server = _GatedFileServerClient()
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=server,  # type: ignore[arg-type]
    )

    # NB: post events directly (first event auto-creates session state),
    # mirroring test_sessions_native_resolves_file_id_before_harness. An
    # explicit POST /v1/sessions needs a spec_resolver this app omits.
    async with _runner_client(app) as client:

        async def _post_alpha() -> httpx.Response:
            """POST the first message (gated image + text)."""
            return await client.post(
                "/v1/sessions/conv_ord/events",
                json={
                    "type": "message",
                    "role": "user",
                    "model": "test-agent",
                    "content": [
                        {"type": "input_image", "file_id": "file_gated", "filename": "a.png"},
                        {"type": "input_text", "text": "alpha-first"},
                    ],
                    "harness": "openai-agents",
                },
            )

        # Submit alpha first; it takes arrival slot 0, passes the ingest
        # gate, and parks inside content resolution on the gated metadata
        # fetch — holding its slot open.
        alpha_task = asyncio.create_task(_post_alpha())
        await asyncio.wait_for(server.meta_fetch_started.wait(), timeout=5.0)

        # Submit bravo second, as a task: under the ordering fix it cannot
        # return until alpha's decision completes, so awaiting it inline
        # here would deadlock. Bravo takes arrival slot 1 and blocks at the
        # ingest gate behind alpha even though its plain-text content
        # resolves instantly.
        async def _post_bravo() -> httpx.Response:
            """POST the second message (plain text)."""
            return await client.post(
                "/v1/sessions/conv_ord/events",
                json={
                    "type": "message",
                    "role": "user",
                    "model": "test-agent",
                    "content": [{"type": "input_text", "text": "bravo-second"}],
                    "harness": "openai-agents",
                },
            )

        bravo_task = asyncio.create_task(_post_bravo())
        # Let bravo reach its steady state: blocked at the gate (fixed) or
        # already racing into the turn-vs-buffer decision (buggy). This is
        # what makes the assertion below catch a regression — without the
        # gate, bravo's plain-text turn starts here while alpha is parked.
        await asyncio.sleep(0.05)

        # Release alpha; correct ordering requires alpha's decision (start
        # turn) to complete before bravo's (buffer behind the active turn).
        server.release.set()
        alpha_resp = await asyncio.wait_for(alpha_task, timeout=5.0)
        bravo_resp = await asyncio.wait_for(bravo_task, timeout=5.0)
        assert alpha_resp.status_code == 202
        assert bravo_resp.status_code == 202

        # Wait for the first turn to reach the harness.
        for _ in range(200):
            if hc.posted_bodies:
                break
            await asyncio.sleep(0.01)

    assert hc.posted_bodies, "harness never received a turn"
    # The harness builds each turn from session history, so the order of
    # user messages there reflects the order the runner accepted them.
    # Submission order was alpha → bravo, so "alpha-first" must precede
    # "bravo-second". They are reversed today: "bravo-second" reached the
    # runner's turn gate first (alpha was still parked in content
    # resolution) and so was appended to history first. Containment alone
    # is not enough to assert here — both texts are present — only order
    # distinguishes the bug.
    ordered = _ordered_user_texts(hc.posted_bodies[0])
    assert ordered.index("alpha-first") < ordered.index("bravo-second"), (
        "out-of-order delivery: 'alpha-first' was submitted before "
        "'bravo-second', but the harness sees them in the order "
        f"{ordered}. post_session_events gates turn-vs-buffer AFTER "
        "awaiting content resolution, so a message with slow resolution is "
        "overtaken by a later one."
    )


@pytest.mark.asyncio
async def test_buffered_continuation_skips_transient_idle() -> None:
    """End-of-turn `idle` is suppressed when a buffered message will start a new turn."""
    import asyncio as _aio

    gate = _aio.Event()
    app, _pm, _hc = _build_blocking_app(gate)

    async with _runner_client(app) as client:
        await client.post(
            "/v1/sessions",
            json={"session_id": "conv_skip", "agent_id": "ag_1"},
        )

        collected: list[dict[str, Any]] = []

        async def _sub() -> None:
            async with client.stream("GET", "/v1/sessions/conv_skip/stream") as stream:
                async for line in stream.aiter_lines():
                    if line.startswith("data: "):
                        payload = line[6:]
                        if payload == "[DONE]":
                            return
                        collected.append(json.loads(payload))

        sub_task = _aio.create_task(_sub())
        await _aio.sleep(0.05)

        async def _first() -> None:
            resp = await client.post(
                "/v1/sessions/conv_skip/events",
                json={
                    "type": "message",
                    "role": "user",
                    "model": "test-agent",
                    "content": [{"type": "input_text", "text": "first"}],
                    "harness": "openai-agents",
                },
            )
            async for _ in resp.aiter_text():
                pass

        turn_task = _aio.create_task(_first())
        await _aio.sleep(0.05)

        await client.post(
            "/v1/sessions/conv_skip/events",
            json={
                "type": "message",
                "role": "user",
                "model": "test-agent",
                "content": [{"type": "input_text", "text": "second"}],
                "harness": "openai-agents",
            },
        )

        gate.set()
        await _aio.wait_for(turn_task, timeout=5.0)
        await _aio.sleep(0.3)

        await client.delete("/v1/sessions/conv_skip")
        await _aio.wait_for(sub_task, timeout=5.0)

    statuses = [e["status"] for e in collected if e.get("type") == "session.status"]
    # Buffered continuation: the turn-1 idle must be skipped so the client
    # never sees a running → idle → running flicker that would hide the
    # Working indicator. Expected: running (turn 1), running (turn 2),
    # then a terminal idle once the buffer drains.
    assert "idle" not in statuses[:-1], (
        f"Expected no transient idle between turns; got statuses: {statuses}"
    )


@pytest.mark.asyncio
async def test_cancelled_turn_publishes_idle_so_client_unsticks() -> None:
    """CancelledError in `_drain_streaming_response` must publish idle.

    Without this, the client sits on stale ``running`` forever after DELETE.
    """
    import asyncio as _aio

    gate = _aio.Event()  # never set → harness stream blocks forever
    app, _pm, _hc = _build_blocking_app(gate)

    async with _runner_client(app) as client:
        await client.post(
            "/v1/sessions",
            json={"session_id": "conv_cancel", "agent_id": "ag_1"},
        )

        collected: list[dict[str, Any]] = []

        async def _sub() -> None:
            async with client.stream("GET", "/v1/sessions/conv_cancel/stream") as stream:
                async for line in stream.aiter_lines():
                    if line.startswith("data: "):
                        payload = line[6:]
                        if payload == "[DONE]":
                            return
                        collected.append(json.loads(payload))

        sub_task = _aio.create_task(_sub())
        await _aio.sleep(0.05)

        async def _stuck() -> None:
            resp = await client.post(
                "/v1/sessions/conv_cancel/events",
                json={
                    "type": "message",
                    "role": "user",
                    "model": "test-agent",
                    "content": [{"type": "input_text", "text": "blocked"}],
                    "harness": "openai-agents",
                },
            )
            async for _ in resp.aiter_text():
                pass

        turn_task = _aio.create_task(_stuck())
        await _aio.sleep(0.1)

        # DELETE cancels the turn task — exercises `_drain_streaming_response`'s
        # CancelledError path.
        del_resp = await client.delete("/v1/sessions/conv_cancel")
        assert del_resp.status_code == 200

        gate.set()  # unblock the stuck stream so the test can finish
        with contextlib.suppress(Exception):
            await _aio.wait_for(turn_task, timeout=2.0)
        await _aio.wait_for(sub_task, timeout=2.0)

    statuses = [e["status"] for e in collected if e.get("type") == "session.status"]
    # Without the fix, the only emitted status is "running" — client stays stuck.
    assert "idle" in statuses, f"Cancelled turn must publish a terminal status; got: {statuses}"


# ── Crash recovery (Step 8.5 Scenario A) ──────────────────────────


class _FakeServerClient:
    """Fake server_client that returns paginated history items.

    Items must have an ``"id"`` field. Supports ``after`` cursor
    and ``limit`` params, returns ``has_more`` when more pages
    exist. Tracks GET calls for assertion.
    """

    def __init__(
        self, items: list[dict[str, Any]], *, session_snapshot: dict[str, Any] | None = None
    ) -> None:
        self._items = items
        self._session_snapshot: dict[str, Any] = session_snapshot or {}
        self.get_calls: list[dict[str, str]] = []

    async def get(
        self, url: str, *, params: dict[str, str] | None = None, timeout: float = 10.0
    ) -> Any:
        del timeout
        params = params or {}
        self.get_calls.append(dict(params))

        # Session snapshot GET (e.g. /v1/sessions/{id}, no /items suffix).
        if "/items" not in url:
            snapshot = dict(self._session_snapshot)

            class _SnapshotResp:
                status_code = 200

                def json(self_inner) -> dict[str, Any]:
                    return snapshot

            return _SnapshotResp()

        after = params.get("after")
        limit = int(params.get("limit", "100"))

        # Find start index based on after cursor.
        start = 0
        if after:
            for i, item in enumerate(self._items):
                if item.get("id") == after:
                    start = i + 1
                    break
        page = self._items[start : start + limit]
        has_more = (start + limit) < len(self._items)

        class _Resp:
            status_code = 200

            def json(self_inner) -> dict[str, Any]:
                return {"data": page, "has_more": has_more}

        return _Resp()


def _build_recovery_app(
    history_items: list[dict[str, Any]],
    *,
    harness_name: str | None = None,
) -> tuple[FastAPI, _FakeProcessManager, _ScriptedHarnessClient]:
    """Build a runner app with a fake server_client returning history.

    :param history_items: Items returned by GET /v1/sessions/{id}/items.
    :param harness_name: Optional harness override for the resolved spec,
        e.g. ``"codex-native"``.
    :returns: ``(app, process_manager, harness_client)`` tuple.
    """
    spec_kwargs: dict[str, Any] = {
        "spec_version": 1,
        "name": "recovery-test",
    }
    if harness_name is not None:
        spec_kwargs["executor"] = ExecutorSpec(
            type="omnigent",
            config={"harness": harness_name},
        )
    spec = AgentSpec(**spec_kwargs)
    sse_frames = [
        _sse({"type": "response.created", "response": {"id": "resp_r1"}}),
        _sse({"type": "response.output_text.delta", "delta": "recovered"}),
        _sse({"type": "response.completed", "response": {"id": "resp_r1"}}),
    ]
    hc = _ScriptedHarnessClient(sse_frames)
    pm = _FakeProcessManager(hc)
    server_client = _FakeServerClient(history_items)

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server_client,  # type: ignore[arg-type]
    )
    return app, pm, hc


async def _fake_auto_create_codex_terminal(
    session_id: str,
    resource_registry: SessionResourceRegistry,
    publish_event: Any,
    **kwargs: Any,
) -> SessionResourceView:
    """
    Return a fake Codex terminal without launching native processes.

    :param session_id: Session whose Codex terminal would be created.
    :param resource_registry: Runner resource registry passed by the
        production call site.
    :param publish_event: Event publisher passed by the production call
        site.
    :param kwargs: Auto-create keyword-only arguments.
    :returns: Fake Codex terminal resource for the requested session.
    """
    del resource_registry, publish_event, kwargs
    return SessionResourceView(
        id="terminal_codex_main",
        type="terminal",
        session_id=session_id,
        name="Codex",
    )


@pytest.mark.asyncio
async def test_session_creation_auto_starts_turn_for_unanswered_user_message() -> None:
    """POST /v1/sessions with history ending in a user message starts a recovery turn.

    Breakage this catches: if _run_turn_bg's incomplete-turn detection
    is removed, the session stays idle and the harness receives no POST.
    """
    import asyncio as _aio

    history = [
        {
            "id": "item_1",
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "hello"}],
        },
    ]
    app, _pm, hc = _build_recovery_app(history)

    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions",
            json={"session_id": "conv_recover_1", "agent_id": "ag_1"},
        )
        assert resp.status_code == 201
        # "running" proves the recovery turn was started during
        # session creation. "idle" would mean the incomplete-turn
        # detection didn't fire.
        assert resp.json()["status"] == "running"

        # Wait for the background turn to POST to harness.
        # The scripted harness completes instantly so 0.5s is
        # generous; event-driven sync isn't possible because the
        # turn runs in a fire-and-forget background task.
        await _aio.sleep(0.5)

    # 1 POST = the recovery turn sent full history to the harness.
    # 0 would mean _run_turn_bg never ran (detection broken).
    assert len(hc.posted_bodies) == 1, (
        f"Expected exactly 1 harness POST (recovery turn), "
        f"got {len(hc.posted_bodies)}. 0 = detection broken, "
        f">1 = duplicate turn started."
    )
    # The recovery turn's body must include the unanswered user
    # message in its content (loaded from server history).
    body_content = hc.posted_bodies[0].get("content", [])
    assert any(
        item.get("type") == "message" and item.get("role") == "user" for item in body_content
    ), (
        "Recovery turn body must contain the unanswered user message "
        "from history. Empty content means _load_history_as_input "
        "failed or _session_histories wasn't populated."
    )


@pytest.mark.asyncio
async def test_session_creation_does_not_replay_trailing_user_for_codex_native(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Codex-native startup must not replay a trailing user item as recovery.

    Native transcripts are mirrored from Codex. If a Codex turn errors before
    producing an assistant item, Omnigent history can end with the user prompt even
    though Codex already consumed it. Generic crash recovery would treat that
    as an unanswered Omnigent turn and resend the same prompt when ``omnigent
    codex`` reattaches.

    :param monkeypatch: Pytest monkeypatch fixture used to bypass real
        terminal auto-create.
    """
    import asyncio as _aio

    import omnigent.runner.app as runner_app_mod

    session_id = "conv_codex_failed_recover"
    runner_app_mod._session_histories_ref.pop(session_id, None)

    monkeypatch.setattr(
        runner_app_mod,
        "_auto_create_codex_terminal",
        _fake_auto_create_codex_terminal,
    )

    history = [
        {
            "id": "item_user_failed",
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "errored prompt"}],
        },
    ]
    app, _pm, hc = _build_recovery_app(history, harness_name="codex-native")

    try:
        async with _runner_client(app) as client:
            resp = await client.post(
                "/v1/sessions",
                json={"session_id": session_id, "agent_id": "ag_1"},
            )
            assert resp.status_code == 201
            assert resp.json()["status"] == "idle"
            await _aio.sleep(0.1)
    finally:
        runner_app_mod._session_histories_ref.pop(session_id, None)

    assert hc.posted_bodies == [], (
        "Codex-native session startup must not POST a recovery turn for a "
        "mirrored trailing user item. A POST here means the previous failed "
        "prompt was resent to Codex."
    )


@pytest.mark.asyncio
async def test_catch_up_scan_skips_codex_native_history_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Catch-up scan must not replay mirrored Codex-native transcript items.

    Native sessions can enter ``_session_histories`` through normal turn
    processing, not only through ``POST /v1/sessions`` history recovery.
    If catch-up scan treats that native history like a runner-native
    conversation, a tunnel reconnect can fetch a mirrored trailing user
    item and dispatch a duplicate recovery turn to Codex.

    :param monkeypatch: Pytest monkeypatch fixture used to bypass real
        terminal auto-create.
    """
    import asyncio as _aio

    import omnigent.runner.app as runner_app_mod

    session_id = "conv_codex_catchup_skip"
    saved_histories = dict(runner_app_mod._session_histories_ref)
    runner_app_mod._session_histories_ref.clear()
    missed_user_item = {
        "id": "item_missed_user",
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "already typed natively"}],
    }
    server_client = _FakeServerClient([missed_user_item])
    spec = AgentSpec(
        spec_version=1,
        name="catchup-codex-native",
        executor=ExecutorSpec(
            type="omnigent",
            config={"harness": "codex-native"},
        ),
    )
    hc = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_catchup"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_catchup"}}),
        ]
    )
    pm = _FakeProcessManager(hc)

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    monkeypatch.setattr(
        runner_app_mod,
        "_auto_create_codex_terminal",
        _fake_auto_create_codex_terminal,
    )

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server_client,  # type: ignore[arg-type]
    )

    try:
        async with _runner_client(app) as client:
            resp = await client.post(
                "/v1/sessions",
                json={"session_id": session_id, "agent_id": "ag_1"},
            )
            assert resp.status_code == 201
            assert resp.json()["status"] == "idle"

            # Simulate the turn-processing paths that already populated
            # native in-memory history before a tunnel reconnect.
            runner_app_mod._session_histories_ref[session_id] = [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "prior native output"}],
                }
            ]

            server_client.get_calls.clear()
            await app.state.catch_up_scan()
            await _aio.sleep(0.1)
    finally:
        runner_app_mod._session_histories_ref.clear()
        runner_app_mod._session_histories_ref.update(saved_histories)

    assert server_client.get_calls == [], (
        "Catch-up scan must skip Codex-native sessions before fetching Omnigent "
        "items. A GET here means reconnect recovery can observe mirrored "
        "native transcript items and replay them."
    )
    assert hc.posted_bodies == [], (
        "Catch-up scan must not dispatch a recovery turn for a Codex-native "
        "session already present in _session_histories. A POST here means the "
        "mirrored native user item was resent to Codex."
    )


@pytest.mark.asyncio
async def test_session_creation_stays_idle_for_completed_conversation() -> None:
    """POST /v1/sessions with history ending in an assistant message stays idle.

    Breakage this catches: if incomplete-turn detection triggers on
    assistant messages, the runner would start spurious recovery turns.
    """
    history = [
        {
            "id": "item_1",
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "hello"}],
        },
        {
            "id": "item_2",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "hi"}],
        },
    ]
    app, _pm, hc = _build_recovery_app(history)

    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions",
            json={"session_id": "conv_idle_1", "agent_id": "ag_1"},
        )
        assert resp.status_code == 201
        # "idle" proves no recovery turn was started. "running"
        # would mean the detection falsely triggered on a completed
        # conversation.
        assert resp.json()["status"] == "idle"

    # 0 POSTs confirms the harness was never called.
    assert len(hc.posted_bodies) == 0, (
        f"Expected 0 harness POSTs for idle session, "
        f"got {len(hc.posted_bodies)}. >0 = spurious recovery turn."
    )


@pytest.mark.asyncio
async def test_session_creation_auto_starts_turn_for_pending_tool_call() -> None:
    """POST /v1/sessions with history ending in a function_call starts a recovery turn.

    Breakage this catches: if the detection only checks for user
    messages and misses pending tool calls, tool-interrupted sessions
    would stay stuck after crash recovery.
    """
    import asyncio as _aio

    history = [
        {
            "id": "item_1",
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "run ls"}],
        },
        {
            "id": "item_2",
            "type": "function_call",
            "call_id": "call_1",
            "name": "sys_os_shell",
            "arguments": "{}",
        },
    ]
    app, _pm, hc = _build_recovery_app(history)

    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions",
            json={"session_id": "conv_recover_tc", "agent_id": "ag_1"},
        )
        assert resp.status_code == 201
        assert resp.json()["status"] == "running"

        await _aio.sleep(0.5)

    # 1 POST = recovery turn for the pending tool call.
    assert len(hc.posted_bodies) == 1, (
        f"Expected 1 harness POST (recovery for pending tool_call), "
        f"got {len(hc.posted_bodies)}. 0 = detection missed "
        f"function_call items."
    )


@pytest.mark.asyncio
async def test_session_creation_no_recovery_for_empty_history() -> None:
    """POST /v1/sessions with no history stays idle (fresh session).

    Breakage this catches: if the detection crashes on empty history
    (e.g. IndexError on last item), session creation would fail.
    """
    app, _pm, hc = _build_recovery_app([])

    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions",
            json={"session_id": "conv_fresh", "agent_id": "ag_1"},
        )
        assert resp.status_code == 201
        assert resp.json()["status"] == "idle"

    assert len(hc.posted_bodies) == 0, (
        "Fresh session with no history must not trigger a recovery turn."
    )


@pytest.mark.asyncio
async def test_history_load_paginates_beyond_100_items() -> None:
    """_load_history_as_input must paginate when history exceeds one page.

    Breakage this catches: if the history loader fetches only one page
    (limit=100) and doesn't follow has_more, conversations with >100
    items would silently lose early history.
    """
    import asyncio as _aio

    # 150 items — requires 2 pages at limit=100.
    history = [
        {
            "id": f"item_{i}",
            "type": "message",
            "role": "user" if i % 2 == 0 else "assistant",
            "content": [{"type": "input_text", "text": f"msg {i}"}],
        }
        for i in range(150)
    ]
    # Last item is assistant (i=149, odd) so no recovery turn —
    # we're testing pagination, not recovery.
    server_client = _FakeServerClient(history)
    spec = AgentSpec(spec_version=1, name="paginate-test")
    sse_frames = [
        _sse({"type": "response.created", "response": {"id": "resp_p"}}),
        _sse({"type": "response.output_text.delta", "delta": "ok"}),
        _sse({"type": "response.completed", "response": {"id": "resp_p"}}),
    ]
    hc = _ScriptedHarnessClient(sse_frames)
    pm = _FakeProcessManager(hc)

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server_client,  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        # Create session — loads history via pagination.
        resp = await client.post(
            "/v1/sessions",
            json={"session_id": "conv_paginate", "agent_id": "ag_1"},
        )
        assert resp.status_code == 201
        assert resp.json()["status"] == "idle"

        # Now send a message to trigger a turn — the turn uses
        # _session_histories which should have all 150 items.
        resp2 = await client.post(
            "/v1/sessions/conv_paginate/events",
            json={
                "type": "message",
                "role": "user",
                "model": "test",
                "content": [{"type": "input_text", "text": "final"}],
            },
        )
        assert resp2.status_code == 202
        await _aio.sleep(0.5)

    # The server_client should have been called multiple times
    # (pagination). 2 pages for 150 items at limit=100.
    # get_calls[0] has no after cursor (first page).
    # get_calls[1] has after=item_99 (second page).
    assert len(server_client.get_calls) >= 2, (
        f"Expected at least 2 GET calls (pagination), "
        f"got {len(server_client.get_calls)}. "
        f"1 = pagination broken, loader only fetched first page."
    )
    # The harness received the turn with all history in content.
    assert len(hc.posted_bodies) >= 1
    body_content = hc.posted_bodies[0].get("content", [])
    # Must have at least 150 items from the server (the paginated
    # history) plus the new user message. If only 100, pagination
    # stopped at the first page.
    assert len(body_content) > 100, (
        f"Expected >100 history items (all 150 paginated + new "
        f"user msg), got {len(body_content)}. If <=100, pagination "
        f"broke and only one page was loaded."
    )
    assert len(body_content) >= 151, (
        f"Expected at least 151 items (150 server + 1 new), "
        f"got {len(body_content)}. Some server items were dropped."
    )


@pytest.mark.asyncio
async def test_resume_sends_full_history_plus_new_message_to_harness() -> None:
    """Resumed session sends prior history + new user message to the harness.

    Simulates the resume scenario: session was created with a completed
    conversation (user + assistant), then a new message is sent. The
    harness must receive ALL prior history items concatenated with the
    new user message so the LLM has full context.

    Breakage this catches: if _session_histories doesn't include the
    new user message from post_session_events, the harness only sees
    the stale server history (missing the new prompt). If the history
    load fails, the harness sees only the new message (no context).
    """
    import asyncio as _aio

    history = [
        {
            "id": "item_1",
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "Preresume"}],
        },
        {
            "id": "item_2",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "Hello from preresume"}],
        },
    ]
    app, _pm, hc = _build_recovery_app(history)

    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions",
            json={"session_id": "conv_resume_1", "agent_id": "ag_1"},
        )
        # History ends with assistant — session stays idle (no recovery).
        assert resp.status_code == 201
        assert resp.json()["status"] == "idle"

        # Now send a new message (simulating user typing after resume).
        resp2 = await client.post(
            "/v1/sessions/conv_resume_1/events",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "ag_1",
                "model": "test-agent",
                "content": [{"type": "input_text", "text": "Postresume"}],
            },
        )
        # 202 = turn started in background.
        assert resp2.status_code == 202

        await _aio.sleep(0.5)

    # Harness received exactly 1 POST (the new turn).
    assert len(hc.posted_bodies) == 1, (
        f"Expected 1 harness POST (resume turn), got {len(hc.posted_bodies)}. "
        f"0 = turn never started, >1 = duplicate turn."
    )
    body = hc.posted_bodies[0]
    content = body.get("content", [])

    # Content must include at least 3 items: user("Preresume"),
    # assistant("Hello from preresume"), user("Postresume").
    # If only 1, history loading failed. If only 2, the new
    # message wasn't appended to _session_histories.
    # Note: the fake harness stores a dict reference, not a copy.
    # The proxy_stream appends the scripted assistant response to
    # the shared _session_histories list AFTER the harness call,
    # so posted_bodies may show 4 items instead of the 3 the
    # production harness actually received (httpx serializes at
    # call time). Assert >= 3 to cover both shapes.
    assert len(content) >= 3, (
        f"Expected >= 3 history items (2 prior + 1 new), got {len(content)}. Items: {content}"
    )
    # First item: original user message from server history.
    assert content[0].get("type") == "message"
    assert content[0].get("role") == "user"

    # Second item: assistant response from server history.
    assert content[1].get("type") == "message"
    assert content[1].get("role") == "assistant"

    # Third item: the new user message sent after resume.
    assert content[2].get("type") == "message"
    assert content[2].get("role") == "user"
    user_content = content[2].get("content", [])
    # Verify the new message text made it through.
    assert any(
        block.get("text") == "Postresume"
        for block in (user_content if isinstance(user_content, list) else [])
    ), f"New user message 'Postresume' not found in harness content. Content[2]: {content[2]}"


# ── Compaction tests ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_compaction_item_in_history_expands_and_discards_prior() -> None:
    """History loading expands compaction items and discards pre-compaction items.

    Breakage this catches: if _convert_raw_items_to_input drops compaction
    items (the old behavior), the harness receives the full uncompacted
    history — context window overflow on long conversations. If it doesn't
    discard pre-compaction items, the summary is prepended but the original
    items remain — defeating the point of compaction.
    """
    import asyncio as _aio

    history = [
        {
            "id": "item_1",
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "old msg"}],
        },
        {
            "id": "item_2",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "old reply"}],
        },
        {
            "id": "item_3",
            "type": "compaction",
            "summary": "User asked about old stuff. Assistant replied.",
            "last_item_id": "item_2",
            "model": "test-model",
            "token_count": 20,
        },
        {
            "id": "item_4",
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "new msg"}],
        },
    ]
    app, _pm, hc = _build_recovery_app(history)

    async with _runner_client(app) as client:
        # Session has a trailing user message → crash recovery fires.
        resp = await client.post(
            "/v1/sessions",
            json={"session_id": "conv_compact_1", "agent_id": "ag_1"},
        )
        assert resp.status_code == 201
        assert resp.json()["status"] == "running"
        await _aio.sleep(0.5)

    # The harness received the recovery turn.
    assert len(hc.posted_bodies) == 1, (
        f"Expected 1 harness POST (recovery turn), got {len(hc.posted_bodies)}."
    )
    content = hc.posted_bodies[0].get("content", [])

    # 3 items expected: synthetic-user (compaction request),
    # synthetic-assistant (summary), post-compaction user msg.
    # If 5, pre-compaction items weren't discarded.
    # If 1, the compaction item was dropped entirely.
    assert len(content) >= 3, (
        f"Expected >= 3 items (2 synthetic + 1 post-compaction user), "
        f"got {len(content)}. If 1, compaction items are dropped. "
        f"Items: {content}"
    )
    # First item: synthetic user requesting summary.
    assert content[0]["role"] == "user"
    assert "summary" in content[0]["content"][0]["text"].lower()

    # Second item: synthetic assistant with the summary text.
    assert content[1]["role"] == "assistant"
    assert content[1]["content"][0]["text"] == (
        "User asked about old stuff. Assistant replied."
    ), "Summary text must match the compaction item's summary field."

    # Third item: the post-compaction user message.
    assert content[2]["role"] == "user"
    assert content[2]["content"] == [{"type": "input_text", "text": "new msg"}], (
        "Post-compaction items must be converted normally."
    )

    # Pre-compaction items ("old msg", "old reply") must NOT appear.
    all_texts = json.dumps(content)
    assert "old msg" not in all_texts, (
        "Pre-compaction user message leaked through — "
        "_convert_raw_items_to_input didn't discard items before the compaction boundary."
    )
    assert "old reply" not in all_texts, "Pre-compaction assistant message leaked through."


@pytest.mark.asyncio
async def test_error_item_in_history_is_surfaced_as_error_block_not_dropped() -> None:
    """History loading surfaces ``error`` items as typed ERROR blocks, not dropped (#1108).

    Breakage this catches: ``_convert_raw_items_to_input`` used to drop every
    item that wasn't message / function_call / function_call_output, so an
    ``error`` item recorded for a failed turn vanished on history reload — the
    next turn replayed as if the failure had never happened ("silent success").

    The converter now preserves each error item as a typed ``error`` item
    (the ``ErrorData`` shape: ``source`` / ``code`` / ``message``). The fix is
    specifically NOT a synthetic user-role ``input_text`` message: that would
    keep the text visible but mis-attribute the failure to the user's input
    and lose the error semantics. This test pins the typed-error shape and
    guards against a regression back to the user-message shim.
    """
    import asyncio as _aio

    history = [
        {
            "id": "item_1",
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "do the thing"}],
        },
        {
            "id": "item_2",
            "type": "error",
            "response_id": "resp_failed",
            "source": "execution",
            "code": "codex_turn_error",
            "message": "401 Unauthorized: ChatGPT login expired",
        },
        {
            "id": "item_3",
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "try again"}],
        },
    ]
    app, _pm, hc = _build_recovery_app(history)

    async with _runner_client(app) as client:
        # Trailing user message → crash recovery starts a turn, replaying history.
        resp = await client.post(
            "/v1/sessions",
            json={"session_id": "conv_err_1", "agent_id": "ag_1"},
        )
        assert resp.status_code == 201
        assert resp.json()["status"] == "running"
        await _aio.sleep(0.5)

    assert len(hc.posted_bodies) == 1, (
        f"Expected 1 harness POST (recovery turn), got {len(hc.posted_bodies)}."
    )
    content = hc.posted_bodies[0].get("content", [])
    error_items = [
        item for item in content if isinstance(item, dict) and item.get("type") == "error"
    ]
    # The error item survived the converter as a typed ERROR block.
    assert len(error_items) == 1, (
        "Expected exactly one typed 'error' item in the converted history; "
        f"got {len(error_items)}. If 0, the error item was dropped (the "
        "silent-success regression) or wrongly mapped to another type."
    )
    error_item = error_items[0]
    assert error_item["message"] == "401 Unauthorized: ChatGPT login expired"
    # The stable code/source round-trip so the failure stays attributable.
    assert error_item["code"] == "codex_turn_error"
    assert error_item["source"] == "execution"
    # Crucially, the error is NOT mis-attributed as a user input_text message.
    user_texts = [
        block.get("text", "")
        for item in content
        if isinstance(item, dict) and item.get("type") == "message" and item.get("role") == "user"
        for block in (item.get("content") or [])
        if isinstance(block, dict)
    ]
    assert not any("401 Unauthorized" in text for text in user_texts), (
        "Error text leaked into a user message — it must be a typed error block, not user input."
    )


@pytest.mark.asyncio
async def test_crash_recovery_with_compaction_uses_post_compaction_history() -> None:
    """Crash recovery after compaction sees only post-compaction items.

    Breakage this catches: if crash recovery sees pre-compaction items,
    it might start a spurious recovery turn for an item that's already
    been summarized.
    """
    history = [
        {
            "id": "item_1",
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "old"}],
        },
        {
            "id": "item_2",
            "type": "compaction",
            "summary": "Prior context summarized.",
            "last_item_id": "item_1",
            "model": "test-model",
            "token_count": 10,
        },
        {
            "id": "item_3",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "post-compaction reply"}],
        },
    ]
    app, _pm, hc = _build_recovery_app(history)

    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions",
            json={"session_id": "conv_compact_cr", "agent_id": "ag_1"},
        )
        assert resp.status_code == 201
        # History ends with assistant message (post-compaction) → idle.
        # If crash recovery saw the pre-compaction user message ("old"),
        # it would incorrectly start a recovery turn.
        assert resp.json()["status"] == "idle", (
            "Session should be idle — history ends with an assistant message "
            "after the compaction boundary. 'running' would mean crash recovery "
            "looked at pre-compaction items."
        )

    # No harness POSTs — idle session.
    assert len(hc.posted_bodies) == 0, (
        f"Expected 0 harness POSTs for idle post-compaction session, got {len(hc.posted_bodies)}."
    )


class _OverflowThenSuccessHarnessClient:
    """Harness that returns context-window overflow on first call, success on second.

    Used by the reactive compaction test. The first POST returns a
    ``response.failed`` SSE event with ``context_length_exceeded``.
    The second POST returns normal ``response.completed``.

    :param success_frames: SSE frames to return on the second call.
    """

    def __init__(self, success_frames: list[str]) -> None:
        """Initialize with success frames for the retry."""
        self.posted_bodies: list[dict[str, Any]] = []
        self._success_frames = success_frames
        self.patched_events: list[dict[str, Any]] = []
        self._call_count = 0

    def stream(self, method: str, url: str, *, json: dict[str, Any], timeout: Any) -> Any:
        """First call returns overflow; second returns success."""
        del method, url, timeout
        self.posted_bodies.append(json)
        self._call_count += 1
        if self._call_count == 1:
            overflow_frames = [
                _sse(
                    {
                        "type": "response.failed",
                        "error": {
                            "message": (
                                "context_length_exceeded: 5000 tokens > 4096 "
                                "maximum context length"
                            ),
                            "code": "context_length_exceeded",
                        },
                    }
                ),
            ]
            frames = overflow_frames
        else:
            frames = self._success_frames

        class _StreamCtx:
            status_code = 200

            async def __aenter__(self_inner) -> Any:
                return _ScriptedHarnessClient._StreamHandle(frames, None)

            async def __aexit__(self_inner, *_: Any) -> None:
                return None

        return _StreamCtx()

    async def post(self, url: str, *, json: dict[str, Any], timeout: Any = None) -> Any:
        """Record PATCH events and return 200."""
        del url, timeout
        self.patched_events.append(json)

        class _Response:
            status_code = 200

            def raise_for_status(self) -> None:
                pass

        return _Response()


def _build_interrupt_app(
    gate: asyncio.Event,
) -> tuple[FastAPI, _FakeProcessManager, _BlockingHarnessClient]:
    """Build a runner app whose harness emits dangling function_calls.

    The harness streams two ``function_call`` events before blocking
    on *gate*. After the gate is released it streams
    ``response.completed`` — but with no ``function_call_output`` for
    either call, simulating an interrupted tool-chain.

    :param gate: Event that unblocks the harness after the
        function_call frames.
    :returns: ``(app, process_manager, harness_client)`` tuple.
    """
    spec = AgentSpec(spec_version=1, name="t")
    sse_frames = [
        _sse({"type": "response.created", "response": {"id": "resp_int"}}),
        _sse(
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "call_id": "call_a",
                    "name": "read_file",
                    "arguments": '{"path": "/tmp/x"}',
                },
            }
        ),
        # Gate blocks here (after frame index 1).
        _sse(
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "call_id": "call_b",
                    "name": "write_file",
                    "arguments": '{"path": "/tmp/y"}',
                },
            }
        ),
        _sse({"type": "response.completed", "response": {"id": "resp_int"}}),
    ]
    harness_client = _BlockingHarnessClient(sse_frames, gate)
    pm = _FakeProcessManager(harness_client)

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    return app, pm, harness_client


class _ForwardBlockingHarnessClient(_BlockingHarnessClient):
    """Blocks the interrupt FORWARD (``.post``) so a test can assert it is awaited."""

    def __init__(
        self,
        sse_frames: list[str],
        gate: asyncio.Event,
        fwd_gate: asyncio.Event,
    ) -> None:
        """
        :param sse_frames: SSE frames returned by the harness stream.
        :param gate: Event that releases the stream after the first frame.
        :param fwd_gate: Event that releases a blocked interrupt forward.
        """
        super().__init__(sse_frames, gate)
        self._fwd_gate = fwd_gate

    async def post(self, url: str, *, json: dict[str, Any], timeout: Any = None) -> Any:
        """Block an interrupt forward on ``fwd_gate``; pass other posts through."""
        if isinstance(json, dict) and json.get("type") == "interrupt":
            await self._fwd_gate.wait()
        return await super().post(url, json=json, timeout=timeout)


def _build_fwd_blocking_app(
    gate: asyncio.Event,
    fwd_gate: asyncio.Event,
) -> tuple[FastAPI, _FakeProcessManager, _ForwardBlockingHarnessClient]:
    """Build a runner app whose harness stream AND interrupt forward both block.

    :param gate: Releases the harness stream (kept set-never so the turn blocks).
    :param fwd_gate: Releases a blocked interrupt forward.
    :returns: ``(app, process_manager, harness_client)`` tuple.
    """
    spec = AgentSpec(spec_version=1, name="t")
    sse_frames = [
        _sse({"type": "response.created", "response": {"id": "resp_fwd"}}),
        _sse({"type": "response.completed", "response": {"id": "resp_fwd"}}),
    ]
    harness_client = _ForwardBlockingHarnessClient(sse_frames, gate, fwd_gate)
    pm = _FakeProcessManager(harness_client)

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    return app, pm, harness_client


@pytest.mark.asyncio
async def test_interrupt_forwards_to_harness_before_cancelling() -> None:
    """Forward-first: the interrupt is awaited to the harness BEFORE the cancel.

    The harness must receive the interrupt while its turn is still in-flight, so
    its handler engages (cancels the turn + drops the claude-sdk session).
    Cancel-first closed the runner's harness stream first, so the interrupt 404'd
    and the session was never dropped — the next message then resumed the
    abandoned turn and the agent ran one message behind. Here the harness's
    interrupt ``.post`` blocks; the interrupt route must NOT complete until the
    forward is released, proving the forward is awaited first. Cancel-first
    (backgrounded forward) would let the route return immediately.
    """
    import asyncio as _aio

    from omnigent.runner.app import _session_histories_ref

    gate = _aio.Event()  # stream blocks forever
    fwd_gate = _aio.Event()  # interrupt forward blocks until released
    app, _pm, _hc = _build_fwd_blocking_app(gate, fwd_gate)

    async with _runner_client(app) as client:
        conv_id = "conv_fwd_first"
        resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={
                "type": "message",
                "role": "user",
                "model": "test-agent",
                "content": [{"type": "input_text", "text": "go"}],
                "harness": "openai-agents",
            },
        )
        assert resp.status_code == 202
        await _aio.wait_for(_hc.post_seen.wait(), timeout=5.0)

        # The interrupt route must block on the (still-blocked) harness forward —
        # forward-first awaits it before cancelling. If it completes here, the
        # forward was backgrounded (cancel-first) and the harness never got the
        # interrupt in-flight.
        int_task = _aio.create_task(
            client.post(f"/v1/sessions/{conv_id}/events", json={"type": "interrupt"})
        )
        with pytest.raises(_aio.TimeoutError):
            await _aio.wait_for(_aio.shield(int_task), timeout=0.5)
        assert not int_task.done(), "interrupt must await the harness forward (forward-first)"

        # Release the forward → the harness gets the interrupt, then the cancel runs.
        fwd_gate.set()
        int_resp = await _aio.wait_for(int_task, timeout=5.0)
        assert int_resp.status_code == 204, int_resp.text
        markers = _interrupt_markers(list(_session_histories_ref.get(conv_id, [])))

    assert len(markers) == 1, (
        f"interrupt must forward to the harness then finalize the turn with one "
        f"marker; got {len(markers)}."
    )


@pytest.mark.asyncio
async def test_interrupt_inserts_cancellation_items_in_history() -> None:
    """Interrupting a turn with dangling function_calls inserts synthetic outputs.

    When the user interrupts mid-tool-chain, any ``function_call``
    items that were emitted by the harness but never received a
    ``function_call_output`` must get synthetic cancelled outputs.
    A cancellation marker message must also be appended so the LLM
    knows the prior turn was incomplete. This matches the DBOS
    path's ``_append_cancellation_item`` behavior.
    """
    import asyncio as _aio

    from omnigent.runner.app import _session_histories_ref

    gate = _aio.Event()
    app, _pm, _hc = _build_interrupt_app(gate)

    async with _runner_client(app) as client:
        conv_id = "conv_int"

        # Start the turn — it blocks after the first function_call
        # frame, before the second one and response.completed.
        resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={
                "type": "message",
                "role": "user",
                "model": "test-agent",
                "content": [{"type": "input_text", "text": "do something"}],
                "harness": "openai-agents",
            },
        )
        assert resp.status_code == 202
        # Let the background turn reach the gate.
        await _aio.sleep(0.1)

        # Send an interrupt while the turn is blocked.
        int_resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={"type": "interrupt"},
        )
        # The harness stub returns 200; the real scaffold returns
        # 204. Both are success — we care about the side-effect
        # (cancellation items), not the status code.
        assert int_resp.status_code in (200, 204), (
            f"Interrupt must succeed; got {int_resp.status_code}"
        )

        # Release the gate so the harness stream finishes and
        # _on_proxy_stream_end fires with the interrupt flag.
        gate.set()
        await _aio.sleep(0.2)

    # Access the runner's in-memory history for this session.
    histories = _session_histories_ref.get(conv_id, [])

    # The history should contain synthetic function_call_output
    # items for each dangling call_id. call_a was emitted before
    # the gate (always present). call_b may or may not have been
    # emitted depending on timing, but at minimum call_a should
    # have a synthetic output.
    synthetic_outputs = [
        h
        for h in histories
        if h.get("type") == "function_call_output"
        and h.get("output") == "[Cancelled — tool execution was interrupted.]"
    ]
    dangling_calls = [h for h in histories if h.get("type") == "function_call"]
    matched_real_outputs = [
        h
        for h in histories
        if h.get("type") == "function_call_output"
        and h.get("output") != "[Cancelled — tool execution was interrupted.]"
    ]
    dangling_call_ids = {c["call_id"] for c in dangling_calls}
    real_output_call_ids = {o["call_id"] for o in matched_real_outputs}
    orphan_ids = dangling_call_ids - real_output_call_ids
    synthetic_output_call_ids = {o["call_id"] for o in synthetic_outputs}

    # Every orphaned function_call must have a synthetic output.
    # If empty, _append_cancellation_items didn't fire (the interrupt
    # flag wasn't set or wasn't checked in _on_proxy_stream_end).
    assert orphan_ids == synthetic_output_call_ids, (
        f"Every dangling function_call must get a synthetic cancelled "
        f"output. Orphan call_ids={orphan_ids}, synthetic output "
        f"call_ids={synthetic_output_call_ids}. If synthetic is empty, "
        f"_append_cancellation_items was not called on interrupt."
    )

    # Cancellation marker message must be present so the LLM knows
    # the prior output was truncated. If missing, the next turn's
    # context is silently incomplete.
    markers = [
        h
        for h in histories
        if h.get("type") == "message"
        and h.get("role") == "user"
        and any("interrupted" in (b.get("text") or "").lower() for b in h.get("content", []))
    ]
    assert len(markers) == 1, (
        f"Expected exactly 1 cancellation marker message, "
        f"got {len(markers)}. If 0, _append_cancellation_items "
        f"didn't insert the marker."
    )


def _interrupt_markers(histories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the synthetic ``[System: interrupted]`` marker messages."""
    return [
        h
        for h in histories
        if h.get("type") == "message"
        and h.get("role") == "user"
        and any("interrupted" in (b.get("text") or "").lower() for b in h.get("content", []))
    ]


@pytest.mark.asyncio
async def test_interrupt_cancel_floor_finalizes_stuck_turn() -> None:
    """The cancel floor: interrupt force-cancels a turn the harness never finishes.

    Sister to ``test_interrupt_inserts_cancellation_items_in_history``, but the
    gate is NEVER released — the harness stream stays blocked forever, and the
    forwarded interrupt (recorded by the stub but ignored) does not unblock it.
    The turn can therefore only end because the runner force-cancels its turn
    task (``_cancel_active_turn``). If that floor regresses to forward-only, the
    turn stays stuck and no cancellation marker is ever appended.
    """
    import asyncio as _aio

    from omnigent.runner.app import _session_histories_ref

    gate = _aio.Event()  # never set — only the floor's task-cancel can end the turn
    app, _pm, _hc = _build_interrupt_app(gate)

    async with _runner_client(app) as client:
        conv_id = "conv_stuck_int"
        resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={
                "type": "message",
                "role": "user",
                "model": "test-agent",
                "content": [{"type": "input_text", "text": "do something"}],
                "harness": "openai-agents",
            },
        )
        assert resp.status_code == 202
        # Deterministic: the harness stream sets post_seen when it begins, so
        # the turn is in flight and blocked on the gate before we interrupt.
        await _aio.wait_for(_hc.post_seen.wait(), timeout=5.0)

        int_resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={"type": "interrupt"},
        )
        # The handler awaits the cancel, so the turn is finalized by the time
        # this returns — capture history before the client context tears down.
        assert int_resp.status_code == 204, int_resp.text
        markers = _interrupt_markers(list(_session_histories_ref.get(conv_id, [])))

    assert len(markers) == 1, (
        f"The cancel floor must finalize a stuck turn even though the gate was "
        f"never released; got {len(markers)} interrupted markers. 0 means the "
        f"turn is still blocked — interrupt only forwarded to the harness "
        f"without cancelling the runner turn task."
    )


@pytest.mark.asyncio
async def test_stop_session_cancels_inprocess_turn() -> None:
    """``stop_session`` cancels an in-process harness's in-flight turn.

    For non-native harnesses this used to be a 204 no-op — the sidebar Stop did
    nothing. It now routes through the same cancel floor as interrupt: with the
    gate never released, the blocked turn ends only because stop_session
    force-cancels the turn task. 0 markers means the no-op regressed.
    """
    import asyncio as _aio

    from omnigent.runner.app import _session_histories_ref

    gate = _aio.Event()  # never set
    app, _pm, _hc = _build_interrupt_app(gate)

    async with _runner_client(app) as client:
        conv_id = "conv_stuck_stop"
        resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={
                "type": "message",
                "role": "user",
                "model": "test-agent",
                "content": [{"type": "input_text", "text": "do something"}],
                "harness": "openai-agents",
            },
        )
        assert resp.status_code == 202
        # Deterministic: wait for the harness stream to begin (turn in flight,
        # blocked on the gate) before stopping.
        await _aio.wait_for(_hc.post_seen.wait(), timeout=5.0)

        stop_resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={"type": "stop_session"},
        )
        assert stop_resp.status_code == 204, stop_resp.text
        markers = _interrupt_markers(list(_session_histories_ref.get(conv_id, [])))

    assert len(markers) == 1, (
        f"stop_session must cancel the in-flight in-process turn (was a 204 "
        f"no-op); got {len(markers)} interrupted markers. 0 means stop_session "
        f"still no-ops for non-native harnesses."
    )


@pytest.mark.asyncio
async def test_interrupt_during_setup_phase_recovers_stuck_turn() -> None:
    """Interrupt during the setup phase finalizes the turn — the session isn't stuck.

    A cancel that lands while the turn is still in setup (here: blocked in the
    background turn's spec resolution, before ``_drain_streaming_response`` is
    entered) raises ``CancelledError`` past ``_run_turn_bg``'s
    ``except Exception`` (it's a ``BaseException``), so neither the drain handler
    nor the setup handler cleans up. Without the floor's setup-phase recovery,
    ``_active_turns`` keeps the done task — every later message buffers behind a
    turn that never runs and the session hangs.

    Proof: after the setup-phase interrupt, a NEW message must start a fresh
    turn (its setup re-enters the resolver). With the bug it would be buffered
    behind the stale ``_active_turns`` entry and never run. Also asserts exactly
    one interrupted marker from the cancelled turn.
    """
    import asyncio as _aio

    from omnigent.runner.app import _session_histories_ref

    resolver_gate = _aio.Event()  # released only in teardown → spec resolution blocks
    resolver_entered = _aio.Event()
    spec = AgentSpec(spec_version=1, name="t")

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Signal entry, then block so the background turn stalls in setup."""
        del agent_id, session_id
        resolver_entered.set()
        await resolver_gate.wait()
        return spec

    # Frames let the eventual (post-teardown) turn drain cleanly.
    hc = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_s"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_s"}}),
        ]
    )
    pm = _FakeProcessManager(hc)
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    conv_id = "conv_setup_cancel"
    # agent_id is required for the background turn's setup to invoke the resolver.
    msg = {
        "type": "message",
        "role": "user",
        "model": "test-agent",
        "agent_id": "ag_1",
        "content": [{"type": "input_text", "text": "do something"}],
        "harness": "openai-agents",
    }
    fresh_turn_started = False
    async with _runner_client(app) as client:
        # First turn: the handler returns 202, then the background turn blocks in
        # spec resolution (the setup phase).
        r1 = await client.post(f"/v1/sessions/{conv_id}/events", json=msg)
        assert r1.status_code == 202
        await _aio.wait_for(resolver_entered.wait(), timeout=5.0)

        # Interrupt while the turn is still in setup.
        int_resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={"type": "interrupt"},
        )
        assert int_resp.status_code == 204, int_resp.text
        markers = _interrupt_markers(list(_session_histories_ref.get(conv_id, [])))

        # The session must accept a new turn. If _active_turns were left stale,
        # this message buffers behind the dead turn and never starts a new one,
        # so the resolver is never re-entered.
        resolver_entered.clear()
        r2 = await client.post(f"/v1/sessions/{conv_id}/events", json=msg)
        assert r2.status_code == 202
        try:
            await _aio.wait_for(resolver_entered.wait(), timeout=5.0)
            fresh_turn_started = True
        except _aio.TimeoutError:
            fresh_turn_started = False

        resolver_gate.set()  # release the blocked turn for clean teardown
        await _aio.sleep(0.1)

    assert fresh_turn_started, (
        "After a setup-phase interrupt the session must accept a new turn; the "
        "follow-up message never re-entered spec resolution, so _active_turns was "
        "left stale and the session is stuck (the bug this guards)."
    )
    # Exactly one marker = only the interrupted first turn was finalized.
    assert len(markers) == 1, (
        f"The interrupted setup-phase turn must append exactly one marker; got {len(markers)}."
    )


@pytest.mark.asyncio
async def test_interrupt_marker_instructs_model_to_disregard_abandoned_request() -> None:
    """The cancellation marker tells the model to drop the canceled request.

    The bug this guards against: a marker that only says the assistant
    reply was cut off leaves the canceled user instruction in history,
    so the next turn replays it and the agent follows the abandoned
    request. The marker must explicitly instruct the model not to resume
    the interrupted request and to treat the next user message as current.
    A revert to the old "halted the agent response / may be incomplete"
    text (no disregard instruction) turns this test red.
    """
    import asyncio as _aio

    from omnigent.runner.app import _session_histories_ref

    gate = _aio.Event()
    app, _pm, _hc = _build_interrupt_app(gate)

    async with _runner_client(app) as client:
        conv_id = "conv_int_disregard"
        resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={
                "type": "message",
                "role": "user",
                "model": "test-agent",
                "content": [{"type": "input_text", "text": "delete all my files"}],
                "harness": "openai-agents",
            },
        )
        assert resp.status_code == 202
        await _aio.sleep(0.1)

        int_resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={"type": "interrupt"},
        )
        assert int_resp.status_code in (200, 204)

        gate.set()
        await _aio.sleep(0.2)

    histories = _session_histories_ref.get(conv_id, [])
    marker_texts = [
        b.get("text") or ""
        for h in histories
        if h.get("type") == "message" and h.get("role") == "user"
        for b in h.get("content", [])
        if "interrupted" in (b.get("text") or "").lower()
    ]
    assert len(marker_texts) == 1, (
        f"Expected exactly 1 interrupted marker, got {len(marker_texts)}."
    )
    marker = marker_texts[0].lower()
    # "abandoned" framing + an explicit do-not-continue instruction are what
    # make the model drop the canceled request. The old marker had neither;
    # asserting both fails loud if the disregard semantics regress.
    assert "abandon" in marker, (
        f"Marker must frame the prior request as abandoned, got: {marker_texts[0]!r}"
    )
    assert "do not resume or act on" in marker, (
        f"Marker must instruct the model not to continue the interrupted "
        f"request, got: {marker_texts[0]!r}"
    )
    # The original assistant-incomplete disclaimer is still useful context
    # and must be retained alongside the new instruction.
    assert "may be" in marker and "incomplete" in marker, (
        f"Marker should still note the assistant message may be incomplete, "
        f"got: {marker_texts[0]!r}"
    )


@pytest.mark.asyncio
async def test_external_session_status_idle_delivers_forwarded_native_output_to_parent_inbox() -> (
    None
):
    """
    Native idle status completes sub-agent work with AP-forwarded output.

    Native harness transcript items are persisted by Omnigent server, so the
    runner's local history can be empty or stale. A forwarded
    ``data.output`` value must be used for the parent inbox instead of
    falling back to the runner-local history.
    """
    from omnigent.runner import app as runner_app
    from omnigent.runner.tool_dispatch import execute_tool

    parent_id = "conv_parent_native_complete"
    child_id = "conv_child_native_complete"
    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    runner_app._session_inboxes_ref[parent_id] = session_inbox
    runner_app._session_histories_ref[child_id] = [
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "LOCAL_SHOULD_NOT_WIN"}],
        }
    ]
    runner_app.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="worker",
        title="native",
    )

    async def _server_handler(request: httpx.Request) -> httpx.Response:
        """Serve the parent TOOL_RESULT policy check."""
        if (
            request.method == "POST"
            and request.url.path == f"/v1/sessions/{parent_id}/policies/evaluate"
        ):
            return httpx.Response(200, json={"result": "POLICY_ACTION_UNSPECIFIED"})
        return httpx.Response(404, json={"error": str(request.url)})

    try:
        async with _runner_client(app) as client:
            resp = await client.post(
                f"/v1/sessions/{child_id}/events",
                json={
                    "type": "external_session_status",
                    "data": {"status": "idle", "output": "AP_NATIVE_DONE"},
                },
            )
        assert resp.status_code == 204, resp.text

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(_server_handler),
            base_url="http://server",
        ) as server_client:
            inbox_output = await execute_tool(
                tool_name="sys_read_inbox",
                arguments="{}",
                server_client=server_client,
                conversation_id=parent_id,
                session_inbox=session_inbox,
            )
    finally:
        runner_app.unregister_subagent_work(child_id)
        runner_app._session_inboxes_ref.pop(parent_id, None)
        runner_app._session_histories_ref.pop(child_id, None)

    assert "sub-agent task conv_child_native_complete completed" in inbox_output
    assert "worker:native returned: AP_NATIVE_DONE" in inbox_output
    assert "LOCAL_SHOULD_NOT_WIN" not in inbox_output


@pytest.mark.asyncio
async def test_external_session_status_running_fans_out_child_busy_to_parent() -> None:
    """
    Native child ``running`` status updates the parent's child-session cache.

    Codex-native and claude-native workers report their real terminal
    lifecycle through ``external_session_status`` after the runner's prompt
    injection turn has already completed. The parent stream must still receive
    a ``session.child_session.updated`` delta with ``busy=True``; otherwise
    Nessie's Agents rail has no durable "Working" signal for native children.
    """
    from omnigent.runner import app as runner_app

    parent_id = "conv_parent_native_status_fanout"
    child_id = "conv_child_native_status_fanout"
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    runner_app._session_event_queues_ref.pop(parent_id, None)
    runner_app._session_event_queues_ref.pop(child_id, None)
    runner_app.register_child_session(
        child_id,
        parent_session_id=parent_id,
        title="codex:impl",
        tool="codex",
        session_name="impl",
    )
    runner_app.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="codex",
        title="impl",
    )
    entry = runner_app.get_subagent_work(child_id)
    assert entry is not None
    assert entry.status == "launching"

    try:
        async with _runner_client(app) as client:
            resp = await client.post(
                f"/v1/sessions/{child_id}/events",
                json={"type": "external_session_status", "data": {"status": "running"}},
            )
        assert resp.status_code == 204, resp.text
        entry = runner_app.get_subagent_work(child_id)
        assert entry is not None
        assert entry.status == "running"

        events = _drain_session_event_queue(runner_app._session_event_queues_ref.get(parent_id))
    finally:
        runner_app.unregister_subagent_work(child_id)
        runner_app.unregister_child_session(child_id)
        runner_app._session_event_queues_ref.pop(parent_id, None)
        runner_app._session_event_queues_ref.pop(child_id, None)

    assert events == [
        {
            "type": "session.child_session.updated",
            "conversation_id": parent_id,
            "child_session_id": child_id,
            "child": {
                "id": child_id,
                "title": "codex:impl",
                "tool": "codex",
                "session_name": "impl",
                "busy": True,
                "current_task_status": "in_progress",
                "last_task_error": None,
            },
        }
    ]


@pytest.mark.asyncio
async def test_external_status_sequence_coalesces_duplicates_but_emits_task_status_change() -> (
    None
):
    """
    Native status fan-out coalesces duplicates, not task-status changes.

    The child rail should not churn on repeated ``running`` edges, but a rare
    ``idle`` → ``failed`` sequence must still update ``current_task_status``
    from ``"completed"`` to ``"failed"`` even though both edges are non-busy.
    """
    from omnigent.runner import app as runner_app

    parent_id = "conv_parent_native_status_sequence"
    child_id = "conv_child_native_status_sequence"
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    runner_app._session_event_queues_ref.pop(parent_id, None)
    runner_app._session_event_queues_ref.pop(child_id, None)
    runner_app.register_child_session(
        child_id,
        parent_session_id=parent_id,
        title="codex:impl",
        tool="codex",
        session_name="impl",
    )

    try:
        async with _runner_client(app) as client:
            for status, output in [
                ("running", None),
                ("running", None),
                ("idle", "DONE"),
                ("failed", "BROKEN"),
            ]:
                data = {"status": status}
                if output is not None:
                    data["output"] = output
                resp = await client.post(
                    f"/v1/sessions/{child_id}/events",
                    json={"type": "external_session_status", "data": data},
                )
                assert resp.status_code == 204, resp.text

        events = _drain_session_event_queue(runner_app._session_event_queues_ref.get(parent_id))
    finally:
        runner_app.unregister_child_session(child_id)
        runner_app._session_event_queues_ref.pop(parent_id, None)
        runner_app._session_event_queues_ref.pop(child_id, None)

    assert events == [
        {
            "type": "session.child_session.updated",
            "conversation_id": parent_id,
            "child_session_id": child_id,
            "child": {
                "id": child_id,
                "title": "codex:impl",
                "tool": "codex",
                "session_name": "impl",
                "busy": True,
                "current_task_status": "in_progress",
                "last_task_error": None,
            },
        },
        {
            "type": "session.child_session.updated",
            "conversation_id": parent_id,
            "child_session_id": child_id,
            "child": {
                "id": child_id,
                "title": "codex:impl",
                "tool": "codex",
                "session_name": "impl",
                "busy": False,
                "current_task_status": "completed",
                "last_message_preview": "DONE",
            },
        },
        {
            "type": "session.child_session.updated",
            "conversation_id": parent_id,
            "child_session_id": child_id,
            "child": {
                "id": child_id,
                "title": "codex:impl",
                "tool": "codex",
                "session_name": "impl",
                "busy": False,
                "current_task_status": "failed",
                "last_message_preview": "BROKEN",
            },
        },
    ]


@pytest.mark.asyncio
async def test_external_status_idle_fans_out_forwarded_output_preview_to_parent() -> None:
    """
    Native child ``idle`` status uses AP-forwarded output for rail preview.

    Native terminal transcripts are persisted by AP, not runner-local history.
    A terminal-observed idle edge must therefore fan out the ``data.output``
    value forwarded by AP; otherwise the Agents rail can replace the real
    native reply with stale runner-local text while clearing the spinner.
    """
    from omnigent.runner import app as runner_app

    parent_id = "conv_parent_native_preview_fanout"
    child_id = "conv_child_native_preview_fanout"
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    runner_app._session_event_queues_ref.pop(parent_id, None)
    runner_app._session_event_queues_ref.pop(child_id, None)
    runner_app._session_histories_ref[child_id] = [
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "STALE_RUNNER_HISTORY"}],
        }
    ]
    runner_app.register_child_session(
        child_id,
        parent_session_id=parent_id,
        title="codex:impl",
        tool="codex",
        session_name="impl",
    )

    try:
        async with _runner_client(app) as client:
            resp = await client.post(
                f"/v1/sessions/{child_id}/events",
                json={
                    "type": "external_session_status",
                    "data": {"status": "idle", "output": "AP_NATIVE_DONE"},
                },
            )
        assert resp.status_code == 204, resp.text

        events = _drain_session_event_queue(runner_app._session_event_queues_ref.get(parent_id))
    finally:
        runner_app.unregister_child_session(child_id)
        runner_app._session_event_queues_ref.pop(parent_id, None)
        runner_app._session_event_queues_ref.pop(child_id, None)
        runner_app._session_histories_ref.pop(child_id, None)

    assert events == [
        {
            "type": "session.child_session.updated",
            "conversation_id": parent_id,
            "child_session_id": child_id,
            "child": {
                "id": child_id,
                "title": "codex:impl",
                "tool": "codex",
                "session_name": "impl",
                "busy": False,
                "current_task_status": "completed",
                "last_message_preview": "AP_NATIVE_DONE",
            },
        }
    ]


@pytest.mark.asyncio
async def test_external_status_idle_without_output_omits_stale_history_preview() -> None:
    """
    Native child ``idle`` without forwarded output omits stale local text.

    If Omnigent has no authoritative native transcript text to forward, the parent
    rail and parent inbox must not fall back to runner-local history: native
    runner history may be stale because the terminal forwarder owns
    persistence. The inbox receives an explicit empty result so the parent can
    still observe completion without fabricated output.
    """
    from omnigent.runner import app as runner_app

    parent_id = "conv_parent_native_no_preview"
    child_id = "conv_child_native_no_preview"
    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    server_client = _WakeRecordingServerClient(parent_id)
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=server_client,  # type: ignore[arg-type]
    )

    runner_app._session_event_queues_ref.pop(parent_id, None)
    runner_app._session_event_queues_ref.pop(child_id, None)
    runner_app._session_inboxes_ref[parent_id] = session_inbox
    runner_app._session_histories_ref[child_id] = [
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "STALE_RUNNER_HISTORY"}],
        }
    ]
    runner_app.register_child_session(
        child_id,
        parent_session_id=parent_id,
        title="codex:impl",
        tool="codex",
        session_name="impl",
    )
    runner_app.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="codex",
        title="impl",
    )

    try:
        async with _runner_client(app) as client:
            resp = await client.post(
                f"/v1/sessions/{child_id}/events",
                json={"type": "external_session_status", "data": {"status": "idle"}},
            )
        assert resp.status_code == 204, resp.text
        await asyncio.wait_for(server_client.wake_seen.wait(), timeout=5.0)

        events = _drain_session_event_queue(runner_app._session_event_queues_ref.get(parent_id))
    finally:
        runner_app.unregister_child_session(child_id)
        runner_app.unregister_subagent_work(child_id)
        runner_app._session_event_queues_ref.pop(parent_id, None)
        runner_app._session_event_queues_ref.pop(child_id, None)
        runner_app._session_inboxes_ref.pop(parent_id, None)
        runner_app._session_histories_ref.pop(child_id, None)

    assert events == [
        {
            "type": "session.child_session.updated",
            "conversation_id": parent_id,
            "child_session_id": child_id,
            "child": {
                "id": child_id,
                "title": "codex:impl",
                "tool": "codex",
                "session_name": "impl",
                "busy": False,
                "current_task_status": "completed",
            },
        }
    ]
    assert session_inbox.qsize() == 1, (
        f"Expected one empty completion in the parent inbox, got {session_inbox.qsize()}."
    )
    delivered = session_inbox.get_nowait()
    assert delivered["status"] == "completed"
    assert delivered["output"] == ""
    assert delivered["output"] != "STALE_RUNNER_HISTORY"
    assert len(server_client.wake_posts) == 1


class _WakeRecordingServerClient(NullServerClient):
    """Records the runner→AP wake POSTs a parent session's ``/events`` receives.

    Subclasses :class:`NullServerClient` so every other runner→AP call still
    gets a benign empty 200; only POSTs to the watched parent's ``/events``
    path are captured. ``wake_seen`` lets a test await the (background) wake
    deterministically instead of sleeping.
    """

    def __init__(self, parent_id: str) -> None:
        """
        :param parent_id: Parent session whose ``/events`` POSTs to capture,
            e.g. ``"conv_parent_wake"``.
        """
        self._parent_events_path = f"/v1/sessions/{parent_id}/events"
        self.wake_posts: list[dict[str, Any]] = []
        self.wake_seen = asyncio.Event()

    async def post(self, url: str, **kwargs: Any) -> NullServerClient._Response:
        """Capture a wake POST to the watched parent, else defer to the base.

        :param url: Request URL, e.g. ``"/v1/sessions/conv_parent_wake/events"``.
        :param kwargs: Request kwargs; the wake notice is in ``json``.
        :returns: Stub 200 response from :class:`NullServerClient`.
        """
        if url == self._parent_events_path:
            body = kwargs.get("json")
            if isinstance(body, dict):
                self.wake_posts.append(body)
            self.wake_seen.set()
        return await super().post(url, **kwargs)


@pytest.mark.asyncio
async def test_native_subagent_completion_wakes_idle_parent() -> None:
    """
    A finished native sub-agent wakes its idle parent via a ``/events`` POST.

    nessie's workers are native harnesses whose completion arrives as an
    ``external_session_status: idle`` event. Delivering that completion to the
    parent inbox must ALSO post a ``[System: ...]`` wake notice to the
    *parent's* event stream, so an idle orchestrator takes a continuation turn
    instead of sleeping until the next user message. Without the wake wiring
    the inbox still fills but no parent ``/events`` POST is made — exactly the
    "nessie doesn't know its sub-agent finished" bug this fixes.
    """
    from omnigent.runner import app as runner_app

    parent_id = "conv_parent_wake"
    child_id = "conv_child_wake"
    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    server_client = _WakeRecordingServerClient(parent_id)
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=server_client,  # type: ignore[arg-type]
    )

    runner_app._session_inboxes_ref[parent_id] = session_inbox
    runner_app.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="claude_code",
        title="auth",
    )

    try:
        async with _runner_client(app) as client:
            resp = await client.post(
                f"/v1/sessions/{child_id}/events",
                json={
                    "type": "external_session_status",
                    "data": {"status": "idle", "output": "WORKER_DONE"},
                },
            )
            assert resp.status_code == 204, resp.text
            # Wake is a background task; await the recorded POST (TimeoutError if none).
            await asyncio.wait_for(server_client.wake_seen.wait(), timeout=5.0)
    finally:
        runner_app.unregister_subagent_work(child_id)
        runner_app._session_inboxes_ref.pop(parent_id, None)

    # Delivery still happens: the child result is in the parent inbox. If 0,
    # external_session_status:idle did not deliver the completion at all.
    assert session_inbox.qsize() == 1, (
        f"Expected one completion in the parent inbox, got {session_inbox.qsize()}."
    )
    delivered = session_inbox.get_nowait()
    assert delivered["status"] == "completed"
    assert delivered["output"] == "WORKER_DONE"
    # Exactly one wake notice was POSTed to the PARENT's event stream. If 0,
    # the completion landed in the inbox but the idle parent was never woken
    # (the regression this test guards against).
    assert len(server_client.wake_posts) == 1, (
        f"Expected one wake POST to the parent, got {len(server_client.wake_posts)}."
    )
    wake_text = server_client.wake_posts[0]["data"]["content"][0]["text"]
    # Notice names the finished worker and steers the parent to drain the inbox.
    assert "sub-agent claude_code/auth finished (completed)" in wake_text
    assert "sys_read_inbox" in wake_text


@pytest.mark.asyncio
async def test_external_status_for_untracked_session_does_not_wake() -> None:
    """
    Completing a session that is not a tracked sub-agent wakes nobody.

    This is the loop-safety guarantee: the orchestrator's own turn ending
    routes through the same call site, but it is not registered as anyone's
    child, so ``mark_subagent_work_terminal`` returns an untracked ack and no
    wake is scheduled. A regression that dropped the ``entry is not None`` guard
    would either 500 (None.delivered) or post a spurious wake — both caught here.
    """
    orphan_id = "conv_not_a_subagent"
    server_client = _WakeRecordingServerClient(orphan_id)
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=server_client,  # type: ignore[arg-type]
    )

    # No register_subagent_work for orphan_id — it is nobody's child.
    async with _runner_client(app) as client:
        resp = await client.post(
            f"/v1/sessions/{orphan_id}/events",
            json={
                "type": "external_session_status",
                "data": {"status": "idle", "output": "IGNORED"},
            },
        )
        assert resp.status_code == 204, resp.text
        # Let any erroneously-scheduled wake task run before asserting absence.
        for _ in range(5):
            await asyncio.sleep(0)

    # No wake was scheduled because the session is untracked. A non-empty list
    # would mean the orchestrator could wake (and loop on) its own turn-end.
    assert server_client.wake_posts == []
    assert not server_client.wake_seen.is_set()


@pytest.mark.asyncio
async def test_tracked_subagent_status_without_parent_inbox_returns_503() -> None:
    """
    A tracked sub-agent terminal status is not ACKed without parent delivery.

    The parent inbox is the durable handoff point for async sub-agent results.
    If the runner has a child work entry but the parent inbox is missing, a
    204 would tell AP/the forwarder the completion was delivered even though
    the parent can never drain it.
    """
    from omnigent.runner import app as runner_app

    parent_id = "conv_parent_missing_inbox"
    child_id = "conv_child_missing_inbox"
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    runner_app.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="worker",
        title="lost-parent",
    )

    try:
        async with _runner_client(app) as client:
            resp = await client.post(
                f"/v1/sessions/{child_id}/events",
                json={
                    "type": "external_session_status",
                    "data": {"status": "idle", "output": "DONE_BUT_UNDELIVERED"},
                },
            )
        entry = runner_app.get_subagent_work(child_id)
    finally:
        runner_app.unregister_subagent_work(child_id)

    assert resp.status_code == 503, resp.text
    assert resp.json()["reason"] == "missing_parent_inbox"
    assert entry is not None
    # The child is terminal, but not delivered; if delivered were True here,
    # the runner would have ACKed a result the parent cannot read.
    assert entry.status == "completed"
    assert entry.delivered is False


def test_subagent_terminal_delivery_retry_uses_latest_undelivered_report() -> None:
    """
    Terminal retry delivers the latest report after the parent inbox reappears.

    A first terminal report can arrive while runner-local parent state is
    missing. The work entry must stay undelivered, but if a later terminal
    report carries newer status/output before the parent inbox returns, the
    parent should receive that latest report rather than stale cancellation
    text from the first failed delivery attempt.
    """
    from omnigent.runner import app as runner_app

    parent_id = "conv_parent_retry_missing_inbox"
    child_id = "conv_child_retry_missing_inbox"
    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    runner_app.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="worker",
        title="retry",
    )

    try:
        first_ack = runner_app.mark_subagent_work_terminal(
            child_id,
            status="cancelled",
            output="[System: sub-agent stopped]",
        )
        runner_app._session_inboxes_ref[parent_id] = session_inbox
        second_ack = runner_app.mark_subagent_work_terminal(
            child_id,
            status="completed",
            output="DONE_AFTER_RETRY",
        )
        entry = runner_app.get_subagent_work(child_id)
    finally:
        runner_app.unregister_subagent_work(child_id)
        runner_app._session_inboxes_ref.pop(parent_id, None)

    assert first_ack.reason == "missing_parent_inbox"
    assert first_ack.delivered is False
    assert first_ack.delivered_now is False
    assert second_ack.reason == "delivered"
    assert second_ack.delivered is True
    assert second_ack.delivered_now is True
    assert entry is not None
    assert entry.delivered is True
    assert session_inbox.qsize() == 1
    delivered = session_inbox.get_nowait()
    assert delivered["task_id"] == child_id
    assert delivered["status"] == "completed"
    assert delivered["output"] == "DONE_AFTER_RETRY"


def test_subagent_terminal_delivery_handles_missing_output() -> None:
    """
    Terminal work with no assistant text still delivers a marker payload.

    Native status reporters can emit ``idle`` without a final assistant
    message. That must not become an unstructured ``RuntimeError`` after the
    parent inbox is available.
    """
    from omnigent.runner import app as runner_app

    parent_id = "conv_parent_missing_output"
    child_id = "conv_child_missing_output"
    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    runner_app._session_inboxes_ref[parent_id] = session_inbox
    runner_app.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="worker",
        title="empty-output",
    )

    try:
        ack = runner_app.mark_subagent_work_terminal(
            child_id,
            status="completed",
            output=None,
        )
    finally:
        runner_app.unregister_subagent_work(child_id)
        runner_app._session_inboxes_ref.pop(parent_id, None)

    assert ack.reason == "delivered"
    assert ack.delivered is True
    assert session_inbox.qsize() == 1
    delivered = session_inbox.get_nowait()
    assert delivered["output"] == "[System: sub-agent completed with no output]"


@pytest.mark.asyncio
async def test_known_subagent_status_without_work_entry_returns_503() -> None:
    """
    A runner-known sub-agent session is not ACKed without a work entry.

    After runner state loss, the child session may still report terminal
    status while the child→parent work registry no longer has the handoff
    metadata. Returning 503 forces the AP/forwarder path to preserve the
    failed delivery instead of reporting success.
    """
    child_id = "conv_child_missing_work_entry"
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={
                "session_id": child_id,
                "agent_id": "ag_missing_work_entry",
                "sub_agent_name": "worker",
            },
        )
        assert create_resp.status_code == 201, create_resp.text
        try:
            resp = await client.post(
                f"/v1/sessions/{child_id}/events",
                json={
                    "type": "external_session_status",
                    "data": {"status": "idle", "output": "DONE_WITH_NO_WORK_ENTRY"},
                },
            )
        finally:
            await client.delete(f"/v1/sessions/{child_id}")

    assert resp.status_code == 503, resp.text
    assert resp.json()["reason"] == "missing_work_entry"


@pytest.mark.asyncio
async def test_repeated_idle_status_wakes_parent_only_once() -> None:
    """
    Re-posting a child's idle status wakes the parent only once.

    The wake gate fires on the not-delivered → delivered transition. A second
    ``external_session_status: idle`` for an already-terminal child must NOT
    re-deliver or re-wake — this is what keeps a parallel fan-out (or a
    forwarder that re-sends idle) from triggering a wake storm.
    """
    from omnigent.runner import app as runner_app

    parent_id = "conv_parent_wake_once"
    child_id = "conv_child_wake_once"
    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    server_client = _WakeRecordingServerClient(parent_id)
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=server_client,  # type: ignore[arg-type]
    )

    runner_app._session_inboxes_ref[parent_id] = session_inbox
    runner_app.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="worker",
        title="phase-a",
    )

    idle_event = {
        "type": "external_session_status",
        "data": {"status": "idle", "output": "DONE"},
    }
    try:
        async with _runner_client(app) as client:
            resp1 = await client.post(f"/v1/sessions/{child_id}/events", json=idle_event)
            assert resp1.status_code == 204, resp1.text
            await asyncio.wait_for(server_client.wake_seen.wait(), timeout=5.0)
            server_client.wake_seen.clear()

            resp2 = await client.post(f"/v1/sessions/{child_id}/events", json=idle_event)
            assert resp2.status_code == 204, resp2.text
            # Give a (wrongly) re-scheduled wake a chance to land before asserting.
            for _ in range(5):
                await asyncio.sleep(0)
    finally:
        runner_app.unregister_subagent_work(child_id)
        runner_app._session_inboxes_ref.pop(parent_id, None)

    # One delivery, one wake — the second idle was a no-op. A count of 2 would
    # mean the already-delivered gate regressed and re-marking re-wakes.
    assert session_inbox.qsize() == 1, (
        f"Expected one inbox item after two idle posts, got {session_inbox.qsize()}."
    )
    assert len(server_client.wake_posts) == 1, (
        f"Expected one wake POST after two idle posts, got {len(server_client.wake_posts)}."
    )


@pytest.mark.asyncio
async def test_delete_session_clears_pending_subagent_wake() -> None:
    """
    Deleting a parent clears its outstanding sub-agent wake debounce.

    A wake POST remains pending until the parent starts a turn. If the parent
    session is deleted before consuming that wake, the debounce entry must go
    away too; otherwise a later session reusing the same id can receive a child
    result in its inbox but never get the wake notice that tells it to drain.
    """
    from omnigent.runner import app as runner_app

    parent_id = "conv_parent_delete_clears_wake"
    first_child_id = "conv_child_before_delete"
    second_child_id = "conv_child_after_delete"
    first_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    second_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    server_client = _WakeRecordingServerClient(parent_id)
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=server_client,  # type: ignore[arg-type]
    )

    try:
        async with _runner_client(app) as client:
            create_resp = await client.post(
                "/v1/sessions",
                json={"session_id": parent_id, "agent_id": "ag_parent_delete_wake"},
            )
            assert create_resp.status_code == 201, create_resp.text

            runner_app._session_inboxes_ref[parent_id] = first_inbox
            runner_app.register_subagent_work(
                parent_session_id=parent_id,
                child_session_id=first_child_id,
                agent="worker",
                title="before-delete",
            )
            first_resp = await client.post(
                f"/v1/sessions/{first_child_id}/events",
                json={
                    "type": "external_session_status",
                    "data": {"status": "idle", "output": "BEFORE_DELETE"},
                },
            )
            assert first_resp.status_code == 204, first_resp.text
            await asyncio.wait_for(server_client.wake_seen.wait(), timeout=5.0)
            assert len(server_client.wake_posts) == 1

            delete_resp = await client.delete(f"/v1/sessions/{parent_id}")
            assert delete_resp.status_code == 200, delete_resp.text
            server_client.wake_seen.clear()

            runner_app._session_inboxes_ref[parent_id] = second_inbox
            runner_app.register_subagent_work(
                parent_session_id=parent_id,
                child_session_id=second_child_id,
                agent="worker",
                title="after-delete",
            )
            second_resp = await client.post(
                f"/v1/sessions/{second_child_id}/events",
                json={
                    "type": "external_session_status",
                    "data": {"status": "idle", "output": "AFTER_DELETE"},
                },
            )
            assert second_resp.status_code == 204, second_resp.text
            await asyncio.wait_for(server_client.wake_seen.wait(), timeout=5.0)
    finally:
        runner_app.unregister_subagent_work(first_child_id)
        runner_app.unregister_subagent_work(second_child_id)
        runner_app.unregister_subagent_work_for_session(parent_id)
        runner_app._session_inboxes_ref.pop(parent_id, None)

    assert first_inbox.qsize() == 1
    assert second_inbox.qsize() == 1
    assert len(server_client.wake_posts) == 2, (
        f"Expected a fresh wake after deleting and reusing the parent id, got "
        f"{len(server_client.wake_posts)} wake posts."
    )
    followup_text = server_client.wake_posts[1]["data"]["content"][0]["text"]
    assert "sub-agent worker/after-delete finished (completed)" in followup_text


@pytest.mark.asyncio
async def test_subagent_completion_during_parent_wake_turn_posts_followup_wake() -> None:
    """
    A child finishing during the parent's wake turn posts the next wake.

    The first child completion creates an outstanding wake for the parent. Once
    the parent starts processing that wake notice, the debounce must be
    considered consumed. A second child completion that lands while the parent
    turn is still active should therefore enqueue a follow-up wake rather than
    leaving the result stranded until a human sends another message.
    """
    from omnigent.runner import app as runner_app

    parent_id = "conv_parent_wake_turn_race"
    first_child_id = "conv_child_initial_wake"
    second_child_id = "conv_child_followup_wake"
    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    server_client = _WakeRecordingServerClient(parent_id)
    gate = asyncio.Event()
    harness_client = _BlockingHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_wake_turn"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_wake_turn"}}),
        ],
        gate,
    )
    pm = _FakeProcessManager(harness_client)  # type: ignore[arg-type]
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=server_client,  # type: ignore[arg-type]
    )

    runner_app._session_inboxes_ref[parent_id] = session_inbox
    runner_app.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=first_child_id,
        agent="codex",
        title="initial",
    )

    try:
        async with _runner_client(app) as client:
            first_resp = await client.post(
                f"/v1/sessions/{first_child_id}/events",
                json={
                    "type": "external_session_status",
                    "data": {"status": "idle", "output": "FIRST_DONE"},
                },
            )
            assert first_resp.status_code == 204, first_resp.text
            await asyncio.wait_for(server_client.wake_seen.wait(), timeout=5.0)
            assert len(server_client.wake_posts) == 1
            server_client.wake_seen.clear()

            parent_resp = await client.post(
                f"/v1/sessions/{parent_id}/events",
                json={
                    "type": "message",
                    "role": "user",
                    "agent_id": "ag_parent_wake_turn",
                    "model": "test-agent",
                    "harness": "openai-agents",
                    "content": [{"type": "input_text", "text": "wake notice"}],
                },
            )
            assert parent_resp.status_code == 202, parent_resp.text
            await asyncio.wait_for(harness_client.post_seen.wait(), timeout=5.0)
            assert harness_client.posted_bodies, "parent wake turn must reach the harness"

            runner_app.register_subagent_work(
                parent_session_id=parent_id,
                child_session_id=second_child_id,
                agent="codex",
                title="followup",
            )
            second_resp = await client.post(
                f"/v1/sessions/{second_child_id}/events",
                json={
                    "type": "external_session_status",
                    "data": {"status": "idle", "output": "SECOND_DONE"},
                },
            )
            assert second_resp.status_code == 204, second_resp.text
            await asyncio.wait_for(server_client.wake_seen.wait(), timeout=5.0)
            gate.set()
    finally:
        gate.set()
        runner_app.unregister_subagent_work(first_child_id)
        runner_app.unregister_subagent_work(second_child_id)
        runner_app._session_inboxes_ref.pop(parent_id, None)

    assert session_inbox.qsize() == 2, (
        f"Expected both completions in the parent inbox, got {session_inbox.qsize()}."
    )
    assert len(server_client.wake_posts) == 2, (
        f"Expected a follow-up wake after the parent turn started, got "
        f"{len(server_client.wake_posts)} wake posts."
    )
    followup_text = server_client.wake_posts[1]["data"]["content"][0]["text"]
    assert "sub-agent codex/followup finished (completed)" in followup_text
    assert "sys_read_inbox" in followup_text


@pytest.mark.asyncio
async def test_parent_idle_with_stuck_wake_flag_posts_recovery_wake() -> None:
    """
    A parent going idle while holding a stuck wake flag posts a recovery wake.

    Guards the multi-round fan-out stranding bug. ``_subagent_wake_pending`` is
    cleared only at turn start, so a child that completes mid-turn re-arms the
    flag with no later turn to clear it; the parent then idles with the flag
    stuck and results still in the inbox, and further completions are debounced
    and stranded. The fix (``_rewake_parent_if_inbox_stranded`` from
    ``_check_and_start_next_turn``) re-arms one wake on idle.

    Sequence (wake counts bracketed): (1) child A completes idle → wake [1],
    parent turn starts (clears flag); (2) child B completes mid-turn → wake [2],
    re-arms flag; (3) turn ends → recovery wake [3] WITH the fix, stays [2]
    without it (the discriminator); (4) child C completes → correctly
    *coalesced* against the re-armed flag (inbox grows, no 4th wake). Child C is
    kept only to pin that coalesce contract — the signal is the step-3 wake.
    """
    from omnigent.runner import app as runner_app

    parent_id = "conv_parent_rewake_after_consume"
    child_a = "conv_child_round1_a"
    child_b = "conv_child_round1_b"
    child_c = "conv_child_round2_c"
    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    server_client = _WakeRecordingServerClient(parent_id)
    gate = asyncio.Event()
    harness_client = _BlockingHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_rewake"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_rewake"}}),
        ],
        gate,
    )
    pm = _FakeProcessManager(harness_client)  # type: ignore[arg-type]
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=server_client,  # type: ignore[arg-type]
    )

    runner_app._session_inboxes_ref[parent_id] = session_inbox
    runner_app.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_a,
        agent="claude",
        title="debate",
    )

    try:
        async with _runner_client(app) as client:
            # 1. Child A finishes while the parent is idle → first wake.
            resp_a = await client.post(
                f"/v1/sessions/{child_a}/events",
                json={
                    "type": "external_session_status",
                    "data": {"status": "idle", "output": "A_DONE"},
                },
            )
            assert resp_a.status_code == 204, resp_a.text
            await asyncio.wait_for(server_client.wake_seen.wait(), timeout=5.0)
            # Just the idle-parent wake; 0 = never fired, 2 = spurious POST.
            assert len(server_client.wake_posts) == 1, (
                f"Expected the single idle-parent wake, got {len(server_client.wake_posts)}."
            )
            server_client.wake_seen.clear()

            # Start the parent's wake turn (blocking harness holds it active).
            # post_seen resolves only after _run_turn_bg's turn-start clear, so
            # the flag is guaranteed clear for child B below.
            parent_resp = await client.post(
                f"/v1/sessions/{parent_id}/events",
                json={
                    "type": "message",
                    "role": "user",
                    "agent_id": "ag_parent_rewake",
                    "model": "test-agent",
                    "harness": "openai-agents",
                    "content": [{"type": "input_text", "text": "wake notice"}],
                },
            )
            assert parent_resp.status_code == 202, parent_resp.text
            await asyncio.wait_for(harness_client.post_seen.wait(), timeout=5.0)

            # 2. Child B finishes DURING the active parent turn: not debounced
            # (flag was just cleared), posts its own wake, and re-arms the flag.
            # No new turn starts on this wake, so nothing clears it again.
            runner_app.register_subagent_work(
                parent_session_id=parent_id,
                child_session_id=child_b,
                agent="gpt",
                title="debate",
            )
            resp_b = await client.post(
                f"/v1/sessions/{child_b}/events",
                json={
                    "type": "external_session_status",
                    "data": {"status": "idle", "output": "B_DONE"},
                },
            )
            assert resp_b.status_code == 204, resp_b.text
            await asyncio.wait_for(server_client.wake_seen.wait(), timeout=5.0)
            server_client.wake_seen.clear()
            # Baseline before the parent idles: A + B, recovery not yet fired.
            # Not 2 → child B posted no distinct wake, so the stuck-flag
            # precondition the fix recovers from is not reproduced.
            wakes_before_idle = len(server_client.wake_posts)
            assert wakes_before_idle == 2, (
                f"Expected 2 wakes (idle-parent A + mid-turn B) before the "
                f"parent goes idle, got {wakes_before_idle}."
            )

            # 3. End the parent turn → _check_and_start_next_turn runs the
            # re-arm. Await the recovery wake directly (not a fixed sleep);
            # without the fix it never posts and this wait_for times out.
            gate.set()
            try:
                await asyncio.wait_for(server_client.wake_seen.wait(), timeout=5.0)
            except TimeoutError:
                raise AssertionError(
                    "No recovery wake was posted after the parent went idle "
                    "holding a stuck wake flag with a non-empty inbox. The "
                    f"count stayed at {len(server_client.wake_posts)} "
                    "(expected it to grow to 3). This is the multi-round "
                    "fan-out stranding bug: _rewake_parent_if_inbox_stranded "
                    "did not re-arm the wake at turn end."
                ) from None
            # Recovery wake is the 3rd POST. 2 = re-arm never fired (stranding
            # bug); 4 = double-posted.
            assert len(server_client.wake_posts) == 3, (
                f"Expected the recovery wake to bring the count to 3, got "
                f"{len(server_client.wake_posts)}."
            )
            server_client.wake_seen.clear()

            # Let the turn fully settle so C is unambiguously a post-idle event.
            _deadline = asyncio.get_running_loop().time() + 5.0
            while app.state.has_active_work():
                if asyncio.get_running_loop().time() > _deadline:
                    raise AssertionError("parent wake turn did not end within 5s")
                await asyncio.sleep(0.01)
            for _ in range(5):
                await asyncio.sleep(0)

            # 4. Child C finishes post-idle. The recovery wake re-armed the
            # flag, so C must COALESCE: result lands in the inbox, no new wake.
            # Yield generously so a (wrongly) scheduled extra wake would land.
            runner_app.register_subagent_work(
                parent_session_id=parent_id,
                child_session_id=child_c,
                agent="claude",
                title="debate",
            )
            resp_c = await client.post(
                f"/v1/sessions/{child_c}/events",
                json={
                    "type": "external_session_status",
                    "data": {"status": "idle", "output": "C_DONE"},
                },
            )
            assert resp_c.status_code == 204, resp_c.text
            for _ in range(10):
                await asyncio.sleep(0)
    finally:
        gate.set()
        runner_app.unregister_subagent_work(child_a)
        runner_app.unregister_subagent_work(child_b)
        runner_app.unregister_subagent_work(child_c)
        runner_app._session_inboxes_ref.pop(parent_id, None)

    # All 3 completions reached the inbox regardless of coalescing; 2 = a
    # completion was lost.
    assert session_inbox.qsize() == 3, (
        f"Expected all 3 completions in the parent inbox, got {session_inbox.qsize()}."
    )
    # Exactly 3 wakes: A + B + recovery, with C coalesced. 4 = fix wrongly woke
    # per-completion; 2 = recovery wake never fired (stranding regression).
    assert len(server_client.wake_posts) == 3, (
        f"Expected exactly 3 wakes (A + B + recovery, with C coalesced), got "
        f"{len(server_client.wake_posts)}."
    )
    # Recovery wake (3rd) names child B — the latest completed at turn end, not
    # C — and reports the 2 results stranded then (A + B, before C).
    recovery_text = server_client.wake_posts[2]["data"]["content"][0]["text"]
    assert "sub-agent gpt/debate finished (completed)" in recovery_text, (
        f"Recovery wake should name child B (gpt/debate), the latest "
        f"completed child at turn end; got: {recovery_text!r}"
    )
    assert "2 results waiting in inbox" in recovery_text, (
        f"Recovery wake should report the 2 results stranded at turn end "
        f"(A's + B's, before C); got: {recovery_text!r}"
    )
    assert "sys_read_inbox" in recovery_text


@pytest.mark.asyncio
async def test_parent_idle_with_stuck_wake_flag_and_drained_inbox_clears_flag() -> None:
    """
    A parent idling with a stuck wake flag but an EMPTY inbox clears the flag.

    Companion to ``test_parent_idle_with_stuck_wake_flag_posts_recovery_wake``,
    which covers the stuck-flag + *non-empty* inbox case (re-arm a recovery
    wake). This guards the sibling variant the reviewer flagged:
    the parent consumes a mid-turn wake as an injection AND drains
    ``sys_read_inbox`` in that *same* live turn, so the turn ends with the
    debounce flag still set (turn start is the only place it clears) but the
    inbox already emptied. The buggy helper returned early on
    ``inbox.empty()`` WITHOUT discarding the flag, so the flag stayed stuck
    forever — and the NEXT child completion was debounced and stranded. The
    fix (``_rewake_parent_if_inbox_stranded``) discards the flag on idle
    *regardless* of inbox state, posting a recovery wake only when results
    remain.

    The closure-local ``_subagent_wake_pending`` set lives inside
    ``create_runner_app`` and has no module-level ref (unlike
    ``_session_inboxes_ref``), so the flag-clear is asserted *behaviorally* —
    which is also the stronger, user-facing claim: a subsequent child
    completion WAKES the parent (fresh POST) instead of being silently
    debounced. A stuck flag would suppress that wake, which is exactly the
    stranding the fix prevents. This is the discriminator: it goes red on the
    buggy ``inbox.empty()``-returns-without-discard ordering and green on the
    fix.

    Sequence (wake counts bracketed): (1) child A completes idle → wake [1],
    parent turn starts (clears flag); (2) child B completes mid-turn → wake
    [2], re-arms flag (A's + B's results now both queued); (3) the test drains
    the inbox to EMPTY in-turn (mirrors the parent draining via
    ``sys_read_inbox`` during its live turn) — flag stays set; (4) the turn
    ends idle → helper discards the stuck flag and, because the inbox is
    empty, posts NO recovery wake (count stays [2]); (5) child C completes
    post-idle → because the flag was cleared, C is NOT debounced and posts a
    fresh wake [3]. Under the bug, step 4 leaves the flag set, so step 5's C
    is debounced (count stays [2]) and C's result strands.
    """
    from omnigent.runner import app as runner_app

    parent_id = "conv_parent_rewake_drained_inbox"
    child_a = "conv_child_drained_a"
    child_b = "conv_child_drained_b"
    child_c = "conv_child_drained_c"
    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    server_client = _WakeRecordingServerClient(parent_id)
    gate = asyncio.Event()
    harness_client = _BlockingHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_drained"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_drained"}}),
        ],
        gate,
    )
    pm = _FakeProcessManager(harness_client)  # type: ignore[arg-type]
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=server_client,  # type: ignore[arg-type]
    )

    runner_app._session_inboxes_ref[parent_id] = session_inbox
    runner_app.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_a,
        agent="claude",
        title="debate",
    )

    try:
        async with _runner_client(app) as client:
            # 1. Child A finishes while the parent is idle → first wake.
            resp_a = await client.post(
                f"/v1/sessions/{child_a}/events",
                json={
                    "type": "external_session_status",
                    "data": {"status": "idle", "output": "A_DONE"},
                },
            )
            assert resp_a.status_code == 204, resp_a.text
            await asyncio.wait_for(server_client.wake_seen.wait(), timeout=5.0)
            # Just the idle-parent wake; 0 = never fired, 2 = spurious POST.
            assert len(server_client.wake_posts) == 1, (
                f"Expected the single idle-parent wake, got {len(server_client.wake_posts)}."
            )
            server_client.wake_seen.clear()

            # Start the parent's wake turn (blocking harness holds it active).
            # post_seen resolves only after _run_turn_bg's turn-start clear, so
            # the flag is guaranteed clear for child B below.
            parent_resp = await client.post(
                f"/v1/sessions/{parent_id}/events",
                json={
                    "type": "message",
                    "role": "user",
                    "agent_id": "ag_parent_drained",
                    "model": "test-agent",
                    "harness": "openai-agents",
                    "content": [{"type": "input_text", "text": "wake notice"}],
                },
            )
            assert parent_resp.status_code == 202, parent_resp.text
            await asyncio.wait_for(harness_client.post_seen.wait(), timeout=5.0)

            # 2. Child B finishes DURING the active parent turn: not debounced
            # (flag was just cleared), posts its own wake, and re-arms the flag.
            # No new turn starts on this wake, so nothing clears it again.
            runner_app.register_subagent_work(
                parent_session_id=parent_id,
                child_session_id=child_b,
                agent="gpt",
                title="debate",
            )
            resp_b = await client.post(
                f"/v1/sessions/{child_b}/events",
                json={
                    "type": "external_session_status",
                    "data": {"status": "idle", "output": "B_DONE"},
                },
            )
            assert resp_b.status_code == 204, resp_b.text
            await asyncio.wait_for(server_client.wake_seen.wait(), timeout=5.0)
            server_client.wake_seen.clear()
            # Baseline before the drain: A + B wakes fired, recovery not yet.
            # Not 2 → child B posted no distinct wake, so the stuck-flag
            # precondition the fix recovers from is not reproduced.
            wakes_before_drain = len(server_client.wake_posts)
            assert wakes_before_drain == 2, (
                f"Expected 2 wakes (idle-parent A + mid-turn B) before the "
                f"inbox is drained, got {wakes_before_drain}."
            )

            # 3. Drain the inbox to EMPTY while the turn is still active — the
            # B wake_seen above guarantees both completions are already queued
            # (delivery put_nowait precedes the wake task). This stands in for
            # the parent draining sys_read_inbox during its live turn: the
            # debounce flag is still set (only turn start clears it), but the
            # inbox is now empty. This is the precondition the non-empty-inbox
            # sibling test deliberately never creates.
            drained = 0
            while not session_inbox.empty():
                session_inbox.get_nowait()
                drained += 1
            # A + B were delivered into the inbox before either wake task ran.
            # Not 2 → a completion never reached the inbox (delivery regression),
            # which would invalidate the empty-inbox precondition below.
            assert drained == 2, (
                f"Expected to drain A's + B's queued completions (2), got "
                f"{drained}; the empty-inbox-at-idle precondition is invalid."
            )
            assert session_inbox.empty(), "inbox must be empty before the turn ends"

            # 4. End the parent turn → _check_and_start_next_turn runs the
            # stuck-flag clear. The inbox is empty, so NO recovery wake posts;
            # the only observable here is that the flag was discarded, which
            # step 5 proves. Wait for the turn to fully end (no recovery wake
            # to await on, unlike the non-empty-inbox sibling).
            gate.set()
            _deadline = asyncio.get_running_loop().time() + 5.0
            while app.state.has_active_work():
                if asyncio.get_running_loop().time() > _deadline:
                    raise AssertionError("parent wake turn did not end within 5s")
                await asyncio.sleep(0.01)
            for _ in range(5):
                await asyncio.sleep(0)
            # No recovery wake fired on the empty inbox: still 2. A 3rd here
            # would mean the helper wrongly re-armed a wake with nothing
            # stranded (the non-empty path leaking into the empty case).
            assert len(server_client.wake_posts) == 2, (
                f"Expected no recovery wake on the drained (empty) inbox, "
                f"got {len(server_client.wake_posts)} wakes."
            )

            # 5. Child C finishes post-idle. If step 4 cleared the stuck flag
            # (the fix), C is NOT debounced and posts a FRESH wake. If the flag
            # stayed stuck (the bug: inbox.empty() returned before discard), C
            # is debounced and its result strands with no wake — the exact
            # regression this guards. Await the wake directly: under the bug it
            # never posts and this wait_for times out.
            runner_app.register_subagent_work(
                parent_session_id=parent_id,
                child_session_id=child_c,
                agent="claude",
                title="debate",
            )
            resp_c = await client.post(
                f"/v1/sessions/{child_c}/events",
                json={
                    "type": "external_session_status",
                    "data": {"status": "idle", "output": "C_DONE"},
                },
            )
            assert resp_c.status_code == 204, resp_c.text
            try:
                await asyncio.wait_for(server_client.wake_seen.wait(), timeout=5.0)
            except TimeoutError:
                raise AssertionError(
                    "No wake was posted for child C after the parent idled with "
                    "a drained (empty) inbox. The wake count stayed at "
                    f"{len(server_client.wake_posts)} (expected it to grow to "
                    "3). This is the drained-inbox stuck-flag bug: "
                    "_rewake_parent_if_inbox_stranded returned on inbox.empty() "
                    "WITHOUT discarding _subagent_wake_pending, so child C was "
                    "debounced and stranded."
                ) from None
    finally:
        gate.set()
        runner_app.unregister_subagent_work(child_a)
        runner_app.unregister_subagent_work(child_b)
        runner_app.unregister_subagent_work(child_c)
        runner_app._session_inboxes_ref.pop(parent_id, None)

    # Child C posted the 3rd wake: the stuck flag was cleared on idle. 2 = flag
    # stayed stuck and C was debounced (the regression); 4 = a spurious extra
    # wake (e.g. an erroneous empty-inbox recovery POST in step 4).
    assert len(server_client.wake_posts) == 3, (
        f"Expected exactly 3 wakes (A + B + C's post-clear wake), got "
        f"{len(server_client.wake_posts)}."
    )
    # The 3rd wake is C's: it names child C and steers the parent to drain.
    # A wrong name would mean a different completion posted this wake.
    c_wake_text = server_client.wake_posts[2]["data"]["content"][0]["text"]
    assert "sub-agent claude/debate finished (completed)" in c_wake_text, (
        f"Third wake should name child C (claude/debate); got: {c_wake_text!r}"
    )
    assert "sys_read_inbox" in c_wake_text
    # C's completion reached the inbox (1 item, queued after the step-3 drain).
    # Under the bug C still delivers, but with no wake the parent never learns
    # to drain it — 0 here would instead mean C's delivery itself regressed.
    assert session_inbox.qsize() == 1, (
        f"Expected child C's completion alone in the drained inbox, got {session_inbox.qsize()}."
    )
    delivered_c = session_inbox.get_nowait()
    assert delivered_c["status"] == "completed"
    assert delivered_c["output"] == "C_DONE"


@pytest.mark.asyncio
async def test_replayed_idle_status_after_inbox_drain_is_acknowledged() -> None:
    """
    Replayed terminal status after parent drain is a benign duplicate.

    ``sys_read_inbox`` removes delivered work after the parent collects it.
    The runner keeps a delivered tombstone so a later native forwarder replay
    sees an already-delivered ack instead of a false ``missing_work_entry``
    503 for a still-known child session.
    """
    from omnigent.runner import app as runner_app
    from omnigent.runner.tool_dispatch import execute_tool

    parent_id = "conv_parent_drain_replay"
    child_id = "conv_child_drain_replay"
    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    server_client = _WakeRecordingServerClient(parent_id)
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=server_client,  # type: ignore[arg-type]
    )
    runner_app._session_inboxes_ref[parent_id] = session_inbox
    runner_app.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="worker",
        title="drained",
    )

    idle_event = {
        "type": "external_session_status",
        "data": {"status": "idle", "output": "DONE_AND_DRAINED"},
    }

    async def _policy_handler(request: httpx.Request) -> httpx.Response:
        """
        Allow the parent TOOL_RESULT policy check during inbox drain.

        :param request: Policy-evaluation request from ``sys_read_inbox``.
        :returns: Allow verdict for the delayed sub-agent output.
        """
        if (
            request.method == "POST"
            and request.url.path == f"/v1/sessions/{parent_id}/policies/evaluate"
        ):
            return httpx.Response(200, json={"result": "POLICY_ACTION_UNSPECIFIED"})
        return httpx.Response(404, json={"error": str(request.url)})

    try:
        async with _runner_client(app) as client:
            create_resp = await client.post(
                "/v1/sessions",
                json={
                    "session_id": child_id,
                    "agent_id": "ag_drain_replay",
                    "sub_agent_name": "worker",
                },
            )
            assert create_resp.status_code == 201, create_resp.text
            first_resp = await client.post(f"/v1/sessions/{child_id}/events", json=idle_event)
            assert first_resp.status_code == 204, first_resp.text
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(_policy_handler),
                base_url="http://server",
            ) as policy_client:
                drain_output = await execute_tool(
                    tool_name="sys_read_inbox",
                    arguments="{}",
                    server_client=policy_client,
                    conversation_id=parent_id,
                    session_inbox=session_inbox,
                )
            assert runner_app.get_subagent_work(child_id) is None
            replay_resp = await client.post(f"/v1/sessions/{child_id}/events", json=idle_event)
    finally:
        runner_app.unregister_subagent_work(child_id)
        runner_app.unregister_subagent_work_for_session(child_id)
        runner_app._session_inboxes_ref.pop(parent_id, None)

    assert "DONE_AND_DRAINED" in drain_output
    assert replay_resp.status_code == 204, replay_resp.text
    assert session_inbox.qsize() == 0


@pytest.mark.asyncio
async def test_concurrent_subagent_completions_coalesce_into_one_wake() -> None:
    """
    A fan-out's completions debounce to a single wake POST.

    When a parent dispatches several workers and they finish close together,
    each completion is delivered to the parent inbox, but only the FIRST posts
    a wake notice — the rest are suppressed while that wake is outstanding.
    The one wake turn drains the whole inbox via sys_read_inbox. Without the
    debounce, N completions POST N synthetic /events messages, churning turns
    and tripping the executor's per-turn tool-context guard ("no active turn
    context") — the regression this guards against.
    """
    from omnigent.runner import app as runner_app

    parent_id = "conv_parent_fanout"
    child_ids = ["conv_child_fan_a", "conv_child_fan_b", "conv_child_fan_c"]
    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    server_client = _WakeRecordingServerClient(parent_id)
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=server_client,  # type: ignore[arg-type]
    )

    runner_app._session_inboxes_ref[parent_id] = session_inbox
    for idx, child_id in enumerate(child_ids):
        runner_app.register_subagent_work(
            parent_session_id=parent_id,
            child_session_id=child_id,
            agent="claude_code",
            title=f"worker-{idx}",
        )

    try:
        async with _runner_client(app) as client:
            for child_id in child_ids:
                resp = await client.post(
                    f"/v1/sessions/{child_id}/events",
                    json={
                        "type": "external_session_status",
                        "data": {"status": "idle", "output": f"DONE_{child_id}"},
                    },
                )
                assert resp.status_code == 204, resp.text
            # Let the (single, debounced) wake task and any suppressed ones run.
            await asyncio.wait_for(server_client.wake_seen.wait(), timeout=5.0)
            for _ in range(5):
                await asyncio.sleep(0)
    finally:
        for child_id in child_ids:
            runner_app.unregister_subagent_work(child_id)
        runner_app._session_inboxes_ref.pop(parent_id, None)

    # All three completions were delivered to the parent inbox...
    assert session_inbox.qsize() == 3, (
        f"Expected all 3 completions in the parent inbox, got {session_inbox.qsize()}."
    )
    # ...but only ONE wake notice was posted (the other two were debounced).
    # A count of 3 means the debounce regressed and the wake storm is back.
    assert len(server_client.wake_posts) == 1, (
        f"Expected exactly one (debounced) wake POST for the fan-out, got "
        f"{len(server_client.wake_posts)}."
    )


@pytest.mark.asyncio
async def test_events_interrupt_on_native_session_injects_escape_without_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    POST ``/events`` with ``{"type": "interrupt"}`` on a claude-native
    session sends Escape to the pane — and nothing else to the transcript.

    The interrupt handler's whole job is the Escape keystroke. This test
    pins the three properties of that handler:

    1. ``inject_interrupt`` is called with the bridge dir derived from
       the conversation id (the Escape keystroke that actually stops
       Claude).
    2. NO ``[System: interrupted]`` marker is appended to the runner's
       in-memory ``_session_histories``. That synthetic marker is for
       in-process LLM harnesses, where the runner's history is the
       model's next-turn context; Claude-native owns its own session and
       records the interrupt in its own (forwarder-mirrored) transcript,
       so persisting a forged ``role:"user"`` marker only diverged the
       AP-side mirror from Claude's real transcript.
    3. NO ``session.status: idle`` is enqueued: idle on interrupt now
       comes solely from the terminal's PTY-activity watcher (it sees
       the pane quiesce after the Escape), which also keeps the session
       ``running`` if the interrupt didn't take. Synthesizing idle here
       would bypass the watcher's running/idle dedupe and could strand
       the UI on idle.

    If side effect 1 is missing, the Escape never lands and Claude keeps
    generating; if a marker reappears in 2, the holdover that forged the
    user bubble is back; if a synthesized idle reappears in 3, the
    watcher desync bug is back.
    """
    from omnigent.runner.app import _session_event_queues_ref, _session_histories_ref
    from omnigent.spec.types import ExecutorSpec

    captured_inject: list[Any] = []

    def _fake_inject(bridge_dir: Any, *, timeout_s: float) -> None:
        """Record the call and return without touching tmux."""
        captured_inject.append((bridge_dir, timeout_s))

    monkeypatch.setattr(claude_native_bridge, "inject_interrupt", _fake_inject)

    # Native spec: executor.type="omnigent" + config.harness="claude-native"
    # is the canonical shape the runner reads at session start to
    # populate _session_spec_cache; _session_harness_name reads it
    # back at interrupt time to pick the right dispatch branch.
    native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the native spec for any agent_id."""
        del agent_id
        return native_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        # POST /v1/sessions seeds _session_spec_cache so the
        # interrupt dispatch can detect "claude-native".
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": "conv_native_int", "agent_id": "ag_1"},
        )
        assert create_resp.status_code == 201, create_resp.text

        # POST /events with type=interrupt. By the time this returns,
        # ``_handle_claude_native_interrupt`` has fully run — the sync
        # history mutation (``_append_cancellation_items``) and the
        # sub-agent wake finished before the response. So we can read
        # both ``_session_histories`` and ``_session_event_queues`` from
        # the test without any subscribe / sleep dance.
        int_resp = await client.post(
            "/v1/sessions/conv_native_int/events",
            json={"type": "interrupt"},
        )

        # Snapshot history + drain queue BEFORE deleting the session.
        # DELETE clears ``_session_histories`` and pops the queue from
        # ``_session_event_queues``, so reading after delete would
        # always see empty.
        captured_history = list(_session_histories_ref.get("conv_native_int", []))
        queue = _session_event_queues_ref.get("conv_native_int")
        assert queue is not None, (
            "Session creation should have initialized the event queue "
            "for ``conv_native_int``; ``_session_event_queues_ref`` is "
            "missing the entry, so we couldn't drain it to verify the "
            "interrupt handler did not enqueue a synthesized idle."
        )
        queued_events: list[dict[str, Any]] = []
        while not queue.empty():
            item = queue.get_nowait()
            if isinstance(item, dict):
                queued_events.append(item)

    # 1) tmux Escape was sent via inject_interrupt.
    # 0 = the dispatch fell through to the generic forward-to-harness
    # path (which 404s for native — silent regression).
    assert int_resp.status_code == 204, (
        f"Native interrupt must return 204 from /events; "
        f"got {int_resp.status_code}: {int_resp.text}"
    )
    assert len(captured_inject) == 1, (
        f"Expected one inject_interrupt call, got {len(captured_inject)}. "
        f"If 0, the dispatch in /events did not route to the native "
        f"handler — possibly _session_harness_name returned the wrong "
        f"canonical name."
    )
    bridge_dir, timeout_s = captured_inject[0]
    assert bridge_dir == bridge_dir_for_conversation_id("conv_native_int")
    # 1.0s short timeout: UI stop must feel snappy. If this becomes
    # the helper's 30s default, the user's click would hang on any
    # missing tmux.json.
    assert timeout_s == 1.0

    # 2) NO cancellation marker is appended to the runner's in-memory
    # history. The native interrupt must not forge a [System: interrupted]
    # user message into the AP-side mirror — Claude records the interrupt
    # in its own transcript. If a marker reappears, _append_cancellation_items
    # was wired back into the native handler.
    markers = [
        h
        for h in captured_history
        if h.get("type") == "message"
        and h.get("role") == "user"
        and any("interrupted" in (b.get("text") or "").lower() for b in h.get("content", []))
    ]
    assert markers == [], (
        f"Expected no [System: interrupted] marker in history, got "
        f"{len(markers)}. If 1, _append_cancellation_items was re-invoked "
        f"by the native interrupt handler — the holdover that forged a "
        f"user bubble into the mirror is back. History: {captured_history!r}"
    )

    # 3) The interrupt handler must NOT synthesize session.status: idle.
    # Idle on interrupt now comes from the terminal's PTY activity watcher
    # (it sees the pane quiesce after the Escape) — the single source of
    # truth, which also keeps the session ``running`` if the interrupt
    # didn't take. Synthesizing idle here would bypass the watcher's
    # running/idle dedupe and could strand the UI on idle. This guards
    # against re-adding the pre-PTY synthesized idle.
    status_idle = [
        e for e in queued_events if e.get("type") == "session.status" and e.get("status") == "idle"
    ]
    assert status_idle == [], (
        f"The native interrupt handler must not enqueue session.status: idle "
        f"(the PTY watcher emits it on quiesce); got {status_idle!r} on the "
        f"queue: {queued_events!r}."
    )


@pytest.mark.parametrize(
    ("harness", "expected_statuses"),
    [
        # claude-native's working status is owned by the PTY-activity
        # watcher, so the runner injection task must not publish its
        # own running/idle edges.
        ("claude-native", []),
        # codex-native may use the runner's running edge so the thread
        # shows work as soon as Omnigent accepts the turn, but must not use the
        # runner's idle edge because the injection task completes before
        # the user-visible Codex turn.
        ("codex-native", ["running"]),
        # antigravity-native shares codex's shape: the executor's
        # SendUserCascadeMessage returns as soon as agy accepts the turn, so the
        # RPC read driver (not the runner injection task) owns idle. Publishing
        # the runner's idle here fires ~2s before agy's output streams and
        # prematurely completes the response (the live-e2e "double-idle").
        ("antigravity-native", ["running"]),
        # Non-terminal harnesses have no external lifecycle observer; the
        # runner turn remains their source of truth.
        ("openai-agents", ["running", "idle"]),
    ],
)
@pytest.mark.asyncio
async def test_message_turn_lifecycle_status_suppressed_for_terminal_backed_harnesses(
    harness: str,
    expected_statuses: list[str],
) -> None:
    """
    Runner lifecycle status is edge-specific for terminal-backed harnesses.

    First-principles invariant: the thread's "Working…" indicator should
    represent the user-visible model turn. For claude-native, the runner
    turn is only a pane-injection task, so its ``running`` and ``idle`` edges
    are both suppressed. For codex-native, the runner's ``running`` edge is a
    useful immediate signal that Omnigent accepted the turn, but its ``idle`` edge
    is invalid because the injection task finishes before Codex is done.

    Drives the real ``POST /events`` message path and waits for the
    background injection turn to finish, so both the synchronous ``running``
    edge and the invalid injection-task ``idle`` edge are observable if they
    regress.

    :param harness: Harness configured for the session, e.g.
        ``"codex-native"``.
    :param expected_statuses: Runner-published lifecycle statuses expected
        on the session stream, e.g. ``["running", "idle"]``.
    :returns: None.
    """
    from omnigent.runner.app import _session_event_queues_ref

    session_id = f"conv_ts_{harness.replace('-', '_')}"
    spec = AgentSpec(
        spec_version=1,
        name="t",
        # executor.type="omnigent" + config.harness=<harness> is the
        # canonical shape the runner reads at session start to populate
        # _session_spec_cache; _session_harness_name reads it back to
        # decide whether a PTY watcher owns this session's status.
        executor=ExecutorSpec(type="omnigent", config={"harness": harness}),
    )
    stream_finished = asyncio.Event()
    harness_client = _ScriptedHarnessClient([], stream_finished=stream_finished)
    app = create_runner_app(
        process_manager=_FakeProcessManager(harness_client),  # type: ignore[arg-type]
        spec_resolver=await _spec_resolver_returning(spec),
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        # POST /v1/sessions seeds _session_spec_cache so the turn path can
        # resolve the harness and decide whether to suppress turn status.
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": session_id, "agent_id": "ag_1"},
        )
        assert create_resp.status_code == 201, create_resp.text
        # Native session creation may try to auto-create a terminal. This
        # fixture intentionally has no real terminal registry / workspace, so
        # that setup path can enqueue ``session.status: failed`` before the
        # message turn under test. Drain creation-time events so the assertion
        # below isolates only the runner turn lifecycle around POST /events.
        queue = _session_event_queues_ref.get(session_id)
        assert queue is not None, (
            f"session creation must initialize the per-session event queue "
            f"for {session_id!r}; missing means the turn-status publish had "
            f"nowhere to land."
        )
        while not queue.empty():
            queue.get_nowait()

        msg_resp = await client.post(
            f"/v1/sessions/{session_id}/events",
            json={
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "test"}],
                "agent_id": "ag_1",
            },
        )
        assert msg_resp.status_code in (200, 202), msg_resp.text

        if harness != "claude-native":
            # Wait until the background injection task drains its scripted
            # empty stream. If the runner still owns terminal-backed status,
            # this is where the invalid ``idle`` edge appears.
            await asyncio.wait_for(stream_finished.wait(), timeout=1.0)

            # Let the task resume after stream exhaustion and publish its
            # terminal lifecycle edge before the queue is drained below.
            await asyncio.sleep(0)

        # Drain BEFORE deleting: DELETE removes the queue. By this point both
        # the synchronous turn-start edge and the async turn-end edge have run.
        statuses: list[str] = []
        while not queue.empty():
            item = queue.get_nowait()
            if isinstance(item, dict) and item.get("type") == "session.status":
                statuses.append(item.get("status"))

        # DELETE cancels the background turn so it can't outlive the test.
        await client.delete(f"/v1/sessions/{session_id}")

    assert statuses == expected_statuses, (
        f"harness={harness}: expected runner turn lifecycle statuses "
        f"{expected_statuses!r}, got {statuses!r}. Claude-native must rely "
        f"fully on the PTY watcher, Codex-native must keep only the runner "
        f"running edge, and non-terminal harnesses must keep the full runner "
        f"lifecycle source."
    )


@pytest.mark.asyncio
async def test_events_interrupt_on_native_session_503_skips_cleanup_when_inject_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    POST ``/events`` interrupt returns 503 and skips cleanup when
    ``inject_interrupt`` can't reach tmux.

    Sister to the happy-path test. The contract is: if the runner
    can't actually deliver Escape (e.g. tmux pane gone, bridge dir
    not yet advertised), it must not (a) persist any
    ``[System: interrupted]`` marker (native never appends one — this
    also confirms the 503 early-return doesn't) and (b) publish
    ``session.status: idle`` — that would lie to the web UI ("we
    stopped it") while Claude keeps generating. The right signal is a
    503 so the caller can surface a failure (the spinner staying is
    correct).

    This was previously pinned by an AP-side test
    (``..._skips_idle_publish_on_runner_failure``); after the
    refactor the responsibility lives on the runner, so the
    invariant is pinned here.
    """
    from omnigent.runner.app import _session_event_queues_ref, _session_histories_ref
    from omnigent.spec.types import ExecutorSpec

    def _fake_inject(bridge_dir: Any, *, timeout_s: float) -> None:
        """Simulate the bridge-not-ready path."""
        del bridge_dir, timeout_s
        raise RuntimeError("tmux target is not advertised")

    monkeypatch.setattr(claude_native_bridge, "inject_interrupt", _fake_inject)

    native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the native spec for any agent_id."""
        del agent_id
        return native_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": "conv_native_int_fail", "agent_id": "ag_1"},
        )
        assert create_resp.status_code == 201, create_resp.text

        int_resp = await client.post(
            "/v1/sessions/conv_native_int_fail/events",
            json={"type": "interrupt"},
        )

        captured_history = list(_session_histories_ref.get("conv_native_int_fail", []))
        queue = _session_event_queues_ref.get("conv_native_int_fail")
        assert queue is not None, (
            "Session creation should have initialized the event queue; "
            "the failure path still needs the queue to exist so we can "
            "assert nothing was published into it."
        )
        queued_events: list[dict[str, Any]] = []
        while not queue.empty():
            item = queue.get_nowait()
            if isinstance(item, dict):
                queued_events.append(item)

    # 1) The route must surface the failure as 503 so the caller
    # treats the cancel as not delivered. 204 would let the caller
    # claim success and (e.g.) clear the spinner client-side.
    assert int_resp.status_code == 503, (
        f"Native interrupt with inject_interrupt failure must return "
        f"503; got {int_resp.status_code}: {int_resp.text}"
    )
    body = int_resp.json()
    assert body.get("error") == "claude_native_interrupt_failed", (
        f"503 body must carry the bridge-failure error code; got {body!r}"
    )

    # 2) No [System: interrupted] marker must be persisted. Native never
    # appends one; this additionally guards the failure path against a
    # reorder bug that appended the marker before the 503 early return.
    markers = [
        h
        for h in captured_history
        if h.get("type") == "message"
        and h.get("role") == "user"
        and any("interrupted" in (b.get("text") or "").lower() for b in h.get("content", []))
    ]
    assert markers == [], (
        f"No [System: interrupted] marker should be persisted on the "
        f"inject_interrupt failure path; got {markers!r}. "
        f"If non-empty, _append_cancellation_items fired before the "
        f"503 early return — likely a reordering bug in "
        f"_handle_claude_native_interrupt."
    )

    # 3) No session.status: idle on the failure path. Idle would
    # tell the web UI the cancel landed — the spinner clearing while
    # Claude keeps generating is exactly the misleading state we
    # need to avoid.
    status_idle = [
        e for e in queued_events if e.get("type") == "session.status" and e.get("status") == "idle"
    ]
    assert status_idle == [], (
        f"No session.status: idle should be enqueued when the Escape "
        f"injection failed; got {status_idle!r}. "
        f"If non-empty, _publish_event fired before the 503 early "
        f"return — same reordering concern as the marker."
    )


class _EventRecordingServerClient(NullServerClient):
    """Records Omnigent ``external_conversation_item`` POSTs for assertion.

    Subclasses :class:`NullServerClient` so all other runner→AP calls still
    succeed silently; captures the bodies so a test can assert that NO
    interrupt marker was persisted.
    """

    def __init__(self) -> None:
        self.posted_items: list[dict[str, Any]] = []

    async def post(self, url: str, **kwargs: Any) -> NullServerClient._Response:
        """Record ``external_conversation_item`` bodies."""
        del url
        body = kwargs.get("json")
        if isinstance(body, dict) and body.get("type") == "external_conversation_item":
            self.posted_items.append(body.get("data") or {})
        return self._Response()


class _RecordingCodexAppServerClient:
    """
    Test double for Codex app-server JSON-RPC controls.

    :param transport: Transport passed to
        :func:`omnigent.codex_native_app_server.client_for_transport`, e.g.
        ``"ws://127.0.0.1:1234"``.
    :param client_name: App-server client name, e.g.
        ``"omnigent-codex-native-runner"``.
    """

    def __init__(self, transport: str, client_name: str) -> None:
        self.transport = transport
        self.client_name = client_name
        self.connected = False
        self.closed = False
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.model_list_responses: list[dict[str, Any]] = []

    async def connect(self) -> None:
        """
        Mark the fake client connected.

        :returns: None.
        """
        self.connected = True

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """
        Capture a JSON-RPC request.

        :param method: JSON-RPC method, e.g. ``"turn/interrupt"``.
        :param params: JSON-RPC params, e.g.
            ``{"threadId": "thread_123", "turnId": "turn_123"}``.
        :returns: Empty successful JSON-RPC result.
        """
        self.requests.append((method, params))
        if method == "model/list" and self.model_list_responses:
            return self.model_list_responses.pop(0)
        return {"result": {}}

    async def close(self) -> None:
        """
        Mark the fake client closed.

        :returns: None.
        """
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event_payload,expected_params",
    [
        (
            {"type": "model_change", "model": "gpt-5.4"},
            {"threadId": "thread_codex", "model": "gpt-5.4"},
        ),
        (
            {"type": "effort_change", "effort": "xhigh"},
            {"threadId": "thread_codex", "effort": "xhigh"},
        ),
        (
            {"type": "plan_mode_change", "enabled": True},
            {
                "threadId": "thread_codex",
                "collaborationMode": {
                    "mode": "plan",
                    "settings": {
                        "model": "gpt-5.4",
                        "reasoning_effort": None,
                        "developer_instructions": None,
                    },
                },
            },
        ),
    ],
    ids=["model_change", "effort_change", "plan_mode_change"],
)
async def test_events_codex_native_settings_change_uses_thread_settings_update(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    event_payload: dict[str, Any],
    expected_params: dict[str, Any],
) -> None:
    """
    Codex-native model / effort updates call ``thread/settings/update``.

    The web UI persists model and effort through Omnigent's normal session
    PATCH path. The runner must translate the forwarded control event into
    Codex app-server's structured settings RPC, not type into the terminal or
    204 as a no-op. The update is a next-turn setting: it is valid even when
    no active turn id is recorded.
    """
    from omnigent import codex_native_app_server
    from omnigent.spec.types import ExecutorSpec

    conv_id = "conv_codex_native_settings"
    monkeypatch.setattr(codex_native_bridge, "_BRIDGE_ROOT", tmp_path / "codex-bridge")
    bridge_dir = codex_native_bridge.bridge_dir_for_bridge_id(conv_id)
    codex_native_bridge.write_bridge_state(
        bridge_dir,
        codex_native_bridge.CodexNativeBridgeState(
            session_id=conv_id,
            socket_path="ws://127.0.0.1:43210",
            thread_id="thread_codex",
            codex_home=str(tmp_path / "codex-home"),
            active_turn_id=None,
        ),
    )

    fake_client = _RecordingCodexAppServerClient(
        transport="ws://127.0.0.1:43210",
        client_name="omnigent-codex-native-runner",
    )

    def _fake_client_for_transport(
        transport: str,
        *,
        client_name: str = "omnigent",
    ) -> _RecordingCodexAppServerClient:
        """
        Return the fake Codex app-server client for the recorded bridge state.

        :param transport: App-server transport from bridge state, e.g.
            ``"ws://127.0.0.1:43210"``.
        :param client_name: Client name supplied by the runner, e.g.
            ``"omnigent-codex-native-runner"``.
        :returns: Fake client that records JSON-RPC calls.
        """
        assert transport == fake_client.transport
        assert client_name == fake_client.client_name
        return fake_client

    monkeypatch.setattr(
        codex_native_app_server,
        "client_for_transport",
        _fake_client_for_transport,
    )

    codex_native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(
            type="omnigent",
            config={"harness": "codex-native", "model": "gpt-5.4"},
        ),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the codex-native spec for any agent_id."""
        del agent_id, session_id
        return codex_native_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": conv_id, "agent_id": "ag_1"},
        )
        assert create_resp.status_code == 201, create_resp.text

        resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json=event_payload,
        )

    assert resp.status_code == 204, (
        f"codex-native {event_payload['type']} must return 204; "
        f"got {resp.status_code}: {resp.text}"
    )
    assert fake_client.connected
    assert fake_client.closed
    assert fake_client.requests == [
        ("thread/settings/update", expected_params),
    ], (
        f"codex-native {event_payload['type']} must call thread/settings/update "
        f"with next-turn settings; got {fake_client.requests!r}."
    )


@pytest.mark.asyncio
async def test_codex_native_model_options_returns_503_until_bridge_state_exists(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Runner model-options endpoint is retryable before Codex bridge startup.

    The AP server caches successful runner responses. A codex-native runner
    must therefore not return ``200 {"models": []}`` while the Codex terminal
    is still creating its app-server bridge; that would permanently hide the
    Web UI model picker for the session.
    """
    from omnigent import codex_native_app_server

    conv_id = "conv_codex_native_model_options_not_ready"
    monkeypatch.setattr(codex_native_bridge, "_BRIDGE_ROOT", tmp_path / "codex-bridge")

    def _client_for_transport(
        transport: str,
        *,
        client_name: str = "omnigent",
    ) -> _RecordingCodexAppServerClient:
        """
        Fail the test if the endpoint reaches Codex without bridge state.

        :param transport: App-server transport from bridge state, e.g.
            ``"ws://127.0.0.1:43210"``.
        :param client_name: Client name supplied by the runner, e.g.
            ``"omnigent-codex-native-runner"``.
        :returns: Never returns; raises if called.
        """
        raise AssertionError(
            f"client_for_transport must not be called before bridge state exists: "
            f"{transport=} {client_name=}"
        )

    monkeypatch.setattr(
        codex_native_app_server,
        "client_for_transport",
        _client_for_transport,
    )

    codex_native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "codex-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the codex-native spec for any agent_id."""
        del agent_id, session_id
        return codex_native_spec

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": conv_id, "agent_id": "ag_1"},
        )
        assert create_resp.status_code == 201, create_resp.text

        resp = await client.get(f"/v1/sessions/{conv_id}/codex-model-options")

    # A retryable 503 keeps the AP server from caching an empty model list;
    # returning 200 here would recreate the missing-picker regression.
    assert resp.status_code == 503, resp.text
    assert resp.json() == {
        "error": "codex_native_model_options_failed",
        "detail": "Codex-native model options are not ready yet.",
    }


@pytest.mark.asyncio
async def test_codex_native_model_options_query_model_list(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Runner model-options endpoint queries Codex ``model/list``.

    The Web UI must not carry its own Codex model / effort catalog. The
    runner is the process that can reach the session's Codex app-server, so
    this endpoint should ask Codex for models and return those model objects
    unchanged for the AP snapshot.
    """
    from omnigent import codex_native_app_server
    from omnigent.spec.types import ExecutorSpec

    conv_id = "conv_codex_native_model_options"
    monkeypatch.setattr(codex_native_bridge, "_BRIDGE_ROOT", tmp_path / "codex-bridge")
    bridge_dir = codex_native_bridge.bridge_dir_for_bridge_id(conv_id)
    codex_native_bridge.write_bridge_state(
        bridge_dir,
        codex_native_bridge.CodexNativeBridgeState(
            session_id=conv_id,
            socket_path="ws://127.0.0.1:43210",
            thread_id="thread_codex",
            codex_home=str(tmp_path / "codex-home"),
            active_turn_id=None,
        ),
    )

    fake_client = _RecordingCodexAppServerClient(
        transport="ws://127.0.0.1:43210",
        client_name="omnigent-codex-native-runner",
    )
    fake_client.model_list_responses = [
        {
            "result": {
                "data": [
                    {
                        "id": "gpt-5.5",
                        "model": "databricks-gpt-5-5",
                        "displayName": "GPT-5.5",
                        "defaultReasoningEffort": "high",
                        "supportedReasoningEfforts": [
                            {"reasoningEffort": "low", "description": "Low"},
                            {"reasoningEffort": "medium", "description": "Medium"},
                        ],
                        "isDefault": True,
                    }
                ],
                "nextCursor": "next-page",
            }
        },
        {
            "result": {
                "data": [
                    {
                        "id": "gpt-5.4-mini",
                        "model": "databricks-gpt-5-4-mini",
                        "displayName": "GPT-5.4 mini",
                        "defaultReasoningEffort": "medium",
                        "supportedReasoningEfforts": [
                            {"reasoningEffort": "minimal", "description": "Minimal"}
                        ],
                        "isDefault": False,
                    }
                ],
                "nextCursor": None,
            }
        },
    ]

    def _fake_client_for_transport(
        transport: str,
        *,
        client_name: str = "omnigent",
    ) -> _RecordingCodexAppServerClient:
        """
        Return the fake Codex app-server client for the recorded bridge state.

        :param transport: App-server transport from bridge state, e.g.
            ``"ws://127.0.0.1:43210"``.
        :param client_name: Client name supplied by the runner, e.g.
            ``"omnigent-codex-native-runner"``.
        :returns: Fake client scripted with ``model/list`` pages.
        """
        assert transport == fake_client.transport
        assert client_name == fake_client.client_name
        return fake_client

    monkeypatch.setattr(
        codex_native_app_server,
        "client_for_transport",
        _fake_client_for_transport,
    )

    codex_native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "codex-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the codex-native spec for any agent_id."""
        del agent_id, session_id
        return codex_native_spec

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": conv_id, "agent_id": "ag_1"},
        )
        assert create_resp.status_code == 201, create_resp.text

        resp = await client.get(f"/v1/sessions/{conv_id}/codex-model-options")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "models": [
            {
                "id": "gpt-5.5",
                "model": "databricks-gpt-5-5",
                "displayName": "GPT-5.5",
                "defaultReasoningEffort": "high",
                "supportedReasoningEfforts": [
                    {"reasoningEffort": "low", "description": "Low"},
                    {"reasoningEffort": "medium", "description": "Medium"},
                ],
                "isDefault": True,
            },
            {
                "id": "gpt-5.4-mini",
                "model": "databricks-gpt-5-4-mini",
                "displayName": "GPT-5.4 mini",
                "defaultReasoningEffort": "medium",
                "supportedReasoningEfforts": [
                    {"reasoningEffort": "minimal", "description": "Minimal"}
                ],
                "isDefault": False,
            },
        ]
    }
    assert fake_client.requests == [
        ("model/list", {"includeHidden": False}),
        ("model/list", {"includeHidden": False, "cursor": "next-page"}),
    ]
    assert fake_client.connected
    assert fake_client.closed


@pytest.mark.asyncio
async def test_events_codex_native_plan_mode_requires_loaded_bridge(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Codex-native Plan-mode updates fail when no Codex bridge is loaded.

    The AP server treats a 2xx runner response as proof that the UI can show
    Plan mode. Returning 204 when no bridge state exists would therefore
    persist a false Plan indicator even though Codex app-server never received
    ``thread/settings/update``.
    """
    conv_id = "conv_codex_native_plan_no_bridge"
    monkeypatch.setattr(codex_native_bridge, "_BRIDGE_ROOT", tmp_path / "codex-bridge")

    codex_native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(
            type="omnigent",
            config={"harness": "codex-native", "model": "gpt-5.4"},
        ),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """
        Return the codex-native spec for any agent id.

        :param agent_id: Agent identifier, e.g. ``"ag_1"``.
        :param session_id: Session identifier, e.g. ``"conv_abc123"``.
        :returns: Codex-native agent spec.
        """
        del agent_id, session_id
        return codex_native_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": conv_id, "agent_id": "ag_1"},
        )
        assert create_resp.status_code == 201, create_resp.text

        resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={"type": "plan_mode_change", "enabled": True},
        )

    assert resp.status_code == 503, resp.text
    assert "loaded Codex bridge" in resp.text


@pytest.mark.asyncio
async def test_events_interrupt_on_codex_native_uses_turn_interrupt_without_marker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    POST ``/events`` interrupt on a codex-native session calls
    Codex app-server ``turn/interrupt``.

    Codex's TUI interrupt key is only a UI shortcut for the structured
    app-server call. The runner/web path must use the app-server protocol
    directly so Codex validates the active turn id and returns only after the
    abort is accepted. Codex records the interrupt as a turn-status edge, not
    as a message, so the runner still must not synthesize a
    ``[System: interrupted]`` bubble.

    Pins:
    1. ``turn/interrupt`` is sent with the recorded thread/turn ids.
    2. NO ``[System: interrupted]`` marker is persisted to AP.
    3. The session is NOT added to ``_interrupted_sessions``; no marker in
       ``_session_histories``.
    """
    from omnigent import codex_native_app_server
    from omnigent.runner.app import _session_histories_ref
    from omnigent.spec.types import ExecutorSpec

    conv_id = "conv_codex_native_int"
    monkeypatch.setattr(codex_native_bridge, "_BRIDGE_ROOT", tmp_path / "codex-bridge")
    bridge_dir = codex_native_bridge.bridge_dir_for_bridge_id(conv_id)
    codex_native_bridge.write_bridge_state(
        bridge_dir,
        codex_native_bridge.CodexNativeBridgeState(
            session_id=conv_id,
            socket_path="ws://127.0.0.1:43210",
            thread_id="thread_codex",
            codex_home=str(tmp_path / "codex-home"),
            active_turn_id="turn_codex",
        ),
    )

    fake_client = _RecordingCodexAppServerClient(
        transport="ws://127.0.0.1:43210",
        client_name="omnigent-codex-native-runner",
    )

    def _fake_client_for_transport(
        transport: str,
        *,
        client_name: str = "omnigent",
    ) -> _RecordingCodexAppServerClient:
        """
        Return the fake Codex app-server client for the recorded bridge state.

        :param transport: App-server transport from bridge state, e.g.
            ``"ws://127.0.0.1:43210"``.
        :param client_name: Client name supplied by the runner, e.g.
            ``"omnigent-codex-native-runner"``.
        :returns: Fake client that records JSON-RPC calls.
        """
        assert transport == fake_client.transport
        assert client_name == fake_client.client_name
        return fake_client

    monkeypatch.setattr(
        codex_native_app_server,
        "client_for_transport",
        _fake_client_for_transport,
    )

    codex_native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "codex-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the codex-native spec for any agent_id."""
        del agent_id, session_id
        return codex_native_spec

    server_client = _EventRecordingServerClient()
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server_client,  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        # Seeds _session_spec_cache so the dispatch detects "codex-native".
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": conv_id, "agent_id": "ag_1"},
        )
        assert create_resp.status_code == 201, create_resp.text

        int_resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={"type": "interrupt"},
        )

        captured_history = list(_session_histories_ref.get(conv_id, []))
        flagged = conv_id in app.state.interrupted_sessions

    assert int_resp.status_code == 204, (
        f"codex-native interrupt must return 204; got {int_resp.status_code}: {int_resp.text}"
    )

    # 1) The runner reached Codex app-server's structured interrupt path. If
    # this is empty, the handler regressed to a terminal-only or no-op cancel.
    assert fake_client.connected
    assert fake_client.closed
    assert fake_client.requests == [
        (
            "turn/interrupt",
            {
                "threadId": "thread_codex",
                "turnId": "turn_codex",
            },
        )
    ], (
        f"codex-native interrupt must call turn/interrupt with the active "
        f"thread/turn ids; got {fake_client.requests!r}."
    )

    # 2) NO marker persisted — a synthesized [System: interrupted] would diverge
    # the web UI from Codex's own session (the mismatch this revert removes).
    marker_texts = [
        b.get("text")
        for data in server_client.posted_items
        for b in (data.get("item_data") or {}).get("content", [])
        if isinstance(b, dict)
    ]
    assert not any("interrupted" in (t or "").lower() for t in marker_texts), (
        f"codex-native interrupt must NOT persist an interrupted marker; "
        f"posted item texts were {marker_texts!r}."
    )

    # 3) Not flagged, and nothing leaks into the runner's in-memory history.
    assert not flagged, f"codex-native session {conv_id!r} must not be flagged interrupted."
    assert all(
        not (
            h.get("role") == "user"
            and any("interrupted" in (b.get("text") or "").lower() for b in h.get("content", []))
        )
        for h in captured_history
    ), f"no interrupt marker should enter _session_histories; got {captured_history!r}"


@pytest.mark.asyncio
async def test_events_stop_session_on_codex_native_uses_turn_interrupt_without_marker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    POST ``/events`` ``stop_session`` on codex-native interrupts the active turn.

    Regression guard for the cancel-floor work: ``stop_session`` only
    special-cased claude-native, so codex-native fell into
    ``_cancel_inprocess_turn``, which flags the session interrupted and (on the
    next turn or a live-task race) synthesizes the ``[System: interrupted]``
    marker Codex never emits. codex-native must reach the same app-server
    ``turn/interrupt`` path as the interrupt branch.

    Pins (sister to ``...interrupt_on_codex_native...``):
    1. ``turn/interrupt`` is sent with the recorded thread/turn ids.
    2. NO ``[System: interrupted]`` marker is persisted to AP.
    3. The session is NOT added to ``_interrupted_sessions``; no marker leaks
       into ``_session_histories``.
    """
    from omnigent import codex_native_app_server
    from omnigent.runner.app import _session_histories_ref
    from omnigent.spec.types import ExecutorSpec

    conv_id = "conv_codex_native_stop"
    monkeypatch.setattr(codex_native_bridge, "_BRIDGE_ROOT", tmp_path / "codex-bridge")
    bridge_dir = codex_native_bridge.bridge_dir_for_bridge_id(conv_id)
    codex_native_bridge.write_bridge_state(
        bridge_dir,
        codex_native_bridge.CodexNativeBridgeState(
            session_id=conv_id,
            socket_path="ws://127.0.0.1:43211",
            thread_id="thread_codex_stop",
            codex_home=str(tmp_path / "codex-home"),
            active_turn_id="turn_codex_stop",
        ),
    )

    fake_client = _RecordingCodexAppServerClient(
        transport="ws://127.0.0.1:43211",
        client_name="omnigent-codex-native-runner",
    )

    def _fake_client_for_transport(
        transport: str,
        *,
        client_name: str = "omnigent",
    ) -> _RecordingCodexAppServerClient:
        """
        Return the fake Codex app-server client for the stop-session path.

        :param transport: App-server transport from bridge state, e.g.
            ``"ws://127.0.0.1:43211"``.
        :param client_name: Client name supplied by the runner, e.g.
            ``"omnigent-codex-native-runner"``.
        :returns: Fake client that records JSON-RPC calls.
        """
        assert transport == fake_client.transport
        assert client_name == fake_client.client_name
        return fake_client

    monkeypatch.setattr(
        codex_native_app_server,
        "client_for_transport",
        _fake_client_for_transport,
    )

    codex_native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "codex-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the codex-native spec for any agent_id."""
        del agent_id, session_id
        return codex_native_spec

    server_client = _EventRecordingServerClient()
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server_client,  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": conv_id, "agent_id": "ag_1"},
        )
        assert create_resp.status_code == 201, create_resp.text

        stop_resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={"type": "stop_session"},
        )

        captured_history = list(_session_histories_ref.get(conv_id, []))
        flagged = conv_id in app.state.interrupted_sessions

    assert stop_resp.status_code == 204, (
        f"codex-native stop_session must return 204; got {stop_resp.status_code}: {stop_resp.text}"
    )

    # 1) The runner reached Codex app-server's structured interrupt path. If
    # this is empty, stop_session regressed to the in-process cancel floor or
    # the old terminal-key path.
    assert fake_client.connected
    assert fake_client.closed
    assert fake_client.requests == [
        (
            "turn/interrupt",
            {
                "threadId": "thread_codex_stop",
                "turnId": "turn_codex_stop",
            },
        )
    ], (
        f"codex-native stop_session must call turn/interrupt with the active "
        f"thread/turn ids; got {fake_client.requests!r}."
    )

    # 2) NO marker persisted — the in-process floor would have synthesized one.
    marker_texts = [
        b.get("text")
        for data in server_client.posted_items
        for b in (data.get("item_data") or {}).get("content", [])
        if isinstance(b, dict)
    ]
    assert not any("interrupted" in (t or "").lower() for t in marker_texts), (
        f"codex-native stop_session must NOT persist an interrupted marker; "
        f"posted item texts were {marker_texts!r}."
    )

    # 3) Not flagged (the in-process floor's _interrupted_sessions.add never ran),
    # and nothing leaks into the runner's in-memory history.
    assert not flagged, (
        f"codex-native session {conv_id!r} must not be flagged interrupted — a "
        f"stale flag would taint the next turn with a bogus marker."
    )
    assert all(
        not (
            h.get("role") == "user"
            and any("interrupted" in (b.get("text") or "").lower() for b in h.get("content", []))
        )
        for h in captured_history
    ), f"no interrupt marker should enter _session_histories; got {captured_history!r}"


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", ["interrupt", "stop_session"])
async def test_events_interrupt_and_stop_on_pi_native_enqueue_bridge_interrupt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    event_type: str,
) -> None:
    """
    POST ``/events`` interrupt / stop_session on a pi-native session queues an
    interrupt payload to the Pi extension inbox.

    A pi-native turn runs inside the resident Pi TUI process; the runner's
    harness task only enqueues the user message and returns, so the in-process
    cancel floor has nothing to cancel. Both the ``interrupt`` and
    ``stop_session`` dispatch must route to ``_handle_pi_native_interrupt``,
    which drops an ``interrupt`` payload into the bridge inbox for the extension
    to consume via ``ExtensionContext.abort()``.

    Regression guard: both branches originally enumerated only claude-native
    and codex-native, so pi-native silently fell through to the no-op
    ``_cancel_inprocess_turn`` floor — clicking Stop on a Pi turn did nothing.

    Pins:
    1. 204 returned.
    2. An ``interrupt_*`` payload is written to the session's bridge inbox.
    3. NO ``[System: interrupted]`` marker is persisted (the floor never ran).
    """
    import omnigent.pi_native_bridge as pi_native_bridge
    from omnigent.runner.app import _session_histories_ref
    from omnigent.spec.types import ExecutorSpec

    conv_id = f"conv_pi_native_{event_type}"
    monkeypatch.setattr(pi_native_bridge, "_BRIDGE_ROOT", tmp_path / "pi-bridge")

    pi_native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "pi-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the pi-native spec for any agent_id."""
        del agent_id, session_id
        return pi_native_spec

    server_client = _EventRecordingServerClient()
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server_client,  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        # Seeds _session_spec_cache so the dispatch detects "pi-native".
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": conv_id, "agent_id": "ag_1"},
        )
        assert create_resp.status_code == 201, create_resp.text

        resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={"type": event_type},
        )

        captured_history = list(_session_histories_ref.get(conv_id, []))
        flagged = conv_id in app.state.interrupted_sessions

    assert resp.status_code == 204, (
        f"pi-native {event_type} must return 204; got {resp.status_code}: {resp.text}"
    )

    # 1) The request reached the bridge inbox (the extension's abort channel). If
    # empty, the dispatch fell through to the no-op in-process cancel floor
    # instead of _handle_pi_native_interrupt.
    inbox = pi_native_bridge.bridge_dir_for_session_id(conv_id) / "inbox"
    queued = sorted(p.name for p in inbox.glob("*.json")) if inbox.exists() else []
    assert any("interrupt_" in name for name in queued), (
        f"pi-native {event_type} must enqueue an interrupt payload to the bridge "
        f"inbox; inbox contained {queued!r}."
    )

    # 2) No synthesized marker — pi-native never goes through the in-process floor.
    marker_texts = [
        b.get("text")
        for data in server_client.posted_items
        for b in (data.get("item_data") or {}).get("content", [])
        if isinstance(b, dict)
    ]
    assert not any("interrupted" in (t or "").lower() for t in marker_texts), (
        f"pi-native {event_type} must NOT persist an interrupted marker; got {marker_texts!r}."
    )

    # 3) Not flagged, and nothing leaks into the runner's in-memory history.
    assert not flagged, f"pi-native session {conv_id!r} must not be flagged interrupted."
    assert all(
        not (
            h.get("role") == "user"
            and any("interrupted" in (b.get("text") or "").lower() for b in h.get("content", []))
        )
        for h in captured_history
    ), f"no interrupt marker should enter _session_histories; got {captured_history!r}"


def test_interrupted_sessions_isolated_per_app_instance() -> None:
    """
    Each ``create_runner_app()`` gets its own ``_interrupted_sessions`` set.

    Regression guard: when ``_interrupted_sessions`` was a module-global,
    interrupt flags leaked between distinct app instances in the same
    process — app1 flagging a conv made app2 append a bogus
    ``[System: interrupted]`` marker on a normal turn for the same conv id.
    Keeping the set closure-local (exposed on ``app.state`` only for test
    inspection) prevents that.
    """
    app1 = create_runner_app(server_client=NullServerClient())  # type: ignore[arg-type]
    app2 = create_runner_app(server_client=NullServerClient())  # type: ignore[arg-type]

    # Distinct objects: a shared module-global would make these identical, so
    # a flag added to one app would be visible from the other.
    assert app1.state.interrupted_sessions is not app2.state.interrupted_sessions, (
        "Each app instance must own its _interrupted_sessions set; if they are "
        "the same object, the set is module-global again and flags leak across apps."
    )

    app1.state.interrupted_sessions.add("conv_x")
    assert "conv_x" not in app2.state.interrupted_sessions, (
        "app2 must not observe app1's interrupt flag. If it does, "
        "_interrupted_sessions is shared process-global state and a stale flag "
        "would fire a bogus [System: interrupted] marker on app2's next turn."
    )


@pytest.mark.asyncio
async def test_events_stop_session_on_native_kills_tmux_and_publishes_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    POST ``/events`` ``{"type": "stop_session"}`` on a claude-native
    session kills the tmux session and clears the spinner.

    "Stop session" is the web UI affordance for terminating a
    claude-native session without re-attaching to tmux. Unlike
    ``interrupt`` (a single Escape that cancels the current response
    but leaves the session alive), it must:

    1. Call ``kill_session`` with the bridge dir derived from the
       conversation id and the snappy 1.0s timeout — this is what
       actually ends the ``claude`` process.
    2. Enqueue exactly one ``session.status: idle`` event so the web
       UI's "Working…" spinner clears immediately (Claude's ``Stop``
       hook never fires on a hard kill).
    3. NOT append a ``[System: interrupted]`` marker — the session is
       being torn down, not interrupted mid-turn. A stray marker would
       be the interrupt handler leaking into the stop path.
    """
    from omnigent.runner.app import _session_event_queues_ref, _session_histories_ref
    from omnigent.spec.types import ExecutorSpec

    captured_kill: list[Any] = []

    def _fake_kill(bridge_dir: Any, *, timeout_s: float) -> None:
        """Record the call and return without touching tmux."""
        captured_kill.append((bridge_dir, timeout_s))

    monkeypatch.setattr(claude_native_bridge, "kill_session", _fake_kill)

    native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the native spec for any agent_id."""
        del agent_id
        return native_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": "conv_native_stop", "agent_id": "ag_1"},
        )
        assert create_resp.status_code == 201, create_resp.text

        stop_resp = await client.post(
            "/v1/sessions/conv_native_stop/events",
            json={"type": "stop_session"},
        )

        captured_history = list(_session_histories_ref.get("conv_native_stop", []))
        queue = _session_event_queues_ref.get("conv_native_stop")
        assert queue is not None, (
            "Session creation should have initialized the event queue "
            "for ``conv_native_stop``; without it ``_publish_event`` had "
            "nowhere to land its idle event."
        )
        queued_events: list[dict[str, Any]] = []
        while not queue.empty():
            item = queue.get_nowait()
            if isinstance(item, dict):
                queued_events.append(item)

    # 1) 204 + exactly one kill_session call on the conversation's
    # bridge dir. 0 = the dispatch fell through to the generic
    # forward-to-harness path (which 404s for native — silent
    # regression); 2+ = the handler ran twice.
    assert stop_resp.status_code == 204, (
        f"Native stop_session must return 204 from /events; "
        f"got {stop_resp.status_code}: {stop_resp.text}"
    )
    assert len(captured_kill) == 1, (
        f"Expected one kill_session call, got {len(captured_kill)}. "
        f"If 0, the dispatch in /events did not route to the native "
        f"stop handler — possibly _session_harness_name returned the "
        f"wrong canonical name."
    )
    bridge_dir, timeout_s = captured_kill[0]
    assert bridge_dir == bridge_dir_for_conversation_id("conv_native_stop")
    # 1.0s short timeout: the UI stop must feel snappy. The helper's
    # 30s default would hang the user's click on a missing tmux.json.
    assert timeout_s == 1.0

    # 2) session.status: idle enqueued exactly once so the spinner
    # clears. 0 = _publish_event was skipped; 2+ = double-publish.
    status_idle = [
        e for e in queued_events if e.get("type") == "session.status" and e.get("status") == "idle"
    ]
    assert len(status_idle) == 1, (
        f"Expected exactly one session.status: idle event after a "
        f"native stop, got {len(status_idle)}. Full queue: {queued_events!r}."
    )

    # 3) No [System: interrupted] marker — stop is a teardown, not a
    # mid-turn interrupt. A marker here means the interrupt handler's
    # _append_cancellation_items leaked into the stop path.
    markers = [
        h
        for h in captured_history
        if h.get("type") == "message"
        and h.get("role") == "user"
        and any("interrupted" in (b.get("text") or "").lower() for b in h.get("content", []))
    ]
    assert markers == [], (
        f"stop_session must not append a [System: interrupted] marker; "
        f"got {markers!r}. If non-empty, the stop handler is reusing the "
        f"interrupt cleanup path."
    )


@pytest.mark.asyncio
async def test_stop_session_on_native_subagent_reclaims_work_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Hard-stopping a claude-native SUB-AGENT worker reclaims its work entry.

    When the stopped session is a tracked sub-agent, ``_handle_claude_native_stop``
    must mark the work entry ``cancelled`` and deliver a terminal payload to the
    parent's inbox — so the orchestrator (via ``sys_cancel_task`` → ``stop_session``)
    learns the worker is gone instead of waiting on the wrapper's reconnect loop.
    Pre-fix the kill happened but the entry was never reclaimed (the parent could
    hang thinking the worker was still running).
    """
    from omnigent.runner import app as runner_app
    from omnigent.spec.types import ExecutorSpec

    parent_id = "conv_parent_stop_reclaim"
    worker_id = "conv_worker_stop_reclaim"
    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    monkeypatch.setattr(claude_native_bridge, "kill_session", lambda *a, **k: None)

    native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return native_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    runner_app._session_inboxes_ref[parent_id] = session_inbox
    runner_app.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=worker_id,
        agent="claude_code",
        title="task",
    )

    try:
        async with _runner_client(app) as client:
            create_resp = await client.post(
                "/v1/sessions",
                json={"session_id": worker_id, "agent_id": "ag_1"},
            )
            assert create_resp.status_code == 201, create_resp.text
            stop_resp = await client.post(
                f"/v1/sessions/{worker_id}/events",
                json={"type": "stop_session"},
            )
            assert stop_resp.status_code == 204, stop_resp.text
    finally:
        runner_app.unregister_subagent_work(worker_id)
        runner_app._session_inboxes_ref.pop(parent_id, None)

    # The killed worker's entry was reclaimed: a single cancelled completion
    # landed in the parent's inbox. If 0, the stop path killed the pane but
    # left the parent thinking the worker was still running (the bug).
    assert session_inbox.qsize() == 1, (
        f"Expected one cancelled completion in the parent inbox after stopping "
        f"the worker, got {session_inbox.qsize()}."
    )
    delivered = session_inbox.get_nowait()
    assert delivered["status"] == "cancelled"
    assert delivered["task_id"] == worker_id


@pytest.mark.asyncio
async def test_stop_session_on_native_subagent_without_parent_inbox_returns_204(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Hard-stopping a tracked native sub-agent succeeds after the kill lands.

    ``stop_session`` is user-initiated stop orchestration, not the native
    terminal-status ACK path. Once the pane is killed, the runner must return
    204 so Omnigent can finish host-runner teardown and write the deliberate-stop
    label even if parent delivery cannot be confirmed.
    """
    from omnigent.runner import app as runner_app
    from omnigent.spec.types import ExecutorSpec

    parent_id = "conv_parent_stop_missing_inbox"
    worker_id = "conv_worker_stop_missing_inbox"

    monkeypatch.setattr(claude_native_bridge, "kill_session", lambda *a, **k: None)

    native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """
        Resolve every test session to a claude-native spec.

        :param agent_id: Agent id requested by the runner.
        :param session_id: Optional session id being spawned.
        :returns: Native executor spec for the test.
        """
        del agent_id, session_id
        return native_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    runner_app.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=worker_id,
        agent="claude_code",
        title="task",
    )

    try:
        async with _runner_client(app) as client:
            create_resp = await client.post(
                "/v1/sessions",
                json={"session_id": worker_id, "agent_id": "ag_1"},
            )
            assert create_resp.status_code == 201, create_resp.text
            stop_resp = await client.post(
                f"/v1/sessions/{worker_id}/events",
                json={"type": "stop_session"},
            )
        entry = runner_app.get_subagent_work(worker_id)
    finally:
        runner_app.unregister_subagent_work(worker_id)

    assert stop_resp.status_code == 204, stop_resp.text
    assert entry is not None
    # The worker was marked cancelled, but delivery is still unconfirmed. The
    # external_session_status path remains responsible for enforcing delivery
    # ACK failures; explicit stop must not report a failed kill after success.
    assert entry.status == "cancelled"
    assert entry.delivered is False


@pytest.mark.asyncio
async def test_events_stop_session_on_native_returns_503_when_kill_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    POST ``/events`` stop_session returns 503 when ``kill_session``
    can't reach tmux, and publishes no idle.

    Sister to the happy-path test. If the runner can't deliver the
    kill (tmux pane gone, bridge dir not yet advertised) it must
    surface a 503 rather than lie to the web UI with a 204 + idle
    that says "stopped" while the session may still be alive.
    """
    from omnigent.runner.app import _session_event_queues_ref
    from omnigent.spec.types import ExecutorSpec

    def _fake_kill(bridge_dir: Any, *, timeout_s: float) -> None:
        """Simulate the bridge-not-ready path."""
        del bridge_dir, timeout_s
        raise RuntimeError("tmux target is not advertised")

    monkeypatch.setattr(claude_native_bridge, "kill_session", _fake_kill)

    native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the native spec for any agent_id."""
        del agent_id
        return native_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": "conv_native_stop_fail", "agent_id": "ag_1"},
        )
        assert create_resp.status_code == 201, create_resp.text

        stop_resp = await client.post(
            "/v1/sessions/conv_native_stop_fail/events",
            json={"type": "stop_session"},
        )

        queue = _session_event_queues_ref.get("conv_native_stop_fail")
        assert queue is not None
        queued_events: list[dict[str, Any]] = []
        while not queue.empty():
            item = queue.get_nowait()
            if isinstance(item, dict):
                queued_events.append(item)

    assert stop_resp.status_code == 503, (
        f"Native stop_session with kill failure must return 503; "
        f"got {stop_resp.status_code}: {stop_resp.text}"
    )
    body = stop_resp.json()
    assert body.get("error") == "claude_native_stop_failed", (
        f"503 body must carry the stop-failure error code; got {body!r}"
    )
    # No idle on the failure path — clearing the spinner would tell the
    # UI the session stopped when the kill didn't actually land.
    status_idle = [
        e for e in queued_events if e.get("type") == "session.status" and e.get("status") == "idle"
    ]
    assert status_idle == [], (
        f"No session.status: idle should be enqueued when kill_session "
        f"failed; got {status_idle!r}."
    )


@pytest.mark.asyncio
async def test_events_stop_session_on_non_native_session_is_204_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Non-native sessions accept stop_session and 204 without killing tmux.

    In-process harnesses have no external tmux process for the runner to
    kill: stop cancels the in-flight turn via the cancel floor, or — with
    no turn in flight, as here — is a clean 204 no-op. The Omnigent server is
    harness-agnostic and forwards stop_session for any session, so the
    runner must accept it and 204 — never reach ``kill_session``.
    """
    from omnigent.spec.types import ExecutorSpec

    def _fake_kill(bridge_dir: Any, *, timeout_s: float) -> None:
        """Fail the test if a non-native session reaches the killer."""
        del bridge_dir, timeout_s
        raise AssertionError(
            "kill_session must never be called for non-native sessions — "
            "stop_session is a no-op for in-process harnesses."
        )

    monkeypatch.setattr(claude_native_bridge, "kill_session", _fake_kill)

    # Default harness (in-process LLM loop), NOT claude-native.
    default_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the default spec for any agent_id."""
        del agent_id
        return default_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": "conv_default_stop", "agent_id": "ag_1"},
        )
        assert create_resp.status_code == 201, create_resp.text

        resp = await client.post(
            "/v1/sessions/conv_default_stop/events",
            json={"type": "stop_session"},
        )

    # 204 = dispatch saw a non-native harness and short-circuited
    # before any kill. Anything else means the event leaked into a
    # code path it shouldn't reach.
    assert resp.status_code == 204, (
        f"Non-native stop_session must return 204 no-op; got {resp.status_code}: {resp.text}"
    )


@pytest.mark.asyncio
async def test_events_stop_session_closes_terminal_and_publishes_deleted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Native stop tears the session's terminal resource down.

    A host-spawned (web-UI-created) claude-native session has no CLI
    wrapper watching the pane, so after ``kill_session`` ends ``claude``
    nothing else removes the terminal resource — the web UI keeps showing
    a live terminal for the stopped session (the user-reported bug). The
    stop handler must therefore close each of the session's terminals and
    publish ``session.resource.deleted`` so connected clients drop them.
    """
    from omnigent.runner.app import _session_event_queues_ref
    from omnigent.spec.types import ExecutorSpec
    from tests.runner.helpers import make_test_terminal_instance

    def _fake_kill(bridge_dir: Any, *, timeout_s: float) -> None:
        """Record nothing; the stub terminal needs no real tmux kill."""
        del bridge_dir, timeout_s

    monkeypatch.setattr(claude_native_bridge, "kill_session", _fake_kill)

    # Seed the runner's terminal registry with the session's live
    # ``claude:main`` terminal, mirroring what the host-spawned
    # auto-create path leaves behind. Private-attr seed matches the
    # existing resource-registry test convention (no real tmux).
    conv_id = "conv_native_stop_term"
    terminal_registry = TerminalRegistry(
        conversation_link_base_url="http://127.0.0.1:8000",
    )
    instance = make_test_terminal_instance("claude", "main", tmp_path)
    terminal_registry._by_conversation.setdefault(conv_id, {})[("claude", "main")] = instance

    native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the native spec for any agent_id."""
        del agent_id, session_id
        return native_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
        terminal_registry=terminal_registry,
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": conv_id, "agent_id": "ag_1"},
        )
        assert create_resp.status_code == 201, create_resp.text
        # Precondition: the terminal is live before the stop, so a later
        # absence proves the stop closed it (not that it was never there).
        assert terminal_registry.get(conv_id, "claude", "main") is not None

        stop_resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={"type": "stop_session"},
        )

        queue = _session_event_queues_ref.get(conv_id)
        queued_events: list[dict[str, Any]] = []
        while queue is not None and not queue.empty():
            item = queue.get_nowait()
            if isinstance(item, dict):
                queued_events.append(item)

    assert stop_resp.status_code == 204, stop_resp.text

    # The terminal is gone from the registry → the resource list the web
    # UI reads no longer shows a live terminal. Still present = the stop
    # handler skipped teardown (the bug this guards against).
    assert terminal_registry.get(conv_id, "claude", "main") is None, (
        "stop_session must close the session's terminal; it is still "
        "registered, so the web UI would keep showing a live terminal."
    )

    # Exactly one session.resource.deleted for the claude terminal so
    # connected clients drop it live (the server relay also persists it).
    # 0 = teardown didn't publish (UI never updates); 2+ = double-publish.
    deleted = [e for e in queued_events if e.get("type") == "session.resource.deleted"]
    assert deleted == [
        {
            "type": "session.resource.deleted",
            "resource_id": "terminal_claude_main",
            "resource_type": "terminal",
            "session_id": conv_id,
        }
    ], f"expected one terminal session.resource.deleted event, got {deleted!r}"


@pytest.mark.asyncio
async def test_required_terminal_exit_publishes_deleted_and_failed(tmp_path: Path) -> None:
    """
    A required terminal disappearing fails the owning session.

    This uses a generic ``worker`` terminal name to pin the lifecycle rule,
    not a Claude-specific branch: if the terminal was registered as required,
    the runner must publish both resource deletion and ``session.status:
    failed`` when its watcher reports that tmux disappeared.

    :param tmp_path: Temporary directory for fake terminal paths.
    """
    from omnigent.runner import app as runner_app
    from omnigent.runner.app import _session_event_queues_ref
    from tests.runner.helpers import make_test_terminal_instance

    parent_id = f"conv_parent_required_exit_{uuid.uuid4().hex[:12]}"
    conv_id = f"conv_required_exit_{uuid.uuid4().hex[:12]}"
    parent_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    terminal_registry = TerminalRegistry()
    instance = make_test_terminal_instance("worker", "main", tmp_path)
    instance.command = "worker-cli"
    instance.args = ["--profile", "test"]
    instance.launch_cwd = str(tmp_path)
    instance._remember_pane_snapshot("startup failed\ncomplete setup first")
    terminal_registry._by_conversation.setdefault(conv_id, {})[("worker", "main")] = instance
    callbacks: dict[str, Any] = {}

    def _capture_watcher(
        on_idle: object | None = None,
        *,
        on_activity: object | None = None,
        on_exit: object | None = None,
        idle_threshold_s: float | None = None,
        poll_interval_s: float | None = None,
        replace: bool = False,
    ) -> None:
        del on_idle, on_activity, idle_threshold_s, poll_interval_s
        callbacks["on_exit"] = on_exit
        callbacks["replace"] = replace

    instance.start_idle_watcher_thread = _capture_watcher  # type: ignore[method-assign]
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    pm._sessions.add(conv_id)
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
        terminal_registry=terminal_registry,
    )
    resource_registry = app.state.session_resource_registry
    runner_app._session_inboxes_ref[parent_id] = parent_inbox
    runner_app.register_child_session(
        conv_id,
        parent_session_id=parent_id,
        title="worker:main",
        tool="worker",
        session_name="main",
    )
    runner_app.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=conv_id,
        agent="worker",
        title="main",
    )

    async def _collect_exit_events() -> list[dict[str, Any]]:
        while True:
            queue = _session_event_queues_ref.get(conv_id)
            if queue is not None and queue.qsize() >= 2:
                events: list[dict[str, Any]] = []
                while not queue.empty():
                    item = queue.get_nowait()
                    if isinstance(item, dict):
                        events.append(item)
                return events
            await asyncio.sleep(0)

    try:
        await resource_registry.observe_required_terminal(
            conv_id,
            "worker",
            "main",
            instance,
        )
        on_exit = callbacks.get("on_exit")
        assert callable(on_exit)
        on_exit()
        queued_events = await asyncio.wait_for(_collect_exit_events(), timeout=1.0)
        for _ in range(100):
            if pm.released:
                break
            await asyncio.sleep(0)
        parent_events = _drain_session_event_queue(_session_event_queues_ref.get(parent_id))
    finally:
        _session_event_queues_ref.pop(conv_id, None)
        _session_event_queues_ref.pop(parent_id, None)
        runner_app.unregister_subagent_work(conv_id)
        runner_app.unregister_child_session(conv_id)
        runner_app._session_inboxes_ref.pop(parent_id, None)

    assert terminal_registry.get(conv_id, "worker", "main") is None
    assert {
        "type": "session.resource.deleted",
        "resource_id": "terminal_worker_main",
        "resource_type": "terminal",
        "session_id": conv_id,
    } in queued_events
    failed_events = [
        event
        for event in queued_events
        if event.get("type") == "session.status" and event.get("status") == "failed"
    ]
    assert len(failed_events) == 1, f"expected one failed status, got {queued_events!r}"
    assert failed_events[0]["error"]["code"] == "required_terminal_exited"
    assert "Required terminal exited unexpectedly" in failed_events[0]["error"]["message"]
    assert parent_events == [
        {
            "type": "session.child_session.updated",
            "conversation_id": parent_id,
            "child_session_id": conv_id,
            "child": {
                "id": conv_id,
                "title": "worker:main",
                "tool": "worker",
                "session_name": "main",
                "busy": False,
                "current_task_status": "failed",
                "last_task_error": failed_events[0]["error"],
            },
        }
    ]
    assert pm.released == [conv_id]
    inbox_item = parent_inbox.get_nowait()
    assert inbox_item["status"] == "failed"
    assert "Required terminal exited unexpectedly" in inbox_item["output"]
    assert "command: worker-cli (2 args; argv omitted" in inbox_item["output"]
    assert f"cwd: {tmp_path}" in inbox_item["output"]
    assert "startup failed\ncomplete setup first" in inbox_item["output"]
    assert "Suggested next checks" not in inbox_item["output"]


@pytest.mark.asyncio
async def test_required_terminal_exit_while_idle_does_not_fail_session(tmp_path: Path) -> None:
    """
    A required terminal that exits while the session is idle is a clean shutdown.

    The native agent terminal is long-lived and goes ``idle`` once its turn
    completes. When the pane then disappears, the work for that turn was already
    delivered, so the runner must NOT publish ``session.status: failed`` — doing
    so was the source of spurious "failed" chats in the UI. The terminal
    resource is still removed and the harness subprocess released; the runner
    going offline is surfaced separately via liveness, not a failure.

    :param tmp_path: Temporary directory for fake terminal paths.
    """
    from omnigent.runner import app as runner_app
    from omnigent.runner.app import _session_event_queues_ref
    from tests.runner.helpers import make_test_terminal_instance

    parent_id = f"conv_parent_idle_exit_{uuid.uuid4().hex[:12]}"
    conv_id = f"conv_idle_exit_{uuid.uuid4().hex[:12]}"
    parent_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    terminal_registry = TerminalRegistry()
    instance = make_test_terminal_instance("worker", "main", tmp_path)
    instance.command = "worker-cli"
    instance.launch_cwd = str(tmp_path)
    terminal_registry._by_conversation.setdefault(conv_id, {})[("worker", "main")] = instance
    callbacks: dict[str, Any] = {}

    def _capture_watcher(
        on_idle: object | None = None,
        *,
        on_activity: object | None = None,
        on_exit: object | None = None,
        idle_threshold_s: float | None = None,
        poll_interval_s: float | None = None,
        replace: bool = False,
    ) -> None:
        del on_idle, on_activity, idle_threshold_s, poll_interval_s, replace
        callbacks["on_exit"] = on_exit

    instance.start_idle_watcher_thread = _capture_watcher  # type: ignore[method-assign]
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    pm._sessions.add(conv_id)
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
        terminal_registry=terminal_registry,
    )
    resource_registry = app.state.session_resource_registry
    runner_app._session_inboxes_ref[parent_id] = parent_inbox
    runner_app.register_child_session(
        conv_id,
        parent_session_id=parent_id,
        title="worker:main",
        tool="worker",
        session_name="main",
    )
    runner_app.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=conv_id,
        agent="worker",
        title="main",
    )

    try:
        await resource_registry.observe_required_terminal(
            conv_id,
            "worker",
            "main",
            instance,
        )
        # The session reached idle (turn completed) before the pane vanished.
        resource_registry._last_session_status[conv_id] = "idle"
        on_exit = callbacks.get("on_exit")
        assert callable(on_exit)
        on_exit()
        # Terminal-exit cleanup fans out across two independent background
        # tasks: one publishes the resource events, a second releases the
        # harness subprocess. Their completion order is not guaranteed, so
        # settle on BOTH the published ``deleted`` event and the subprocess
        # release — accumulating drained events each tick — rather than using
        # the release as a proxy for the publish (which raced: the release
        # task could finish first, leaving the drain empty).
        deleted_event = {
            "type": "session.resource.deleted",
            "resource_id": "terminal_worker_main",
            "resource_type": "terminal",
            "session_id": conv_id,
        }
        queued_events: list[dict[str, Any]] = []
        parent_events: list[dict[str, Any]] = []
        for _ in range(1000):
            queued_events.extend(
                _drain_session_event_queue(_session_event_queues_ref.get(conv_id))
            )
            parent_events.extend(
                _drain_session_event_queue(_session_event_queues_ref.get(parent_id))
            )
            if pm.released and deleted_event in queued_events:
                break
            await asyncio.sleep(0)
    finally:
        _session_event_queues_ref.pop(conv_id, None)
        _session_event_queues_ref.pop(parent_id, None)
        runner_app.unregister_subagent_work(conv_id)
        runner_app.unregister_child_session(conv_id)
        runner_app._session_inboxes_ref.pop(parent_id, None)

    # The terminal resource is still removed...
    assert terminal_registry.get(conv_id, "worker", "main") is None
    assert deleted_event in queued_events
    # ...but no failure is published, and the parent is not woken as failed.
    assert [
        event
        for event in queued_events
        if event.get("type") == "session.status" and event.get("status") == "failed"
    ] == []
    assert parent_events == []
    assert parent_inbox.empty()
    # The harness subprocess is still released — the terminal is gone.
    assert pm.released == [conv_id]


@pytest.mark.parametrize("terminal_name", ["qwen", "antigravity"])
@pytest.mark.asyncio
async def test_required_terminal_clean_quit_publishes_idle_not_failed(
    terminal_name: str,
) -> None:
    """A clean ``/quit`` of qwen/antigravity-native is not a crash.

    Both harnesses leave the exit-classification memo stuck on ``running`` at
    quit time — qwen's "powering down" redraw trips the PTY-activity watcher,
    and antigravity-native is deliberately excluded from the PTY ``emit_status``
    role set (the RPC reader owns working-status). So ``session_was_idle`` is
    ``False`` even though the user quit normally. The runner must special-case
    these terminals: publish a final ``idle`` (to clear the web "Working…"
    spinner) and release the harness, but never render the spurious red
    ``required_terminal_exited`` failure card.

    :param terminal_name: The native terminal that the user quit cleanly.
    """
    from omnigent.runner import app as runner_app
    from omnigent.runner.app import _session_event_queues_ref
    from omnigent.runner.resource_registry import (
        TerminalExitEvent,
        TerminalLifecycle,
    )

    conv_id = f"conv_clean_quit_{terminal_name}_{uuid.uuid4().hex[:12]}"
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    pm._sessions.add(conv_id)
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    resource_registry = app.state.session_resource_registry
    # Grab the runner's terminal-exit publisher (the branch under test) and
    # drive it directly, mimicking the registry firing on a clean quit.
    publish_exit = resource_registry._terminal_exit_publisher
    assert callable(publish_exit)

    try:
        publish_exit(
            TerminalExitEvent(
                session_id=conv_id,
                terminal_id=f"terminal_{terminal_name}_main",
                terminal_name=terminal_name,
                session_key="main",
                lifecycle=TerminalLifecycle.REQUIRED,
                # The memo never flipped to idle, so the generic guard would
                # otherwise misclassify this normal quit as a crash.
                session_was_idle=False,
            )
        )
        queued_events: list[dict[str, Any]] = []
        for _ in range(1000):
            queued_events.extend(
                _drain_session_event_queue(_session_event_queues_ref.get(conv_id))
            )
            if pm.released:
                break
            await asyncio.sleep(0)
    finally:
        _session_event_queues_ref.pop(conv_id, None)
        runner_app.unregister_child_session(conv_id)

    # The terminal resource is removed and a final idle clears the spinner...
    assert {
        "type": "session.resource.deleted",
        "resource_id": f"terminal_{terminal_name}_main",
        "resource_type": "terminal",
        "session_id": conv_id,
    } in queued_events
    assert {"type": "session.status", "status": "idle"} in queued_events
    # ...but no spurious failure card renders — the user quit normally.
    assert [
        event
        for event in queued_events
        if event.get("type") == "session.status" and event.get("status") == "failed"
    ] == []
    # The harness subprocess is still released — the terminal is gone.
    assert pm.released == [conv_id]


@pytest.mark.asyncio
async def test_external_idle_status_makes_required_terminal_exit_clean(tmp_path: Path) -> None:
    """
    A structured native ``idle`` status prevents a later pane close from failing.

    Kiro completion is observed from its persisted JSONL session, not only from
    PTY diff-idle. After a web turn marks the required terminal ``running``, the
    forwarded ``external_session_status: idle`` must update the same exit memo
    used by the required-terminal watcher; otherwise a normal user close after
    Kiro answered is misclassified as ``required_terminal_exited``.

    :param tmp_path: Temporary directory for fake terminal paths.
    """
    from omnigent.runner.app import _session_event_queues_ref
    from tests.runner.helpers import make_test_terminal_instance

    conv_id = f"conv_kiro_external_idle_exit_{uuid.uuid4().hex[:12]}"
    terminal_registry = TerminalRegistry()
    instance = make_test_terminal_instance("kiro", "main", tmp_path)
    terminal_registry._by_conversation.setdefault(conv_id, {})[("kiro", "main")] = instance
    callbacks: dict[str, Any] = {}

    def _capture_watcher(
        on_idle: object | None = None,
        *,
        on_activity: object | None = None,
        on_exit: object | None = None,
        idle_threshold_s: float | None = None,
        poll_interval_s: float | None = None,
        replace: bool = False,
    ) -> None:
        del on_idle, on_activity, idle_threshold_s, poll_interval_s, replace
        callbacks["on_exit"] = on_exit

    instance.start_idle_watcher_thread = _capture_watcher  # type: ignore[method-assign]
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    pm._sessions.add(conv_id)
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
        terminal_registry=terminal_registry,
    )
    resource_registry = app.state.session_resource_registry

    try:
        await resource_registry.observe_required_terminal(
            conv_id,
            "kiro",
            "main",
            instance,
            resource_role=KIRO_NATIVE_TERMINAL_ROLE,
        )
        resource_registry.note_session_turn_started(conv_id)
        async with _runner_client(app) as client:
            status_resp = await client.post(
                f"/v1/sessions/{conv_id}/events",
                json={"type": "external_session_status", "data": {"status": "idle"}},
            )
        assert status_resp.status_code == 204, status_resp.text

        on_exit = callbacks.get("on_exit")
        assert callable(on_exit)
        on_exit()
        deleted_event = {
            "type": "session.resource.deleted",
            "resource_id": "terminal_kiro_main",
            "resource_type": "terminal",
            "session_id": conv_id,
        }
        queued_events: list[dict[str, Any]] = []
        for _ in range(1000):
            queued_events.extend(
                _drain_session_event_queue(_session_event_queues_ref.get(conv_id))
            )
            if pm.released and deleted_event in queued_events:
                break
            await asyncio.sleep(0)
    finally:
        _session_event_queues_ref.pop(conv_id, None)

    assert terminal_registry.get(conv_id, "kiro", "main") is None
    assert deleted_event in queued_events
    assert [
        event
        for event in queued_events
        if event.get("type") == "session.status" and event.get("status") == "failed"
    ] == []
    assert pm.released == [conv_id]


@pytest.mark.asyncio
async def test_events_effort_change_on_native_session_types_slash_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    POST ``/events`` with ``{"type":"effort_change","effort":"high"}``
    on a claude-native session injects ``/effort high`` into tmux.

    With the unified-effort refactor Omnigent server no longer POSTs to
    ``/claude-native-effort`` — every PATCH effort goes through the
    generic ``/events`` path. The runner's ``/events`` dispatch must
    recognize the native harness and route to
    ``_handle_claude_native_effort_change``, which assembles the
    slash command and types it into the pane.

    A regression in the dispatch (wrong harness name, missing branch)
    would fall through to the generic harness-forward and 404, leaving
    the dropdown click silently ineffective.
    """
    from omnigent.runner.app import _session_event_queues_ref
    from omnigent.spec.types import ExecutorSpec

    captured: list[Any] = []

    def _fake_inject(
        bridge_dir: Any,
        *,
        command: str,
        timeout_s: float,
        auto_confirm: bool = False,
    ) -> None:
        """Record the call and return without touching tmux."""
        captured.append((bridge_dir, command, timeout_s))

    monkeypatch.setattr(claude_native_bridge, "inject_slash_command", _fake_inject)

    native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the native spec for any agent_id."""
        del agent_id
        return native_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        # Seed _session_spec_cache so /events can detect "claude-native".
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": "conv_native_effort", "agent_id": "ag_1"},
        )
        assert create_resp.status_code == 201, create_resp.text
        # Drain creation-time events (the claude-native auto-create path
        # enqueues session.terminal_pending) so the post-effort_change
        # drain below isolates only what the control event emits.
        _drain_session_event_queue(_session_event_queues_ref.get("conv_native_effort"))

        resp = await client.post(
            "/v1/sessions/conv_native_effort/events",
            json={"type": "effort_change", "effort": "high"},
        )

        # Drain the event queue before delete clears it, so we can
        # assert that effort_change does NOT enqueue spurious events
        # (it's a control signal, not a session-state change).
        queue = _session_event_queues_ref.get("conv_native_effort")
        queued_events: list[dict[str, Any]] = []
        if queue is not None:
            while not queue.empty():
                item = queue.get_nowait()
                if isinstance(item, dict):
                    queued_events.append(item)

    # 1) 204 = the dispatch correctly routed to the native handler and
    # the handler completed cleanly. 404 would mean the dispatch fell
    # through to the generic harness-forward.
    assert resp.status_code == 204, (
        f"Native effort_change must return 204 from /events; got {resp.status_code}: {resp.text}"
    )
    # 2) Exactly one inject call. 0 = native dispatch missed (likely
    # _session_harness_name returned the wrong canonical name); 2+ =
    # the handler ran twice.
    assert len(captured) == 1, (
        f"Expected one inject_slash_command call from native effort_change, got {len(captured)}."
    )
    bridge_dir, command, timeout_s = captured[0]
    assert bridge_dir == bridge_dir_for_conversation_id("conv_native_effort")
    # Body contract: ``/effort high`` is the literal Claude Code's TUI
    # accepts. A regression in shape (``/efforthigh``, ``effort high``,
    # missing leading slash) would either 404 on the slash router or
    # land as plain text in the prompt.
    assert command == "/effort high", f"Expected '/effort high' literal, got {command!r}."
    # 1.0s short timeout: missing tmux.json means the pane isn't
    # attached; persisted effort still applies on next spawn. A 30s
    # default would hang the Omnigent PATCH whenever the pane is detached.
    assert timeout_s == 1.0
    # 3) effort_change is a control signal, not a state change.
    # Any session.status enqueued here would mislead the Omnigent relay.
    assert queued_events == [], (
        f"effort_change must not publish session events; got "
        f"{queued_events!r}. If non-empty, the native handler is "
        f"emitting spurious status events."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "effort_value",
    # ``EFFORT_VALUES`` is a superset of ``CLAUDE_EFFORTS``:
    # PATCH accepts {none, minimal, low, medium, high, xhigh, max}
    # but Claude Code's ``/effort`` slash only accepts the last five.
    # ``none`` and ``minimal`` must skip injection (typing ``/effort
    # none`` would land as a TUI error). ``None`` (clear) must skip
    # too — Claude has no slash form for "use spawn default".
    ["none", "minimal", None],
)
async def test_events_effort_change_on_native_session_skips_inject_for_unsupported_level(
    monkeypatch: pytest.MonkeyPatch,
    effort_value: str | None,
) -> None:
    """
    Unsupported / null effort values 204 without typing into tmux.

    Omnigent server is harness-agnostic — it always forwards the new
    persisted effort to ``/events``. The runner's native handler
    owns the level-validation, skipping injection when the value
    isn't in Claude's accepted set. Persistence already happened on
    the Omnigent side; the next spawn picks up the value via ``--effort``.

    Pins that the validation lives in the runner (where the
    harness-specific knowledge belongs), not in the Omnigent server.
    """
    from omnigent.spec.types import ExecutorSpec

    def _fake_inject(
        bridge_dir: Any,
        *,
        command: str,
        timeout_s: float,
        auto_confirm: bool = False,
    ) -> None:
        """Fail the test if the runner reaches inject for an unsupported level."""
        del bridge_dir, command, timeout_s
        raise AssertionError(
            f"inject_slash_command must not be called for effort={effort_value!r}; "
            f"the native handler should skip unsupported / null levels."
        )

    monkeypatch.setattr(claude_native_bridge, "inject_slash_command", _fake_inject)

    native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the native spec for any agent_id."""
        del agent_id
        return native_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": "conv_native_effort_skip", "agent_id": "ag_1"},
        )
        assert create_resp.status_code == 201, create_resp.text

        resp = await client.post(
            "/v1/sessions/conv_native_effort_skip/events",
            json={"type": "effort_change", "effort": effort_value},
        )

    # 204 = the handler ran and decided to skip. 502 would mean it
    # fell through to the harness-forward path. The fake inject above
    # asserts loudly if injection was attempted — silence here proves
    # the skip took effect.
    assert resp.status_code == 204, (
        f"Native effort_change with unsupported / null level must "
        f"return 204 (no-op); got {resp.status_code}: {resp.text}"
    )


@pytest.mark.asyncio
async def test_events_effort_change_on_native_session_returns_503_when_bridge_not_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Bridge-not-ready RuntimeError surfaces as 503 from /events.

    Sister to the happy-path test. Pins that the failure mode of the
    native effort dispatch (tmux pane gone / bridge dir not yet
    advertised) returns 503 with the same error code shape the
    legacy route returns. Omnigent server's PATCH swallows this 503 and
    still returns 200 with the persisted value — the next spawn
    will apply the new effort via ``--effort``.
    """
    from omnigent.spec.types import ExecutorSpec

    def _fake_inject(
        bridge_dir: Any,
        *,
        command: str,
        timeout_s: float,
        auto_confirm: bool = False,
    ) -> None:
        """Simulate the bridge-not-ready path."""
        del bridge_dir, command, timeout_s
        raise RuntimeError("tmux target is not advertised")

    monkeypatch.setattr(claude_native_bridge, "inject_slash_command", _fake_inject)

    native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the native spec for any agent_id."""
        del agent_id
        return native_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": "conv_native_effort_fail", "agent_id": "ag_1"},
        )
        assert create_resp.status_code == 201, create_resp.text

        resp = await client.post(
            "/v1/sessions/conv_native_effort_fail/events",
            json={"type": "effort_change", "effort": "high"},
        )

    assert resp.status_code == 503, (
        f"Native effort_change with inject failure must return 503; "
        f"got {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    # ``claude_native_effort_failed`` is the same error code the
    # legacy route uses — keeps the failure shape stable for callers.
    assert body.get("error") == "claude_native_effort_failed", (
        f"503 body must carry the bridge-failure error code; got {body!r}"
    )


@pytest.mark.asyncio
async def test_events_effort_change_on_non_native_session_is_204_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Non-native sessions accept effort_change and 204 without side effects.

    In-process harnesses (default / claude-sdk / openai-agents / codex)
    re-read the persisted ``reasoning_effort`` from store on each
    turn, so they need no runtime notification when it changes. The
    Omnigent server still POSTs ``effort_change`` to ``/events`` for every
    PATCH (it's harness-agnostic), so the runner must accept the
    event and 204 — never reach the slash-command injector, never
    forward to the harness scaffold.
    """
    from omnigent.spec.types import ExecutorSpec

    def _fake_inject(
        bridge_dir: Any,
        *,
        command: str,
        timeout_s: float,
        auto_confirm: bool = False,
    ) -> None:
        """Fail the test if a non-native session reaches the injector."""
        del bridge_dir, command, timeout_s
        raise AssertionError(
            "inject_slash_command must never be called for non-native "
            "sessions — effort_change is a no-op for in-process harnesses."
        )

    monkeypatch.setattr(claude_native_bridge, "inject_slash_command", _fake_inject)

    # Default harness (in-process LLM loop), NOT claude-native.
    default_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the default spec for any agent_id."""
        del agent_id
        return default_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": "conv_default_effort", "agent_id": "ag_1"},
        )
        assert create_resp.status_code == 201, create_resp.text

        resp = await client.post(
            "/v1/sessions/conv_default_effort/events",
            json={"type": "effort_change", "effort": "high"},
        )

    # 204 = the dispatch saw a non-native harness and returned the
    # no-op short-circuit before any forward / inject. Anything else
    # (200/202/4xx/5xx) would mean the event leaked into a code path
    # it shouldn't reach.
    assert resp.status_code == 204, (
        f"Non-native effort_change must return 204 no-op; got {resp.status_code}: {resp.text}"
    )


@pytest.mark.asyncio
async def test_events_compact_on_native_session_types_slash_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    POST ``/events`` with ``{"type":"compact"}`` on a claude-native
    session injects ``/compact`` into tmux and returns 200.

    Explicit compaction on a claude-native session must run inside
    Claude Code (it owns its own context window in the terminal); the
    Omnigent server's own compaction would only summarise the transcript
    mirror. The runner's ``/events`` dispatch recognises the native
    harness and routes to ``_handle_claude_native_compact``, which
    types the slash command into the pane.

    The 200 (not 204) is load-bearing: the Omnigent server reads it to know
    the control was handled in the terminal and skips its own
    in-process compaction. A regression returning 204 here would make
    the Omnigent server fall through to ``_run_compact_locked``, which 400s
    on the LLM-less claude-native pseudo-agent — the original bug.
    """
    from omnigent.runner.app import _session_event_queues_ref
    from omnigent.spec.types import ExecutorSpec

    captured: list[Any] = []

    def _fake_inject(
        bridge_dir: Any,
        *,
        command: str,
        timeout_s: float,
        auto_confirm: bool = False,
    ) -> None:
        """Record the call (including auto_confirm) without touching tmux."""
        captured.append((bridge_dir, command, timeout_s, auto_confirm))

    monkeypatch.setattr(claude_native_bridge, "inject_slash_command", _fake_inject)

    native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the native spec for any agent_id."""
        del agent_id
        return native_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": "conv_native_compact", "agent_id": "ag_1"},
        )
        assert create_resp.status_code == 201, create_resp.text
        # Drain creation-time events (claude-native auto-create enqueues
        # session.terminal_pending) so the drain below isolates only
        # what /compact emits.
        _drain_session_event_queue(_session_event_queues_ref.get("conv_native_compact"))

        resp = await client.post(
            "/v1/sessions/conv_native_compact/events",
            json={"type": "compact"},
        )

        # Drain the event queue: /compact is a control signal and must
        # not enqueue session.status events.
        queue = _session_event_queues_ref.get("conv_native_compact")
        queued_events: list[dict[str, Any]] = []
        if queue is not None:
            while not queue.empty():
                item = queue.get_nowait()
                if isinstance(item, dict):
                    queued_events.append(item)

    # 200 = native dispatch routed to the compact handler and it
    # injected successfully. 204 would mean the handler returned the
    # in-process no-op (wrong harness branch) → Omnigent falls through to
    # _run_compact_locked and 400s. 404 = the dispatch fell through to
    # the generic harness-forward.
    assert resp.status_code == 200, (
        f"Native compact must return 200 from /events; got {resp.status_code}: {resp.text}"
    )
    # Exactly one inject call. 0 = native dispatch missed; 2+ = handler ran twice.
    assert len(captured) == 1, (
        f"Expected one inject_slash_command call from native compact, got {len(captured)}."
    )
    bridge_dir, command, timeout_s, auto_confirm = captured[0]
    assert bridge_dir == bridge_dir_for_conversation_id("conv_native_compact")
    # Body contract: the literal ``/compact`` is what Claude Code's TUI
    # accepts. A shape regression (``compact``, missing slash) would
    # land as plain prompt text instead of running compaction.
    assert command == "/compact", f"Expected '/compact' literal, got {command!r}."
    # 1.0s short timeout: missing tmux.json means the pane isn't
    # attached, so there's no live Claude to compact.
    assert timeout_s == 1.0
    # auto_confirm must be False — unlike /effort and /model, /compact
    # does not pop a confirmation dialog, so an extra Enter would land
    # on the prompt and submit a stray empty turn.
    assert auto_confirm is False, (
        f"compact must not auto-confirm; got auto_confirm={auto_confirm!r}."
    )
    # /compact is a control signal, not a state change.
    assert queued_events == [], f"compact must not publish session events; got {queued_events!r}."


@pytest.mark.asyncio
async def test_events_compact_on_native_session_returns_503_when_bridge_not_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Bridge-not-ready RuntimeError surfaces as 503 from /events.

    Sister to the happy-path test. When the tmux pane isn't attached
    there is no live Claude to compact, so the native compact handler
    returns 503 with the ``claude_native_compact_failed`` code. The AP
    server treats a non-200/204 runner response as an error rather
    than silently running its own (wrong) compaction.
    """
    from omnigent.spec.types import ExecutorSpec

    def _fake_inject(
        bridge_dir: Any,
        *,
        command: str,
        timeout_s: float,
        auto_confirm: bool = False,
    ) -> None:
        """Simulate the bridge-not-ready path."""
        del bridge_dir, command, timeout_s, auto_confirm
        raise RuntimeError("tmux target is not advertised")

    monkeypatch.setattr(claude_native_bridge, "inject_slash_command", _fake_inject)

    native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the native spec for any agent_id."""
        del agent_id
        return native_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": "conv_native_compact_fail", "agent_id": "ag_1"},
        )
        assert create_resp.status_code == 201, create_resp.text

        resp = await client.post(
            "/v1/sessions/conv_native_compact_fail/events",
            json={"type": "compact"},
        )

    assert resp.status_code == 503, (
        f"Native compact with inject failure must return 503; got {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    assert body.get("error") == "claude_native_compact_failed", (
        f"503 body must carry the bridge-failure error code; got {body!r}"
    )


@pytest.mark.asyncio
async def test_events_compact_on_codex_native_injects_slash_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    POST ``/events`` with ``{"type":"compact"}`` on a codex-native
    session injects ``/compact`` into the codex tmux pane and returns 200.

    Codex owns its own context window in the terminal, so explicit
    compaction must run inside Codex — the same rationale as the
    claude-native path.  The pane coordinates come from the resource
    registry (not a ``tmux.json`` sidecar).  The 200 return is
    load-bearing: the Omnigent server reads it to skip its own
    AP-side compaction.
    """
    from omnigent.runner.app import _session_event_queues_ref
    from tests.runner.helpers import make_test_terminal_instance

    captured: list[tuple[str, list[str]]] = []

    def _fake_run_tmux(socket_path: str, *args: str) -> None:
        """Record tmux send-keys calls without touching tmux."""
        captured.append((socket_path, list(args)))

    monkeypatch.setattr(claude_native_bridge, "_run_tmux", _fake_run_tmux)

    codex_native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "codex-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the codex-native spec for any agent_id."""
        del agent_id, session_id
        return codex_native_spec

    conv_id = "conv_codex_compact"
    terminal_registry = TerminalRegistry()
    instance = make_test_terminal_instance("codex", "main", tmp_path)
    terminal_registry._by_conversation.setdefault(conv_id, {})[("codex", "main")] = instance

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
        terminal_registry=terminal_registry,
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": conv_id, "agent_id": "ag_1"},
        )
        assert create_resp.status_code == 201, create_resp.text
        _drain_session_event_queue(_session_event_queues_ref.get(conv_id))

        resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={"type": "compact"},
        )

        # Drain the event queue: /compact is a control signal and must
        # not enqueue session.status events.
        queue = _session_event_queues_ref.get(conv_id)
        queued_events: list[dict[str, Any]] = []
        if queue is not None:
            while not queue.empty():
                item = queue.get_nowait()
                if isinstance(item, dict):
                    queued_events.append(item)

    # 200 = codex-native dispatch routed to the compact handler and it
    # injected successfully.
    assert resp.status_code == 200, (
        f"Codex-native compact must return 200 from /events; got {resp.status_code}: {resp.text}"
    )

    # Exactly 3 tmux send-keys calls: C-u, -l /compact, Enter.
    assert len(captured) == 3, (
        f"Expected 3 tmux send-keys calls (C-u, /compact, Enter), got {len(captured)}."
    )
    socket = str(instance.socket_path)
    # 1. Clear draft: C-u
    assert captured[0] == (socket, ["send-keys", "-t", "main", "C-u"]), (
        f"First call must clear draft with C-u; got {captured[0]!r}."
    )
    # 2. Type /compact literally
    assert captured[1] == (socket, ["send-keys", "-l", "-t", "main", "/compact"]), (
        f"Second call must type /compact literally; got {captured[1]!r}."
    )
    # 3. Submit with Enter
    assert captured[2] == (socket, ["send-keys", "-t", "main", "Enter"]), (
        f"Third call must submit with Enter; got {captured[2]!r}."
    )
    # /compact is a control signal, not a state change.
    assert queued_events == [], f"compact must not publish session events; got {queued_events!r}."


@pytest.mark.asyncio
async def test_events_compact_on_codex_native_returns_204_when_no_terminal() -> None:
    """
    Codex-native compact returns 204 when no live terminal is registered.

    Without a running codex terminal the ``/compact`` slash command
    has nowhere to go.  204 tells the Omnigent server to fall back to
    its own AP-side compaction (or skip it).
    """
    codex_native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "codex-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the codex-native spec for any agent_id."""
        del agent_id, session_id
        return codex_native_spec

    conv_id = "conv_codex_compact_no_term"
    # Empty registry — no codex terminal registered.
    terminal_registry = TerminalRegistry()

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
        terminal_registry=terminal_registry,
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": conv_id, "agent_id": "ag_1"},
        )
        assert create_resp.status_code == 201, create_resp.text

        resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={"type": "compact"},
        )

    assert resp.status_code == 204, (
        f"Codex-native compact with no terminal must return 204; "
        f"got {resp.status_code}: {resp.text}"
    )


@pytest.mark.asyncio
async def test_events_compact_on_codex_native_returns_503_on_tmux_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Codex-native compact returns 503 when the tmux send-keys call fails.

    The 503 tells the Omnigent server the control was NOT handled, so it
    can surface an error rather than silently running its own (wrong)
    compaction.
    """
    from tests.runner.helpers import make_test_terminal_instance

    def _failing_run_tmux(socket_path: str, *args: str) -> None:
        """Simulate a tmux pane that is no longer alive."""
        del socket_path, args
        raise RuntimeError("no server running on /tmp/dead.sock")

    monkeypatch.setattr(claude_native_bridge, "_run_tmux", _failing_run_tmux)

    codex_native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "codex-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the codex-native spec for any agent_id."""
        del agent_id, session_id
        return codex_native_spec

    conv_id = "conv_codex_compact_fail"
    terminal_registry = TerminalRegistry()
    instance = make_test_terminal_instance("codex", "main", tmp_path)
    terminal_registry._by_conversation.setdefault(conv_id, {})[("codex", "main")] = instance

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
        terminal_registry=terminal_registry,
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": conv_id, "agent_id": "ag_1"},
        )
        assert create_resp.status_code == 201, create_resp.text

        resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={"type": "compact"},
        )

    assert resp.status_code == 503, (
        f"Codex-native compact with tmux failure must return 503; "
        f"got {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    assert body.get("error") == "codex_native_compact_failed", (
        f"503 body must carry the codex bridge-failure error code; got {body!r}"
    )


@pytest.mark.asyncio
async def test_events_compact_on_cursor_native_pastes_summarize_and_raises_spinner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    POST ``/events`` with ``{"type":"compact"}`` on a cursor-native
    session submits ``/summarize`` via bracketed paste, returns 200, and
    raises the "Compacting…" spinner — but does NOT complete it.

    cursor-agent manages its own context window in the TUI, so explicit
    compaction must run there (its built-in ``/summarize`` command) rather
    than as AP-side compaction — the same rationale as the claude-native
    path.  The 200 (not 204) is load-bearing: the Omnigent server reads it to
    skip its own ``_run_compact_locked`` (which 400s on the LLM-less native
    pseudo-agent).

    Two properties are pinned here:

    1. **The command must go through the bracketed-paste path**
       (``inject_user_message``), NOT a ``send-keys``-typed slash command.
       Typing the literal ``/summarize`` opens cursor-agent's slash-command
       autocomplete dropdown, and the single submit Enter then confirms the
       highlighted completion instead of submitting the command — so the
       command was never sent (the original bug, seen as ``/summarize`` left
       sitting in the input box).
    2. **The handler raises the spinner but must NOT complete it.** It publishes
       ``response.compaction.in_progress`` (→ "Compacting conversation…") only.
       cursor-agent runs the summarization asynchronously in the pane after the
       submit, so completing here would flash "Conversation compacted" while
       the TUI is still summarizing.  The ``completed`` edge is emitted later by
       the cursor forwarder when it observes the summary blob (covered by
       ``tests/test_cursor_native_forwarder.py``).
    """
    from omnigent.runner.app import _session_event_queues_ref
    from omnigent.spec.types import ExecutorSpec

    monkeypatch.setattr(cursor_native_bridge, "_BRIDGE_ROOT", tmp_path / "cursor-bridge")

    captured: list[tuple[Any, str, float]] = []

    def _fake_inject(bridge_dir: Any, *, content: str, timeout_s: float) -> None:
        """Record the bracketed-paste call without touching tmux."""
        captured.append((bridge_dir, content, timeout_s))

    monkeypatch.setattr(cursor_native_bridge, "inject_user_message", _fake_inject)

    cursor_native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "cursor-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the cursor-native spec for any agent_id."""
        del agent_id, session_id
        return cursor_native_spec

    conv_id = "conv_cursor_compact"
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": conv_id, "agent_id": "ag_1"},
        )
        assert create_resp.status_code == 201, create_resp.text
        # Drain creation-time events so the drain below isolates only what
        # /compact emits.
        _drain_session_event_queue(_session_event_queues_ref.get(conv_id))

        resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={"type": "compact"},
        )

        queued_events = _drain_session_event_queue(_session_event_queues_ref.get(conv_id))

    # 200 = cursor-native dispatch routed to the compact handler and the paste
    # succeeded. 204 would mean the dispatch fell through to the in-process
    # no-op branch (the original gap) → Omnigent runs its own compaction and 400s.
    assert resp.status_code == 200, (
        f"Cursor-native compact must return 200 from /events; got {resp.status_code}: {resp.text}"
    )
    # Exactly one paste call. 0 = dispatch missed the cursor branch.
    assert len(captured) == 1, (
        f"Expected one inject_user_message call from cursor compact, got {len(captured)}."
    )
    bridge_dir, content, timeout_s = captured[0]
    assert bridge_dir == cursor_native_bridge.bridge_dir_for_session_id(conv_id)
    # The literal ``/summarize`` is cursor-agent's compaction command. It is
    # delivered as *paste content*, not a typed slash command, so the
    # autocomplete dropdown never opens and the submit Enter sends the command.
    assert content == "/summarize", f"Expected '/summarize' paste content, got {content!r}."
    # 1.0s short timeout: a missing tmux target means the pane isn't attached,
    # so there is no live cursor TUI to compact.
    assert timeout_s == 1.0

    # The handler raises the spinner (in_progress) but must NOT complete it —
    # completion is the forwarder's job once the summary blob actually lands.
    # A regression re-adding ``completed`` here would flash the permanent
    # "Conversation compacted" marker while the TUI is still summarizing.
    compaction_types = [
        e.get("type")
        for e in queued_events
        if str(e.get("type", "")).startswith("response.compaction")
    ]
    assert compaction_types == ["response.compaction.in_progress"], (
        f"Handler must publish only in_progress (forwarder completes it); "
        f"got {compaction_types!r}."
    )
    in_progress = next(
        e for e in queued_events if e.get("type") == "response.compaction.in_progress"
    )
    assert in_progress.get("task_id") == conv_id, (
        f"in_progress must carry the session id as task_id; got {in_progress!r}."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("inject_exc", "label"),
    [
        # Pane not attached: _wait_for_tmux_info / _run_tmux raise RuntimeError.
        (RuntimeError("tmux target is not advertised"), "runtime"),
        # Filesystem fault writing the paste tempfile into bridge_dir (disk
        # full, perms, dir removed). cursor's inject_user_message has this
        # surface; the claude-native analog does not. A narrow
        # ``except (RuntimeError, ValueError)`` would let this escape AFTER
        # in_progress fired, stranding the spinner with no failed edge.
        (OSError("No space left on device"), "oserror"),
    ],
)
async def test_events_compact_on_cursor_native_503_dismisses_spinner_on_inject_failure(
    inject_exc: Exception,
    label: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    An injection failure surfaces as 503 AND dismisses the spinner.

    The handler publishes ``response.compaction.in_progress`` before injecting,
    so every failure path must publish ``response.compaction.failed`` to
    dismiss the "Compacting…" spinner — otherwise it is stranded forever — and
    must NOT publish ``completed`` (the history was never compacted). Covers
    both the tmux ``RuntimeError`` and the tempfile ``OSError`` surfaces; the
    latter is unique to cursor's bracketed-paste path.
    """
    from omnigent.runner.app import _session_event_queues_ref
    from omnigent.spec.types import ExecutorSpec

    monkeypatch.setattr(cursor_native_bridge, "_BRIDGE_ROOT", tmp_path / "cursor-bridge")

    def _fake_inject(bridge_dir: Any, *, content: str, timeout_s: float) -> None:
        """Simulate an injection failure (tmux down, or tempfile write fault)."""
        del bridge_dir, content, timeout_s
        raise inject_exc

    monkeypatch.setattr(cursor_native_bridge, "inject_user_message", _fake_inject)

    cursor_native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "cursor-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the cursor-native spec for any agent_id."""
        del agent_id, session_id
        return cursor_native_spec

    conv_id = f"conv_cursor_compact_fail_{label}"
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": conv_id, "agent_id": "ag_1"},
        )
        assert create_resp.status_code == 201, create_resp.text
        _drain_session_event_queue(_session_event_queues_ref.get(conv_id))

        resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={"type": "compact"},
        )

        queued_events = _drain_session_event_queue(_session_event_queues_ref.get(conv_id))

    assert resp.status_code == 503, (
        f"Cursor-native compact with no live pane must return 503; "
        f"got {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    assert body.get("error") == "cursor_native_compact_failed", (
        f"503 body must carry the cursor bridge-failure error code; got {body!r}"
    )

    compaction_types = [
        e.get("type")
        for e in queued_events
        if str(e.get("type", "")).startswith("response.compaction")
    ]
    # in_progress raised the spinner; failed must dismiss it. completed must
    # never fire — the history was not compacted.
    assert compaction_types == [
        "response.compaction.in_progress",
        "response.compaction.failed",
    ], f"Expected in_progress then failed (no completed); got {compaction_types!r}."


@pytest.mark.asyncio
async def test_events_compact_on_non_native_session_is_204_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Non-native sessions accept compact and 204 without side effects.

    For in-process harnesses, explicit compaction is an AP-side
    operation (``_run_compact_locked`` → ``compact_conversation_now``).
    The Omnigent server forwards ``compact`` to ``/events`` for every harness
    (it stays harness-agnostic), so the runner must accept the event
    and 204 — never reach the slash-command injector. The 204 tells the
    Omnigent server to run its own compaction.
    """
    from omnigent.spec.types import ExecutorSpec

    def _fake_inject(
        bridge_dir: Any,
        *,
        command: str,
        timeout_s: float,
        auto_confirm: bool = False,
    ) -> None:
        """Fail the test if a non-native session reaches the injector."""
        del bridge_dir, command, timeout_s, auto_confirm
        raise AssertionError(
            "inject_slash_command must never be called for non-native "
            "sessions — compact is an AP-side operation for in-process harnesses."
        )

    monkeypatch.setattr(claude_native_bridge, "inject_slash_command", _fake_inject)

    # Default harness (in-process LLM loop), NOT claude-native.
    default_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the default spec for any agent_id."""
        del agent_id
        return default_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": "conv_default_compact", "agent_id": "ag_1"},
        )
        assert create_resp.status_code == 201, create_resp.text

        resp = await client.post(
            "/v1/sessions/conv_default_compact/events",
            json={"type": "compact"},
        )

    # 204 = the dispatch saw a non-native harness and returned the
    # no-op short-circuit before any inject. The fake injector above
    # asserts loudly if reached, so silence here proves the no-op.
    assert resp.status_code == 204, (
        f"Non-native compact must return 204 no-op; got {resp.status_code}: {resp.text}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event_payload,inject_attr",
    # ``/fork`` creates a new conversation that reuses the
    # same Claude process (same bridge_dir), so the new session has
    # bridge_id != conv_id, stored on the ``omnigent.claude_native
    # .bridge_id`` label. The runner-side native dispatch MUST
    # resolve bridge_id via ``_claude_native_bridge_id_for_session``
    # so the slash command lands in the right pane. Using
    # ``bridge_dir_for_conversation_id(conv_id)`` would target a
    # stale / non-existent dir and silently 503.
    #
    # This is the bug that forced a revert. Pinned
    # here for both effort_change and model_change.
    [
        ({"type": "effort_change", "effort": "high"}, "inject_slash_command"),
        ({"type": "model_change", "model": "claude-opus-4-7"}, "inject_slash_command"),
    ],
    ids=["effort_change", "model_change"],
)
async def test_events_native_dispatch_resolves_bridge_id_via_label_lookup(
    monkeypatch: pytest.MonkeyPatch,
    event_payload: dict[str, Any],
    inject_attr: str,
) -> None:
    """
    Native effort / model dispatch must call
    ``_claude_native_bridge_id_for_session`` to resolve the
    bridge_id, not pass conv_id straight to
    ``bridge_dir_for_conversation_id``.

    Regression test for the bug that forced a revert. The
    handlers used ``bridge_dir_for_conversation_id(conv_id)``
    directly, which is broken for ``/fork`` sessions (bridge_id !=
    conv_id, stored on label
    ``omnigent.claude_native.bridge_id``).

    Strategy: monkeypatch ``_claude_native_bridge_id_for_session``
    to return a sentinel bridge_id distinct from conv_id. Then
    assert that the dispatch resolves the bridge_dir from the
    sentinel — proving the handler went through the label-lookup
    path rather than calling ``bridge_dir_for_conversation_id``
    directly. If the handler regresses to the conv_id-only path,
    the assertion fails.
    """
    from omnigent.claude_native_bridge import bridge_dir_for_bridge_id
    from omnigent.runner import app as runner_app_module
    from omnigent.spec.types import ExecutorSpec

    captured_bridge_dir: list[Any] = []

    def _fake_inject(bridge_dir: Any, **kwargs: Any) -> None:
        """Record the bridge_dir the dispatch resolved."""
        del kwargs
        captured_bridge_dir.append(bridge_dir)

    monkeypatch.setattr(claude_native_bridge, "inject_slash_command", _fake_inject)

    sentinel_bridge_id = "bridge_from_fork_label_xyz"

    async def _fake_bridge_id_lookup(*, server_client: Any, session_id: str) -> str:
        """Pretend the session's bridge_id label is the sentinel."""
        del server_client, session_id
        return sentinel_bridge_id

    monkeypatch.setattr(
        runner_app_module,
        "_claude_native_bridge_id_for_session",
        _fake_bridge_id_lookup,
    )

    native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the native spec for any agent_id."""
        del agent_id, session_id
        return native_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    conv_id = "conv_fork_bridge_check"
    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": conv_id, "agent_id": "ag_1"},
        )
        assert create_resp.status_code == 201, create_resp.text

        resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json=event_payload,
        )

    # The dispatch ran the native handler (inject was called via the
    # fake, which doesn't raise) and returned 204.
    assert resp.status_code == 204, (
        f"Native dispatch for {event_payload['type']!r} must return "
        f"204; got {resp.status_code}: {resp.text}"
    )
    # Exactly one inject call, with the bridge_dir derived from the
    # sentinel bridge_id — NOT from the conv_id.
    assert len(captured_bridge_dir) == 1, (
        f"Expected one inject call, got {len(captured_bridge_dir)}"
    )
    expected = bridge_dir_for_bridge_id(sentinel_bridge_id)
    assert captured_bridge_dir[0] == expected, (
        f"Native dispatch used the wrong bridge_dir. Expected the "
        f"bridge_id-label path ({expected!r}); got "
        f"{captured_bridge_dir[0]!r}. If this matches the conv_id-"
        f"hashed path, the handler regressed to ``bridge_dir_for_"
        f"conversation_id(conv_id)`` and would silently 503 against "
        f"the stale dir on real /fork sessions — the same bug that "
        f"previously forced a revert."
    )


@pytest.mark.asyncio
async def test_events_model_change_on_native_session_types_slash_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    POST ``/events`` with ``{"type":"model_change","model":"claude-opus-4-7"}``
    on a claude-native session injects ``/model claude-opus-4-7`` into tmux.

    Mirrors the effort_change happy-path test. Pins that the new
    runner dispatch routes model_change to the native handler and
    assembles the right slash command.
    """
    from omnigent.runner.app import _session_event_queues_ref
    from omnigent.spec.types import ExecutorSpec

    captured: list[Any] = []

    def _fake_inject(
        bridge_dir: Any,
        *,
        command: str,
        timeout_s: float,
        auto_confirm: bool = False,
    ) -> None:
        """Record the call and return without touching tmux."""
        captured.append((bridge_dir, command, timeout_s))

    monkeypatch.setattr(claude_native_bridge, "inject_slash_command", _fake_inject)

    native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the native spec for any agent_id."""
        del agent_id, session_id
        return native_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": "conv_native_model", "agent_id": "ag_1"},
        )
        assert create_resp.status_code == 201, create_resp.text
        # Drain creation-time events (claude-native auto-create enqueues
        # session.terminal_pending) so the drain below isolates only
        # what model_change emits.
        _drain_session_event_queue(_session_event_queues_ref.get("conv_native_model"))

        resp = await client.post(
            "/v1/sessions/conv_native_model/events",
            json={"type": "model_change", "model": "claude-opus-4-7"},
        )

        # Drain the event queue before delete clears it. model_change
        # is a control signal, not a state change — no events should
        # land on the SSE queue.
        queue = _session_event_queues_ref.get("conv_native_model")
        queued_events: list[dict[str, Any]] = []
        if queue is not None:
            while not queue.empty():
                item = queue.get_nowait()
                if isinstance(item, dict):
                    queued_events.append(item)

    assert resp.status_code == 204, (
        f"Native model_change must return 204 from /events; got {resp.status_code}: {resp.text}"
    )
    assert len(captured) == 1, (
        f"Expected one inject_slash_command call from native model_change, got {len(captured)}."
    )
    _bridge_dir, command, timeout_s = captured[0]
    assert command == "/model claude-opus-4-7", (
        f"Expected '/model claude-opus-4-7' literal, got {command!r}."
    )
    assert timeout_s == 1.0
    assert queued_events == [], (
        f"model_change must not publish session events; got {queued_events!r}."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model_value",
    # Claude Code has no slash form for "use spawn default", so
    # ``None`` (clear) must skip injection. Empty / whitespace-only
    # strings must also skip — typing ``/model `` with nothing after
    # would land as a TUI error.
    [None, "", "   "],
)
async def test_events_model_change_on_native_session_skips_inject_for_empty_or_null(
    monkeypatch: pytest.MonkeyPatch,
    model_value: str | None,
) -> None:
    """
    Null / empty / whitespace-only model values 204 without typing.

    Pins that the empty-value validation lives in the runner native
    handler, not in the Omnigent server.
    """
    from omnigent.spec.types import ExecutorSpec

    def _fake_inject(
        bridge_dir: Any,
        *,
        command: str,
        timeout_s: float,
        auto_confirm: bool = False,
    ) -> None:
        """Fail the test if the runner reaches inject for an empty value."""
        del bridge_dir, command, timeout_s
        raise AssertionError(
            f"inject_slash_command must not be called for model={model_value!r}; "
            f"the native handler should skip empty / null values."
        )

    monkeypatch.setattr(claude_native_bridge, "inject_slash_command", _fake_inject)

    native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the native spec for any agent_id."""
        del agent_id, session_id
        return native_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": "conv_native_model_skip", "agent_id": "ag_1"},
        )
        assert create_resp.status_code == 201, create_resp.text

        resp = await client.post(
            "/v1/sessions/conv_native_model_skip/events",
            json={"type": "model_change", "model": model_value},
        )

    assert resp.status_code == 204, (
        f"Native model_change with empty / null value must return "
        f"204 (no-op); got {resp.status_code}: {resp.text}"
    )


@pytest.mark.asyncio
async def test_events_model_change_on_native_session_returns_503_when_bridge_not_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Bridge-not-ready RuntimeError surfaces as 503 from /events.

    Sister to the happy-path test. Pins that the failure mode of the
    native model dispatch (tmux pane gone / bridge dir not yet
    advertised) returns 503 with the same error code shape the
    legacy ``/claude-native-model`` route used. Omnigent server's PATCH
    swallows this 503 and still returns 200 with the persisted
    value — the next spawn applies the new model via ``--model``.
    """
    from omnigent.spec.types import ExecutorSpec

    def _fake_inject(
        bridge_dir: Any,
        *,
        command: str,
        timeout_s: float,
        auto_confirm: bool = False,
    ) -> None:
        """Simulate the bridge-not-ready path."""
        del bridge_dir, command, timeout_s
        raise RuntimeError("tmux target is not advertised")

    monkeypatch.setattr(claude_native_bridge, "inject_slash_command", _fake_inject)

    native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the native spec for any agent_id."""
        del agent_id, session_id
        return native_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": "conv_native_model_fail", "agent_id": "ag_1"},
        )
        assert create_resp.status_code == 201, create_resp.text

        resp = await client.post(
            "/v1/sessions/conv_native_model_fail/events",
            json={"type": "model_change", "model": "claude-opus-4-7"},
        )

    assert resp.status_code == 503, (
        f"Native model_change with inject failure must return 503; "
        f"got {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    assert body.get("error") == "claude_native_model_failed", (
        f"503 body must carry the bridge-failure error code; got {body!r}"
    )


@pytest.mark.asyncio
async def test_events_model_change_on_non_native_session_is_204_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Non-native sessions accept model_change and 204 without side effects.

    In-process harnesses re-read the persisted ``model_override`` on
    each turn (or via the per-event override). Omnigent server is harness-
    agnostic and POSTs model_change for every PATCH, so the runner
    must accept the event with a 204 — never reach the slash-command
    injector.
    """
    from omnigent.spec.types import ExecutorSpec

    def _fake_inject(
        bridge_dir: Any,
        *,
        command: str,
        timeout_s: float,
        auto_confirm: bool = False,
    ) -> None:
        """Fail the test if a non-native session reaches the injector."""
        del bridge_dir, command, timeout_s
        raise AssertionError(
            "inject_slash_command must never be called for non-native "
            "sessions — model_change is a no-op for in-process harnesses."
        )

    monkeypatch.setattr(claude_native_bridge, "inject_slash_command", _fake_inject)

    # Default harness (in-process LLM loop), NOT claude-native.
    default_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the default spec for any agent_id."""
        del agent_id, session_id
        return default_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": "conv_default_model", "agent_id": "ag_1"},
        )
        assert create_resp.status_code == 201, create_resp.text

        resp = await client.post(
            "/v1/sessions/conv_default_model/events",
            json={"type": "model_change", "model": "claude-opus-4-7"},
        )

    assert resp.status_code == 204, (
        f"Non-native model_change must return 204 no-op; got {resp.status_code}: {resp.text}"
    )


@pytest.mark.asyncio
async def test_events_model_change_on_cursor_native_session_types_slash_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    POST ``/events`` with ``model_change`` on a cursor-native session
    drives cursor-agent's ``/model`` picker via ``inject_model_command``.

    Cursor analog of the claude-native happy-path test: the runner
    dispatch must route cursor-native model_change to its TUI handler
    (not the claude slash injector and not a 204 no-op) and pass the
    model id straight through.
    """
    from omnigent.spec.types import ExecutorSpec

    captured: list[tuple[Any, str, float]] = []

    def _fake_inject(bridge_dir: Any, *, model: str, timeout_s: float) -> None:
        """Record the call and return without touching tmux."""
        captured.append((bridge_dir, model, timeout_s))

    monkeypatch.setattr(cursor_native_bridge, "inject_model_command", _fake_inject)

    native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "cursor-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the cursor-native spec for any agent_id."""
        del agent_id, session_id
        return native_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": "conv_cursor_model", "agent_id": "ag_1"},
        )
        assert create_resp.status_code == 201, create_resp.text

        resp = await client.post(
            "/v1/sessions/conv_cursor_model/events",
            json={"type": "model_change", "model": "gpt-5.2"},
        )

    assert resp.status_code == 204, (
        f"cursor-native model_change must return 204 from /events; "
        f"got {resp.status_code}: {resp.text}"
    )
    assert len(captured) == 1, f"Expected one inject_model_command call, got {len(captured)}."
    _bridge_dir, model, timeout_s = captured[0]
    assert model == "gpt-5.2", f"Expected the model id passed through, got {model!r}."
    assert timeout_s == 1.0


@pytest.mark.asyncio
@pytest.mark.parametrize("model_value", [None, "", "   "])
async def test_events_model_change_on_cursor_native_session_skips_inject_for_empty(
    monkeypatch: pytest.MonkeyPatch,
    model_value: str | None,
) -> None:
    """
    Null / empty / whitespace-only model values 204 without driving the picker.

    cursor-agent has no slash form for "use the spawn default", so a
    clear only takes effect on the next spawn — mirrors the claude-native
    skip test.
    """
    from omnigent.spec.types import ExecutorSpec

    def _fake_inject(bridge_dir: Any, *, model: str, timeout_s: float) -> None:
        """Fail the test if the runner reaches inject for an empty value."""
        del bridge_dir, timeout_s
        raise AssertionError(f"inject_model_command must not be called for model={model_value!r}.")

    monkeypatch.setattr(cursor_native_bridge, "inject_model_command", _fake_inject)

    native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "cursor-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the cursor-native spec for any agent_id."""
        del agent_id, session_id
        return native_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": "conv_cursor_model_skip", "agent_id": "ag_1"},
        )
        assert create_resp.status_code == 201, create_resp.text

        resp = await client.post(
            "/v1/sessions/conv_cursor_model_skip/events",
            json={"type": "model_change", "model": model_value},
        )

    assert resp.status_code == 204, (
        f"cursor-native model_change with empty / null value must return "
        f"204 (no-op); got {resp.status_code}: {resp.text}"
    )


@pytest.mark.asyncio
async def test_events_model_change_on_cursor_native_session_returns_503_when_not_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Bridge-not-ready RuntimeError surfaces as 503 from /events.

    Cursor analog of the claude-native 503 test: a missing tmux target
    (pane not attached yet) returns 503 with the cursor-specific error
    code; Omnigent server swallows it and the next spawn applies ``--model``.
    """
    from omnigent.spec.types import ExecutorSpec

    def _fake_inject(bridge_dir: Any, *, model: str, timeout_s: float) -> None:
        """Simulate the bridge-not-ready path."""
        del bridge_dir, model, timeout_s
        raise RuntimeError("tmux target is not advertised")

    monkeypatch.setattr(cursor_native_bridge, "inject_model_command", _fake_inject)

    native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "cursor-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the cursor-native spec for any agent_id."""
        del agent_id, session_id
        return native_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": "conv_cursor_model_fail", "agent_id": "ag_1"},
        )
        assert create_resp.status_code == 201, create_resp.text

        resp = await client.post(
            "/v1/sessions/conv_cursor_model_fail/events",
            json={"type": "model_change", "model": "gpt-5.2"},
        )

    assert resp.status_code == 503, (
        f"cursor-native model_change with inject failure must return 503; "
        f"got {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    assert body.get("error") == "cursor_native_model_failed", (
        f"503 body must carry the cursor bridge-failure error code; got {body!r}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("effort_value", ["high", "medium", "low", "xhigh", None, ""])
async def test_events_effort_change_on_cursor_native_session_is_disabled_noop(
    effort_value: str | None,
) -> None:
    """
    cursor-native effort switching is intentionally dropped (for now): a model
    switch resets cursor's per-model effort to that model's default, so a web
    effort would silently diverge from the TUI. The dispatch must 204 for ANY
    effort value (cursor-native is excluded from the effort_change gate, and the
    effort injector no longer exists).
    """
    from omnigent.spec.types import ExecutorSpec

    native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "cursor-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the cursor-native spec for any agent_id."""
        del agent_id, session_id
        return native_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": "conv_cursor_effort", "agent_id": "ag_1"},
        )
        assert create_resp.status_code == 201, create_resp.text

        resp = await client.post(
            "/v1/sessions/conv_cursor_effort/events",
            json={"type": "effort_change", "effort": effort_value},
        )

    assert resp.status_code == 204, (
        f"cursor-native effort_change must 204 (disabled); got {resp.status_code}: {resp.text}"
    )


@pytest.mark.asyncio
async def test_auto_create_claude_terminal_registers_permission_hook(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Host-spawned terminal launch wires the PermissionRequest hook.

    The runner's ``_auto_create_claude_terminal`` is the launch path
    used when a claude-native session is created with no CLI client
    present (web-UI sessions, the ``omnigent host`` host API). It
    must pass the Omnigent server URL into ``augment_claude_args`` so
    ``build_hook_settings`` registers the ``PermissionRequest`` command
    hook and writes permission_hook.json. Without it, approval prompts
    silently never reach the web UI even though every other hook is
    present (the regression observed in production: settings carried
    SessionStart/Stop/.../PreCompact + statusLine but no
    PermissionRequest).
    """
    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")

    # The real forwarder opens an HTTP stream to the server; stub it so
    # the auto-create flow runs without network. The created task is
    # scheduled and completes immediately. Capture the kwargs so the
    # test can assert the forwarder gets a refresh-capable auth.
    forwarder_kwargs: dict[str, Any] = {}

    async def _no_op_forwarder(**kwargs: Any) -> None:
        forwarder_kwargs.update(kwargs)

    monkeypatch.setattr(
        "omnigent.claude_native_forwarder.supervise_forwarder",
        _no_op_forwarder,
    )

    captured: dict[str, Any] = {}

    class _FakeResourceRegistry:
        """Captures the launched terminal spec; no live terminal registry."""

        # ``_publish_tmux_target_for_bridge`` early-returns when this is
        # None, so the test doesn't need a real terminal instance.
        terminal_registry = None

        async def launch_required_terminal(
            self,
            *,
            session_id: str,
            terminal_name: str,
            session_key: str,
            spec: Any,
            resource_role: str | None = None,
            parent_os_env: Any = None,
        ) -> SessionResourceView:
            """Record the spec and return a terminal resource view."""
            del terminal_name, session_key
            captured["spec"] = spec
            return SessionResourceView(
                id="terminal_claude_main",
                type="terminal",
                session_id=session_id,
                name="claude:main",
                metadata={"terminal_name": "claude", "session_key": "main", "running": True},
            )

    await _auto_create_claude_terminal(
        "conv_abc",
        _FakeResourceRegistry(),
        lambda _sid, _evt: None,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    spec = captured["spec"]
    assert spec.command == "claude"
    assert spec.env["ENABLE_TOOL_SEARCH"] == "true"
    # The claude-native terminal must opt into keeping its private tmux server
    # alive past an inner-CLI exit, so a sub-agent worker whose `claude` exits
    # early no longer cascades into "no server running" (#540).
    assert spec.keep_alive_after_exit is True
    args = spec.args
    settings = json.loads(args[args.index("--settings") + 1])
    assert "PermissionRequest" in settings["hooks"]
    permission_hook = settings["hooks"]["PermissionRequest"][0]["hooks"][0]
    assert permission_hook["type"] == "command"
    assert "claude_native_hook permission-request" in permission_hook["command"]

    # The hook reads the server URL back out of this file at hook time,
    # so it must be written with the runner's Omnigent server URL.
    config = read_permission_hook_config(bridge_dir_for_bridge_id("conv_abc"))
    assert config["ap_server_url"] == "http://127.0.0.1:8000"

    # The forwarder must get a refresh-capable httpx.Auth (not just a
    # one-shot Authorization header) so a long-running host-spawned
    # session keeps forwarding after the ~1h OAuth token expires.
    # ``_auto_create_claude_terminal`` schedules the forwarder as a task;
    # yield once so the stub records its kwargs before asserting.
    from omnigent.runner._entry import _RunnerDatabricksAuth

    await asyncio.sleep(0)
    assert isinstance(forwarder_kwargs.get("auth"), _RunnerDatabricksAuth)


@pytest.mark.asyncio
async def test_auto_create_pi_terminal_launches_required_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Pi-native auto-create must launch a *required* terminal.

    Regression guard for a missed call site. The pi-native runtime *is* the
    terminal process (parity with claude-native), so when the lifecycle-aware
    launch API replaced ``launch_terminal`` with ``launch_required_terminal`` /
    ``launch_auxiliary_terminal``, ``_auto_create_pi_terminal`` had to move to
    ``launch_required_terminal``. The fake registry below exposes *only*
    ``launch_required_terminal`` (no ``launch_terminal``), so a stale call site
    raises ``AttributeError`` here in CI instead of crashing in production the
    moment a real pi-native session boots.

    :param tmp_path: Pytest-provided temporary directory.
    :param monkeypatch: Pytest monkeypatch fixture.
    """
    import omnigent.pi_native as pi_native
    import omnigent.pi_native_bridge as pi_native_bridge
    import omnigent.pi_native_credentials as pi_native_credentials

    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")
    monkeypatch.setattr(pi_native_bridge, "_BRIDGE_ROOT", tmp_path / "pi-bridge")
    # The lifecycle of the launch — not the binary or credentials — is under
    # test, so neither a real Pi install nor a configured provider is needed.
    monkeypatch.setattr(pi_native, "resolve_pi_executable", lambda: "pi")
    # Accept the ``model`` kwarg the runner now threads through (the spec model
    # → models.json path); None still skips provider injection here.
    monkeypatch.setattr(
        pi_native_credentials, "resolve_pi_native_provider", lambda **_kwargs: None
    )

    # Skip the GET /v1/sessions round-trip: hand the flow a ready launch
    # config pointing at the tmp workspace.
    async def _fake_launch_config(**_kwargs: Any) -> _PiNativeLaunchConfig:
        return _PiNativeLaunchConfig(
            workspace=tmp_path,
            server_url="http://127.0.0.1:8000",
            terminal_launch_args=None,
            external_session_id=None,
        )

    monkeypatch.setattr("omnigent.runner.app._pi_native_launch_config", _fake_launch_config)

    captured: dict[str, Any] = {}

    class _FakeResourceRegistry:
        """Records the launch; exposes ONLY the required-terminal launch API."""

        terminal_registry = None

        async def launch_required_terminal(
            self,
            *,
            session_id: str,
            terminal_name: str,
            session_key: str,
            spec: Any,
            resource_role: str | None = None,
            parent_os_env: Any = None,
        ) -> SessionResourceView:
            """Record the launch and return a terminal resource view."""
            captured["terminal_name"] = terminal_name
            captured["session_key"] = session_key
            captured["resource_role"] = resource_role
            captured["spec"] = spec
            return SessionResourceView(
                id="terminal_pi_main",
                type="terminal",
                session_id=session_id,
                name="pi:main",
                metadata={"terminal_name": "pi", "session_key": "main", "running": True},
            )

    published: list[dict[str, Any]] = []

    await _auto_create_pi_terminal(
        "conv_pi",
        _FakeResourceRegistry(),  # type: ignore[arg-type]
        lambda _sid, evt: published.append(evt),
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    # Required lifecycle (parity with claude-native), correct terminal identity.
    assert captured["terminal_name"] == "pi"
    assert captured["session_key"] == "main"
    assert captured["resource_role"] == PI_NATIVE_TERMINAL_ROLE
    assert captured["spec"].command == "pi"
    # The fresh terminal is surfaced on the live stream for the Terminal toggle.
    assert any(evt.get("type") == "session.resource.created" for evt in published)


@pytest.mark.asyncio
async def test_auto_create_kiro_terminal_launches_required_terminal_with_isolated_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kiro-native auto-create launches the TUI and session forwarder."""
    import omnigent.kiro_native as kiro_native

    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:6767")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setattr(kiro_native_bridge, "_BRIDGE_ROOT", tmp_path / "kiro-bridge")
    monkeypatch.setattr(
        kiro_native,
        "resolve_kiro_executable",
        lambda **_kwargs: "/usr/bin/kiro-cli",
    )
    forwarder_calls: list[dict[str, Any]] = []

    async def _fake_supervise_kiro_session_forwarder(**kwargs: Any) -> None:
        forwarder_calls.append(kwargs)

    monkeypatch.setattr(
        "omnigent.kiro_native_session_forwarder.supervise_kiro_session_forwarder",
        _fake_supervise_kiro_session_forwarder,
    )

    async def _fake_launch_config(**_kwargs: Any) -> _KiroNativeLaunchConfig:
        return _KiroNativeLaunchConfig(
            workspace=tmp_path,
            terminal_launch_args=["--model", "auto", "--effort", "high", "hello"],
            external_session_id="kiro-session-123",
        )

    monkeypatch.setattr("omnigent.runner.app._kiro_native_launch_config", _fake_launch_config)

    captured: dict[str, Any] = {}

    class _FakeResourceRegistry:
        """Records the launch; exposes ONLY the required-terminal launch API."""

        terminal_registry = None

        async def launch_required_terminal(
            self,
            *,
            session_id: str,
            terminal_name: str,
            session_key: str,
            spec: Any,
            resource_role: str | None = None,
            parent_os_env: Any = None,
        ) -> SessionResourceView:
            del parent_os_env
            captured["terminal_name"] = terminal_name
            captured["session_key"] = session_key
            captured["resource_role"] = resource_role
            captured["spec"] = spec
            return SessionResourceView(
                id="terminal_kiro_main",
                type="terminal",
                session_id=session_id,
                name="kiro:main",
                metadata={"terminal_name": "kiro", "session_key": "main", "running": True},
            )

    published: list[dict[str, Any]] = []

    await _auto_create_kiro_terminal(
        "conv_kiro",
        _FakeResourceRegistry(),  # type: ignore[arg-type]
        lambda _sid, evt: published.append(evt),
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    await asyncio.sleep(0)

    spec = captured["spec"]
    assert captured["terminal_name"] == "kiro"
    assert captured["session_key"] == "main"
    assert captured["resource_role"] == KIRO_NATIVE_TERMINAL_ROLE
    assert spec.command == "/usr/bin/kiro-cli"
    assert spec.args == [
        "chat",
        "--tui",
        "--resume-id",
        "kiro-session-123",
        "--model",
        "auto",
        "--effort",
        "high",
        "hello",
    ]
    assert spec.inherit_env is False
    assert "OPENAI_API_KEY" not in spec.env
    assert "OPENAI_API_KEY" in spec.env_unset
    assert spec.env[kiro_native_bridge.KIRO_NATIVE_BRIDGE_DIR_ENV_VAR] == str(
        kiro_native_bridge.bridge_dir_for_session_id("conv_kiro")
    )
    assert any(evt.get("type") == "session.resource.created" for evt in published)
    assert forwarder_calls
    assert forwarder_calls[0]["base_url"] == "http://127.0.0.1:6767"
    assert forwarder_calls[0]["session_id"] == "conv_kiro"
    assert forwarder_calls[0]["agent_name"] == "kiro-native-ui"
    assert forwarder_calls[0]["workspace"] == str(tmp_path)


@pytest.mark.asyncio
async def test_auto_create_pi_terminal_inherits_agent_sandbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Pi-native auto-create must honour the agent's ``os_env.sandbox``.

    Regression for the sandbox-override bug: the pi-native auto-create path
    built a fresh ``TerminalEnvSpec`` whose ``os_env`` carried no ``sandbox``
    and passed no ``parent_os_env``, so ``launch_required_terminal`` fell back
    to ``_default_sandbox_for_platform`` (``linux_bwrap`` on Linux) and ignored
    the agent YAML.  A pi-native agent that declares
    ``os_env.sandbox.type: none`` was wrongly forced into bwrap, which failed
    on a hardened host.

    Asserts the launched terminal spec's ``os_env.sandbox`` is the agent's
    ``none`` sandbox (not the platform default) and that the agent's ``os_env``
    is threaded through as the launch ``parent_os_env`` so the rest of the
    policy (egress_rules / env_passthrough) is also inherited.

    :param tmp_path: Pytest-provided temporary directory.
    :param monkeypatch: Pytest monkeypatch fixture.
    """
    import omnigent.pi_native as pi_native
    import omnigent.pi_native_bridge as pi_native_bridge
    import omnigent.pi_native_credentials as pi_native_credentials
    from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec

    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")
    monkeypatch.setattr(pi_native_bridge, "_BRIDGE_ROOT", tmp_path / "pi-bridge")
    monkeypatch.setattr(pi_native, "resolve_pi_executable", lambda: "pi")
    # Accept the ``model`` kwarg the runner now threads through (the spec model
    # → models.json path); None still skips provider injection here.
    monkeypatch.setattr(
        pi_native_credentials, "resolve_pi_native_provider", lambda **_kwargs: None
    )

    async def _fake_launch_config(**_kwargs: Any) -> _PiNativeLaunchConfig:
        return _PiNativeLaunchConfig(
            workspace=tmp_path,
            server_url="http://127.0.0.1:8000",
            terminal_launch_args=None,
            external_session_id=None,
        )

    monkeypatch.setattr("omnigent.runner.app._pi_native_launch_config", _fake_launch_config)

    captured: dict[str, Any] = {}

    class _FakeResourceRegistry:
        """Captures the launched terminal spec and parent os_env."""

        terminal_registry = None

        async def launch_required_terminal(
            self,
            *,
            session_id: str,
            terminal_name: str,
            session_key: str,
            spec: Any,
            resource_role: str | None = None,
            parent_os_env: Any = None,
        ) -> SessionResourceView:
            """Record the spec + parent_os_env and return a resource view."""
            del terminal_name, session_key
            captured["spec"] = spec
            captured["parent_os_env"] = parent_os_env
            return SessionResourceView(
                id="terminal_pi_main",
                type="terminal",
                session_id=session_id,
                name="pi:main",
                metadata={"terminal_name": "pi", "session_key": "main", "running": True},
            )

    # An agent that declares sandbox: none (runs unconfined; outer
    # container/VM is the security boundary).
    agent_os_env = OSEnvSpec(
        type="caller_process",
        cwd=".",
        sandbox=OSEnvSandboxSpec(type="none"),
    )
    agent_spec = AgentSpec(
        spec_version=1,
        name="pi_code",
        executor=ExecutorSpec(
            type="omnigent",
            config={"harness": "pi-native", "model": "pi-default"},
        ),
        os_env=agent_os_env,
    )

    await _auto_create_pi_terminal(
        "conv_pi_sandbox_none",
        _FakeResourceRegistry(),  # type: ignore[arg-type]
        lambda _sid, _evt: None,
        server_client=NullServerClient(),  # type: ignore[arg-type]
        agent_spec=agent_spec,
    )

    launched_sandbox = captured["spec"].os_env.sandbox
    assert launched_sandbox is not None, (
        "auto-create dropped the agent's sandbox; launch_required_terminal will "
        "fall back to _default_sandbox_for_platform (bwrap), overriding sandbox: none"
    )
    assert launched_sandbox.type == "none"
    # The whole os_env is threaded through as the inheritance parent.
    assert captured["parent_os_env"] is agent_os_env


@pytest.mark.asyncio
async def test_auto_create_claude_terminal_passes_session_effort(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Host-spawned terminal launch reads session effort and passes ``--effort``.

    When the Omnigent server returns a session with a persisted
    ``reasoning_effort``, the auto-create path must include
    ``--effort <value>`` in the Claude CLI args so the terminal
    starts at the user's chosen effort level.

    :param tmp_path: Pytest-provided temporary directory.
    :param monkeypatch: Pytest monkeypatch fixture.
    """
    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")

    async def _no_op_forwarder(**kwargs: Any) -> None:
        del kwargs

    monkeypatch.setattr(
        "omnigent.claude_native_forwarder.supervise_forwarder",
        _no_op_forwarder,
    )

    captured: dict[str, Any] = {}

    class _FakeResourceRegistry:
        """Captures the launched terminal spec."""

        terminal_registry = None

        async def launch_required_terminal(
            self,
            *,
            session_id: str,
            terminal_name: str,
            session_key: str,
            spec: Any,
            resource_role: str | None = None,
            parent_os_env: Any = None,
        ) -> SessionResourceView:
            """Record the spec and return a terminal resource view."""
            del terminal_name, session_key
            captured["spec"] = spec
            return SessionResourceView(
                id="terminal_claude_main",
                type="terminal",
                session_id=session_id,
                name="claude:main",
                metadata={"terminal_name": "claude", "session_key": "main", "running": True},
            )

    # Fake Omnigent server client that returns a session with reasoning_effort.
    fake_client = httpx.AsyncClient(
        base_url="http://test-server",
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200,
                json={"reasoning_effort": "high", "labels": {}},
            )
        ),
    )

    await _auto_create_claude_terminal(
        "conv_effort",
        _FakeResourceRegistry(),
        lambda _sid, _evt: None,
        server_client=fake_client,
    )

    args = captured["spec"].args
    assert "--effort" in args
    effort_idx = args.index("--effort")
    assert args[effort_idx + 1] == "high"

    await fake_client.aclose()


def test_agent_os_env_from_spec_unwraps_resolved_and_handles_none() -> None:
    """
    ``_agent_os_env_from_spec`` reads ``os_env`` through the resolved wrapper.

    The auto-create terminals receive either a bare ``AgentSpec`` or a
    ``ResolvedSpec`` wrapping one (the codex path passes ``ResolvedSpec``).
    The helper must unwrap the latter and return the inner ``os_env``, and
    return ``None`` when there is no spec — so the launch falls back to the
    platform default only when there is genuinely no agent policy to honour.
    """
    from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec

    os_env = OSEnvSpec(type="caller_process", cwd=".", sandbox=OSEnvSandboxSpec(type="none"))
    bare = AgentSpec(
        spec_version=1,
        name="agent",
        executor=ExecutorSpec(type="omnigent", config={}),
        os_env=os_env,
    )

    # Bare AgentSpec: returned directly.
    assert _agent_os_env_from_spec(bare) is os_env
    # ResolvedSpec wrapper: unwrapped to the inner spec's os_env.
    assert _agent_os_env_from_spec(ResolvedSpec(spec=bare, workdir=None)) is os_env
    # No spec at all: None (caller then uses the platform default).
    assert _agent_os_env_from_spec(None) is None
    # AgentSpec without an os_env block: None.
    no_os_env = AgentSpec(
        spec_version=1,
        name="agent",
        executor=ExecutorSpec(type="omnigent", config={}),
    )
    assert _agent_os_env_from_spec(no_os_env) is None


@pytest.mark.asyncio
async def test_auto_create_claude_terminal_inherits_agent_sandbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Host-spawned Claude terminal honours the agent's ``os_env.sandbox``.

    Regression for the sandbox-override bug: the auto-create path built a
    fresh ``TerminalEnvSpec`` whose ``os_env`` carried no ``sandbox`` and
    passed no ``parent_os_env``, so ``launch_terminal`` fell back to
    ``_default_sandbox_for_platform`` (``linux_bwrap`` / ``darwin_seatbelt``)
    and ignored the agent YAML. A claude-native agent that declares
    ``os_env.sandbox.type: none`` (e.g. Polly's ``claude_code`` worker,
    which relies on the outer container/VM as the boundary) was wrongly
    forced into bwrap, which then failed to start in a hardened container.

    Asserts the launched terminal spec's ``os_env.sandbox`` is the agent's
    ``none`` sandbox (not the platform default) and that the agent's
    ``os_env`` is threaded through as the launch ``parent_os_env`` so the
    rest of the policy (egress_rules / env_passthrough) is inherited too.

    :param tmp_path: Pytest-provided temporary directory.
    :param monkeypatch: Pytest monkeypatch fixture.
    """
    from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec

    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")

    async def _no_op_forwarder(**kwargs: Any) -> None:
        del kwargs

    monkeypatch.setattr(
        "omnigent.claude_native_forwarder.supervise_forwarder",
        _no_op_forwarder,
    )

    captured: dict[str, Any] = {}

    class _FakeResourceRegistry:
        """Captures the launched terminal spec and parent os_env."""

        terminal_registry = None

        async def launch_required_terminal(
            self,
            *,
            session_id: str,
            terminal_name: str,
            session_key: str,
            spec: Any,
            resource_role: str | None = None,
            parent_os_env: Any = None,
        ) -> SessionResourceView:
            """Record the spec + parent_os_env and return a resource view."""
            del terminal_name, session_key
            captured["spec"] = spec
            captured["parent_os_env"] = parent_os_env
            return SessionResourceView(
                id="terminal_claude_main",
                type="terminal",
                session_id=session_id,
                name="claude:main",
                metadata={"terminal_name": "claude", "session_key": "main", "running": True},
            )

    fake_client = httpx.AsyncClient(
        base_url="http://test-server",
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json={"labels": {}}),
        ),
    )

    # An agent that declares sandbox: none (runs unconfined; the outer
    # container/VM is the boundary) — exactly Polly's coding sub-agents.
    agent_os_env = OSEnvSpec(
        type="caller_process",
        cwd=".",
        sandbox=OSEnvSandboxSpec(type="none"),
    )
    agent_spec = AgentSpec(
        spec_version=1,
        name="claude_code",
        executor=ExecutorSpec(
            type="omnigent",
            config={"harness": "claude-native", "model": "claude-default"},
        ),
        os_env=agent_os_env,
    )

    await _auto_create_claude_terminal(
        "conv_sandbox_none",
        _FakeResourceRegistry(),
        lambda _sid, _evt: None,
        server_client=fake_client,
        agent_spec=agent_spec,
    )

    launched_sandbox = captured["spec"].os_env.sandbox
    assert launched_sandbox is not None, (
        "auto-create dropped the agent's sandbox; launch_terminal will fall "
        "back to _default_sandbox_for_platform (bwrap), overriding sandbox: none"
    )
    assert launched_sandbox.type == "none"
    # The whole os_env is threaded through as the inheritance parent.
    assert captured["parent_os_env"] is agent_os_env

    await fake_client.aclose()


@pytest.mark.asyncio
async def test_auto_create_claude_terminal_injects_ucode_gateway_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Host-spawned launch injects the ucode Databricks gateway config.

    On the daemon / web-UI path the runner — not the CLI — launches
    Claude, so it must reproduce the gateway auth the CLI normally
    injects: the ``ANTHROPIC_BASE_URL`` env, the ``apiKeyHelper`` token
    command, and the gateway default model. The runner derives this from
    the user's provider config (here the legacy global ``auth:`` block —
    the ambient ``DATABRICKS_CONFIG_PROFILE`` env var deliberately no
    longer steers credentials). Without it, Claude would launch with
    empty env and no token and could not reach the Databricks model —
    the exact regression that blocked daemon-routing.

    :param tmp_path: Pytest-provided temporary directory.
    :param monkeypatch: Pytest monkeypatch fixture.
    """
    from omnigent.claude_native import ClaudeNativeUcodeConfig

    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")
    # The supported credential source for a host-spawned runner: the
    # global config's ``auth:`` block (written by ``omnigent setup``),
    # isolated to a temp config home so the developer's real config
    # can't leak in.
    config_home = tmp_path / "config-home"
    config_home.mkdir()
    (config_home / "config.yaml").write_text(
        "auth:\n  type: databricks\n  profile: test-profile\n"
    )
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(config_home))

    async def _no_op_forwarder(**kwargs: Any) -> None:
        del kwargs

    monkeypatch.setattr(
        "omnigent.claude_native_forwarder.supervise_forwarder",
        _no_op_forwarder,
    )

    gateway_env = {"ANTHROPIC_BASE_URL": "https://gw.example/anthropic"}
    ucode = ClaudeNativeUcodeConfig(
        env=dict(gateway_env),
        api_key_helper="databricks auth token --fake-helper",
        model="databricks-claude-opus-4-7",
    )
    # The runner imports ``_ucode_config_for_profile`` from
    # ``omnigent.claude_native`` per call, so patch it at the source.
    monkeypatch.setattr(
        "omnigent.claude_native._ucode_config_for_profile",
        lambda profile: ucode,
    )

    captured: dict[str, Any] = {}

    class _FakeResourceRegistry:
        """Captures the launched terminal spec."""

        terminal_registry = None

        async def launch_required_terminal(
            self,
            *,
            session_id: str,
            terminal_name: str,
            session_key: str,
            spec: Any,
            resource_role: str | None = None,
            parent_os_env: Any = None,
        ) -> SessionResourceView:
            """Record the spec and return a terminal resource view."""
            del terminal_name, session_key
            captured["spec"] = spec
            return SessionResourceView(
                id="terminal_claude_main",
                type="terminal",
                session_id=session_id,
                name="claude:main",
                metadata={"terminal_name": "claude", "session_key": "main", "running": True},
            )

    fake_client = httpx.AsyncClient(
        base_url="http://test-server",
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json={"labels": {}}),
        ),
    )

    await _auto_create_claude_terminal(
        "conv_ucode",
        _FakeResourceRegistry(),
        lambda _sid, _evt: None,
        server_client=fake_client,
    )

    spec = captured["spec"]
    # The gateway env points ``claude`` at the Databricks gateway, and
    # ENABLE_TOOL_SEARCH forces Claude Code to defer MCP tool schemas
    # instead of loading all 200+ bridge tools into startup context.
    assert spec.env == {
        **gateway_env,
        "ENABLE_TOOL_SEARCH": "true",
        "CLAUDE_CODE_DISABLE_AGENT_VIEW": "1",
    }
    assert spec.command == "claude"
    # The gateway default model is applied (no per-session override here).
    assert "--model" in spec.args
    assert spec.args[spec.args.index("--model") + 1] == "databricks-claude-opus-4-7"
    # The apiKeyHelper threaded into the Claude settings augment so the
    # gateway token command is registered.
    assert "databricks auth token --fake-helper" in " ".join(spec.args)

    await fake_client.aclose()


async def _run_auto_create_cursor_terminal(
    *,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_spec: AgentSpec | None,
    terminal_launch_args: list[str] | None,
) -> Any:
    """Drive ``_auto_create_cursor_terminal`` and return the captured launch spec.

    Stubs the cursor-agent binary lookup, the transcript forwarder, and the
    runner auth factory so the model-injection branch runs without a real
    ``cursor-agent`` install or Databricks credentials. The session snapshot
    (workspace + ``terminal_launch_args``) is served from an in-memory
    ``httpx.MockTransport``, and a fake registry records the ``spec`` passed to
    ``launch_required_terminal`` so the test can assert on ``spec.args``.
    """
    from omnigent.runner import _entry as _runner_entry

    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setattr(cursor_native_bridge, "_BRIDGE_ROOT", tmp_path / "cursor-bridge")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")
    monkeypatch.setattr("omnigent.cursor_native.resolve_cursor_executable", lambda: "cursor-agent")

    async def _no_op_forwarder(**kwargs: Any) -> None:
        del kwargs

    monkeypatch.setattr(
        "omnigent.cursor_native_forwarder.supervise_cursor_forwarder",
        _no_op_forwarder,
    )
    # The forwarder is stubbed, so the auth it would carry is never used — keep
    # the factory from reaching for ambient Databricks credentials in tests.
    monkeypatch.setattr(_runner_entry, "_make_auth_token_factory", lambda *a, **k: None)

    captured: dict[str, Any] = {}

    class _FakeResourceRegistry:
        """Captures the launched terminal spec; no real terminal registry."""

        terminal_registry = None

        async def launch_required_terminal(
            self,
            *,
            session_id: str,
            terminal_name: str,
            session_key: str,
            spec: Any,
            resource_role: str | None = None,
            parent_os_env: Any = None,
        ) -> SessionResourceView:
            """Record the spec and return a terminal resource view."""
            del terminal_name, session_key, resource_role, parent_os_env
            captured["spec"] = spec
            return SessionResourceView(
                id="terminal_cursor_main",
                type="terminal",
                session_id=session_id,
                name="cursor:main",
                metadata={"terminal_name": "cursor", "session_key": "main", "running": True},
            )

    snapshot = {"workspace": str(workspace), "terminal_launch_args": terminal_launch_args}
    fake_client = httpx.AsyncClient(
        base_url="http://test-server",
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json=snapshot)),
    )
    try:
        await _auto_create_cursor_terminal(
            "conv_cursor_model",
            _FakeResourceRegistry(),  # type: ignore[arg-type]
            lambda _sid, _evt: None,
            server_client=fake_client,
            ensure_comment_relay=None,
            agent_spec=agent_spec,
        )
    finally:
        await fake_client.aclose()
    return captured["spec"]


@pytest.mark.asyncio
async def test_auto_create_cursor_terminal_injects_spec_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spec-pinned cursor model is threaded into the cursor-agent launch args.

    The web-UI / daemon path launches ``cursor-agent`` from the runner, so the
    session's ``executor.model`` (from ``--model`` or config.yaml ``model:``)
    must reach the TUI as ``--model <id>`` — the regression #933 fixes.
    """
    spec = await _run_auto_create_cursor_terminal(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        agent_spec=AgentSpec(
            spec_version=1, name="cursor", executor=ExecutorSpec(model="sonnet-4-thinking")
        ),
        terminal_launch_args=None,
    )
    assert spec.command == "cursor-agent"
    assert "--model" in spec.args
    assert spec.args[spec.args.index("--model") + 1] == "sonnet-4-thinking"


@pytest.mark.parametrize(
    "passthrough",
    [
        # Both the split (``-- --model X``) and joined (``--model=X``) forms a
        # user can pass through must suppress injection — otherwise cursor-agent
        # sees two ``--model`` values and selection is ambiguous.
        ["--model", "gpt-5"],
        ["--model=gpt-5"],
        ["-m", "gpt-5"],
    ],
    ids=["split", "joined", "short"],
)
@pytest.mark.asyncio
async def test_auto_create_cursor_terminal_user_model_wins(
    passthrough: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A user-pinned passthrough model wins; the spec model is not injected."""
    spec = await _run_auto_create_cursor_terminal(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        agent_spec=AgentSpec(
            spec_version=1, name="cursor", executor=ExecutorSpec(model="sonnet-4-thinking")
        ),
        terminal_launch_args=passthrough,
    )
    # Exactly the user's args survive — no second ``--model`` / spec model added.
    assert spec.args.count("--model") == passthrough.count("--model")
    assert "sonnet-4-thinking" not in spec.args


@pytest.mark.parametrize(
    "spec_model",
    [None, "", "databricks-claude-opus", "databricks/claude-opus"],
    ids=["none", "empty", "databricks-dash", "databricks-slash"],
)
@pytest.mark.asyncio
async def test_auto_create_cursor_terminal_omits_model_when_unusable(
    spec_model: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No usable cursor model id → no ``--model`` (cursor-agent keeps its default).

    Gateway-routed ``databricks-*`` ids are not cursor-agent model ids, so they
    are dropped rather than passed through (which would error on launch).
    """
    spec = await _run_auto_create_cursor_terminal(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        agent_spec=AgentSpec(
            spec_version=1, name="cursor", executor=ExecutorSpec(model=spec_model)
        ),
        terminal_launch_args=None,
    )
    assert "--model" not in spec.args


@pytest.mark.parametrize(
    ("snapshot_external_id", "expected_start_at_end"),
    [
        ("02857840-6362-408f-b41f-309e396ed7c6", True),
        (None, False),
    ],
)
@pytest.mark.asyncio
async def test_auto_create_claude_terminal_forwarder_skips_replayed_transcript_on_resume(
    snapshot_external_id: str | None,
    expected_start_at_end: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Host-spawned resume starts the forwarder past the replayed transcript.

    On cold resume the runner synthesizes Claude's local transcript from
    AP's committed history and launches ``claude --resume``, so the
    transcript file already holds every item Omnigent has at offset 0. The
    forwarder must therefore start at the transcript end
    (``start_at_end=True``); starting at offset 0 would re-post the whole
    history as new ``external_conversation_item`` records — which carry no
    server-side dedup — duplicating the visible conversation on every
    resume. A fresh session has no ``--resume`` and an empty
    transcript, so it must forward from the beginning
    (``start_at_end=False``). This mirrors the CLI client's
    ``prepared.cold_resumed`` handling in ``claude_native.py``.

    :param snapshot_external_id: ``external_session_id`` returned in the AP
        session snapshot, e.g.
        ``"02857840-6362-408f-b41f-309e396ed7c6"`` for a resume, or
        ``None`` for a fresh session.
    :param expected_start_at_end: The ``start_at_end`` the forwarder must
        be launched with for that snapshot.
    :param tmp_path: Pytest-provided temporary directory.
    :param monkeypatch: Pytest monkeypatch fixture.
    """
    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(tmp_path / "workspace"))
    # Pin the launch config to Claude's native auth so the test does not
    # depend on the runner process's ambient Databricks profile.
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)

    forwarder_kwargs: dict[str, Any] = {}

    async def _capture_forwarder(**kwargs: Any) -> None:
        """Record the forwarder launch kwargs without opening a stream."""
        forwarder_kwargs.update(kwargs)

    monkeypatch.setattr(
        "omnigent.claude_native_forwarder.supervise_forwarder",
        _capture_forwarder,
    )

    # Transcript synthesis from Omnigent history has its own coverage; stub it to
    # return a path so the resume branch sets ``resume_external_session_id``
    # without a real item fetch. A non-None return mirrors the production
    # contract: it means ``--resume`` will be passed, which is precisely the
    # condition that makes the replayed transcript hazard real.
    synth_calls: list[str] = []

    async def _fake_synth(
        client: Any,
        *,
        session_id: str,
        external_session_id: str,
        workspace: Path,
    ) -> Path:
        """Record the resume id and return a transcript path."""
        del client, session_id, workspace
        synth_calls.append(external_session_id)
        return tmp_path / f"{external_session_id}.jsonl"

    monkeypatch.setattr(
        "omnigent.claude_native._ensure_local_claude_resume_transcript",
        _fake_synth,
    )

    snapshot: dict[str, Any] = {}
    if snapshot_external_id is not None:
        snapshot["external_session_id"] = snapshot_external_id

    class _SnapshotServerClient(NullServerClient):
        """Server client whose session snapshot carries the resume id."""

        async def get(self, url: str, **kwargs: Any) -> NullServerClient._Response:
            """Return the session snapshot used to derive resume args."""
            del kwargs
            assert url == "/v1/sessions/conv_resume"

            class _SnapResponse(NullServerClient._Response):
                """Snapshot response carrying the parametrized resume id."""

                def json(self) -> dict[str, Any]:
                    """Return the session snapshot dict."""
                    return snapshot

            return _SnapResponse()

    class _FakeResourceRegistry:
        """Resource registry that returns a terminal without launching."""

        terminal_registry = None

        async def launch_required_terminal(
            self,
            *,
            session_id: str,
            terminal_name: str,
            session_key: str,
            spec: Any,
            resource_role: str | None = None,
            parent_os_env: Any = None,
        ) -> SessionResourceView:
            """Return a terminal resource view without spawning a TTY."""
            del terminal_name, session_key, spec
            return SessionResourceView(
                id="terminal_claude_main",
                type="terminal",
                session_id=session_id,
                name="claude:main",
                metadata={"terminal_name": "claude", "session_key": "main", "running": True},
            )

    await _auto_create_claude_terminal(
        "conv_resume",
        _FakeResourceRegistry(),
        lambda _sid, _evt: None,
        server_client=_SnapshotServerClient(),  # type: ignore[arg-type]
    )

    # The forwarder runs as a scheduled task; yield so the stub records its
    # kwargs before asserting.
    await asyncio.sleep(0)

    # Crux of the fix: resume skips the replayed transcript, a fresh session
    # forwards from the start. A regression to the old hardcoded
    # ``start_at_end=False`` flips the resume case False and reintroduces
    # the duplicate-history bug; the ``is`` comparison also fails if a
    # truthy non-bool leaks through.
    assert forwarder_kwargs.get("start_at_end") is expected_start_at_end, (
        f"forwarder start_at_end={forwarder_kwargs.get('start_at_end')!r}; "
        f"expected {expected_start_at_end!r} for external_session_id="
        f"{snapshot_external_id!r}. False on resume means the whole "
        f"transcript is re-posted."
    )

    # ``start_at_end`` must be correct *because* the resume branch ran, not
    # by coincidence: synthesis happens exactly when (and only when) the
    # snapshot carried an external session id.
    if snapshot_external_id is None:
        assert synth_calls == []
    else:
        assert synth_calls == [snapshot_external_id]


def _drain_session_event_queue(queue: asyncio.Queue[Any] | None) -> list[dict[str, Any]]:
    """
    Drain and return every dict item currently on a runner session queue.

    Used by the native control-event tests to clear creation-time events
    (e.g. the ``session.terminal_pending`` pair the claude-native
    auto-create path enqueues) so a later drain isolates only the events
    a specific control signal produced.

    :param queue: The per-session event queue from
        ``_session_event_queues_ref``, or ``None`` when the session has
        no queue (already deleted / never created).
    :returns: The dict items drained, in FIFO order. Empty when the
        queue is ``None`` or held only non-dict sentinels.
    """
    drained: list[dict[str, Any]] = []
    if queue is None:
        return drained
    while not queue.empty():
        item = queue.get_nowait()
        if isinstance(item, dict):
            drained.append(item)
    return drained


@dataclass
class _PublishedEvent:
    """
    One event captured from the runner's per-session publisher.

    :param session_id: Routing session id the event was published under,
        e.g. ``"conv_emit"``.
    :param event: The published SSE event dict.
    """

    session_id: str
    event: dict[str, Any]


@pytest.mark.asyncio
async def test_auto_create_claude_terminal_emits_resource_created_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Host-spawned terminal launch publishes a live ``session.resource.created``.

    The web UI sources its terminal list purely from SSE
    ``session.resource.created`` events, so the auto-create path must
    emit one (the agent-tool / REST launch paths already do via
    ``_emit_terminal_resource_event``). Without it the Terminal toggle
    stays gray until a refresh re-lists terminals via
    snapshot-on-connect.
    """
    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")

    async def _no_op_forwarder(**kwargs: Any) -> None:
        del kwargs

    monkeypatch.setattr(
        "omnigent.claude_native_forwarder.supervise_forwarder",
        _no_op_forwarder,
    )

    class _ViewResourceRegistry:
        """Returns a terminal resource view; no live terminal registry."""

        # ``_publish_tmux_target_for_bridge`` early-returns when this is None.
        terminal_registry = None

        async def launch_required_terminal(
            self,
            *,
            session_id: str,
            terminal_name: str,
            session_key: str,
            spec: Any,
            resource_role: str | None = None,
            parent_os_env: Any = None,
        ) -> SessionResourceView:
            """Return the terminal resource view the runner would launch."""
            del spec
            return SessionResourceView(
                id="terminal_claude_main",
                type="terminal",
                session_id=session_id,
                name=f"{terminal_name}:{session_key}",
                metadata={
                    "terminal_name": terminal_name,
                    "session_key": session_key,
                    "running": True,
                },
            )

    published: list[_PublishedEvent] = []

    def _capture(session_id: str, event: dict[str, Any]) -> None:
        published.append(_PublishedEvent(session_id=session_id, event=event))

    await _auto_create_claude_terminal(
        "conv_emit",
        _ViewResourceRegistry(),
        _capture,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    # Exactly one session.resource.created for the auto-created terminal.
    # 0 means the live publish was dropped (the bug: the toggle would stay
    # gray until a refresh triggers snapshot-on-connect).
    created = [p for p in published if p.event.get("type") == "session.resource.created"]
    assert len(created) == 1, (
        f"auto-create must publish exactly one session.resource.created; got {published}"
    )
    # Routed under the session id so the Omnigent relay forwards it to that
    # session's web stream.
    assert created[0].session_id == "conv_emit"
    resource = created[0].event["resource"]
    assert resource["type"] == "terminal"
    assert resource["id"] == "terminal_claude_main"
    # metadata.running is what the web rail reads for the live terminal.
    assert resource["metadata"]["running"] is True


def test_publish_terminal_pending_emits_pending_then_clear() -> None:
    """
    ``_publish_terminal_pending`` emits the wire shape the Omnigent relay
    consumes for the Terminal-pill spinner.

    The session-creation handler calls this with ``True`` before
    auto-creating a terminal-first session's terminal and ``False`` in a
    ``finally`` (so a failed launch also clears the spinner). The AP
    relay matches on ``type == "session.terminal_pending"`` and reads
    the ``pending`` flag, so both fields must be present and correct, or
    the spinner would never appear (or never clear).
    """
    published: list[_PublishedEvent] = []

    def _capture(session_id: str, event: dict[str, Any]) -> None:
        published.append(_PublishedEvent(session_id=session_id, event=event))

    _publish_terminal_pending(_capture, "conv_pending", True)
    _publish_terminal_pending(_capture, "conv_pending", False)

    assert [p.event for p in published] == [
        {"type": "session.terminal_pending", "pending": True},
        {"type": "session.terminal_pending", "pending": False},
    ]
    # Routed under the session id so the Omnigent relay forwards it to that
    # session's web stream.
    assert all(p.session_id == "conv_pending" for p in published)


def test_publish_native_terminal_start_error_emits_failed_status_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    Native terminal startup failure publishes a generic ``failed`` status.

    The runner must stay alive when terminal auto-create fails, but the
    affected session should only receive ``session.status: failed`` from
    this startup path. A bare ``response.error`` is turn-scoped; if the
    runner publishes one here, Omnigent can persist an orphan transcript error
    and then publish/persist a second error when the user message
    fast-fails against the same terminal.

    The published/returned message is a fixed, client-safe string — the raw
    exception text (which can embed paths/CLI details) is logged for
    operators, not surfaced on the session stream.

    :param caplog: Pytest log capture fixture, used to confirm the raw
        cause is logged server-side.
    """
    published: list[_PublishedEvent] = []

    def _capture(session_id: str, event: dict[str, Any]) -> None:
        published.append(_PublishedEvent(session_id=session_id, event=event))

    with caplog.at_level(logging.WARNING):
        error = _publish_native_terminal_start_error(
            _capture,
            "conv_codex",
            "Codex",
            ImportError("Native Codex requires the 'codex' CLI on PATH."),
        )

    # Generic, client-safe payload — no raw exception text.
    assert error == {
        "code": "native_terminal_start_failed",
        "message": "Native Codex terminal failed to start; see runner logs for details.",
    }
    # The raw cause must NOT leak into the surfaced message, but MUST be
    # logged for operators. If this fails, the redaction regressed (raw
    # text back in the payload) or the server-side log was dropped.
    assert "requires the 'codex' CLI" not in error["message"]
    assert "requires the 'codex' CLI on PATH." in caplog.text
    assert [p.event for p in published] == [
        {
            "type": "session.status",
            "status": "failed",
            "error": error,
        },
    ]
    assert all(p.session_id == "conv_codex" for p in published)


def test_terminal_lookup_miss_log_explains_stopped_registered_terminal(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    Terminal GET miss logs identify a stopped registered terminal.

    The CLI polls ``GET /resources/terminals/terminal_claude_main`` while
    waiting to attach. If a tmux pane was registered and then failed the
    liveness probe, the runner log must say that instead of looking like
    auto-create never ran.

    :param tmp_path: Temporary directory for the fake tmux socket path.
    :param caplog: Pytest log capture fixture.
    :returns: None.
    """
    terminal_registry = TerminalRegistry()
    instance = TerminalInstance(
        name="claude",
        session_key="main",
        socket_path=tmp_path / "tmux.sock",
        private_dir=tmp_path,
        running=False,
    )
    terminal_registry._by_conversation["conv_lookup"] = {("claude", "main"): instance}
    resource_registry = SessionResourceRegistry(terminal_registry=terminal_registry)

    _terminal_lookup_miss_log_state.clear()
    try:
        with caplog.at_level(logging.INFO, logger="omnigent.runner.app"):
            _log_terminal_lookup_miss(
                resource_registry,
                "conv_lookup",
                "terminal_claude_main",
            )
            _log_terminal_lookup_miss(
                resource_registry,
                "conv_lookup",
                "terminal_claude_main",
            )
    finally:
        _terminal_lookup_miss_log_state.clear()

    messages = [
        record.getMessage()
        for record in caplog.records
        if "Terminal resource lookup miss" in record.getMessage()
    ]
    assert len(messages) == 1, (
        f"lookup miss logging should be throttled per reason; got {messages!r}"
    )
    assert "terminal_registered_but_not_running" in messages[0]
    assert "terminal_claude_main" in messages[0]


@pytest.mark.asyncio
async def test_auto_create_claude_terminal_resets_stale_bridge_id_label(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Auto-create corrects a stale ``bridge_id`` label on the Omnigent session.

    If a prior rotation left ``BRIDGE_ID_LABEL_KEY`` set to an older
    bridge id (e.g. ``"m0-bridge_from_prior_rotation"``),
    ``_auto_create_claude_terminal`` must PATCH the label to
    ``session_id`` before proceeding.  Without the correction,
    ``_ensure_comment_relay_started`` would later read the stale label
    and write ``tool_relay.json`` into the wrong bridge dir — the bridge
    MCP subprocess would never see it and the relay tools
    (``list_comments``, ``sys_session_list``, etc.) would be absent.
    """
    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")

    async def _no_op_forwarder(**kwargs: Any) -> None:
        del kwargs

    monkeypatch.setattr(
        "omnigent.claude_native_forwarder.supervise_forwarder",
        _no_op_forwarder,
    )

    class _FakeResourceRegistry:
        """Returns a minimal terminal view; no live terminal registry."""

        terminal_registry = None

        async def launch_required_terminal(
            self,
            *,
            session_id: str,
            terminal_name: str,
            session_key: str,
            spec: Any,
            resource_role: str | None = None,
            parent_os_env: Any = None,
        ) -> SessionResourceView:
            """Return a minimal terminal view so the launch doesn't error."""
            del spec
            # Guards that _auto_create_claude_terminal tags the agent
            # terminal with the claude-native role — the runner gates
            # PTY-activity → session.status emission on this role, so
            # dropping it would silently disable working-status updates.
            assert resource_role == CLAUDE_NATIVE_TERMINAL_ROLE
            return SessionResourceView(
                id="terminal_claude_main",
                type="terminal",
                session_id=session_id,
                name=f"{terminal_name}:{session_key}",
                metadata={
                    "terminal_name": terminal_name,
                    "session_key": session_key,
                    "running": True,
                },
            )

    # Capture all HTTP requests made to the fake Omnigent server.
    recorded_requests: list[httpx.Request] = []

    def _handle(req: httpx.Request) -> httpx.Response:
        """Record every request; return 200 with minimal session payload."""
        recorded_requests.append(req)
        return httpx.Response(
            200,
            json={
                "reasoning_effort": None,
                "labels": {BRIDGE_ID_LABEL_KEY: "m0-bridge_from_prior_rotation"},
            },
            request=req,
        )

    fake_client = httpx.AsyncClient(
        base_url="http://test-server",
        transport=httpx.MockTransport(_handle),
    )

    await _auto_create_claude_terminal(
        "conv_relay_label_fix",
        _FakeResourceRegistry(),
        lambda _sid, _evt: None,
        server_client=fake_client,
    )

    await fake_client.aclose()

    # Exactly one PATCH request must have been sent to correct the label.
    # 0 means the fix was not applied and the relay would target the wrong dir.
    patch_requests = [r for r in recorded_requests if r.method == "PATCH"]
    assert len(patch_requests) == 1, (
        f"Expected exactly one PATCH to correct the stale bridge_id label; "
        f"got {len(patch_requests)}. 0 means _auto_create_claude_terminal did "
        f"not update the label, so _ensure_comment_relay_started would write "
        f"tool_relay.json to a dir the bridge subprocess never reads."
    )

    import json as _json

    patch_body = _json.loads(patch_requests[0].content)
    assert patch_body.get("labels", {}).get(BRIDGE_ID_LABEL_KEY) == "conv_relay_label_fix", (
        f"PATCH must set {BRIDGE_ID_LABEL_KEY!r} to the session_id "
        f"'conv_relay_label_fix' so _ensure_comment_relay_started finds the "
        f"correct bridge dir; got {patch_body.get('labels', {})!r}"
    )


@dataclass
class _AutoCreateScenario:
    """
    One parametrized case for the claude-native auto-create guard.

    :param case_id: Human-readable scenario id used as the pytest id,
        e.g. ``"clear_rotation_target_skips"``.
    :param active_session_id: ``active_session_id`` to seed into the
        shared bridge config, e.g. ``"conv_old"``. ``None`` seeds no
        bridge dir at all (models a genuinely fresh session).
    :param terminal_under: Session id to seed a live ``claude:main``
        terminal under in the registry, e.g. ``"conv_old"``. ``None``
        seeds no terminal (models a dead/absent original terminal).
    :param bridge_id_label: Value returned for the new session's
        ``BRIDGE_ID_LABEL_KEY`` label, e.g. ``"bridge_shared"`` for a
        rotation target (shares the original's bridge) or ``"conv_new"``
        for a fresh session (own bridge).
    :param expect_auto_create: Whether the guard should invoke
        ``_auto_create_claude_terminal`` for the new session.
    """

    case_id: str
    active_session_id: str | None
    terminal_under: str | None
    bridge_id_label: str
    expect_auto_create: bool


class _LabelsAndEmptyHistoryServerClient:
    """
    Server-client stub for the auto-create guard route test.

    Answers the two GETs ``create_session`` issues for a claude-native
    session: the session snapshot (returns a ``BRIDGE_ID_LABEL_KEY``
    label so the guard can resolve the bridge id) and the items page
    (returns empty history so no crash-recovery turn starts). A real
    stub class — not ``MagicMock`` — so an unexpected call shape fails
    loudly instead of silently returning a mock.
    """

    def __init__(self, bridge_id_label: str) -> None:
        """
        :param bridge_id_label: Bridge id to report on the session's
            ``labels``, e.g. ``"bridge_shared"``.
        """
        self._bridge_id_label = bridge_id_label

    async def get(self, url: str, **kwargs: Any) -> Any:
        """
        Return a canned snapshot or empty items page for *url*.

        :param url: Request path, e.g. ``"/v1/sessions/conv_new"`` or
            ``"/v1/sessions/conv_new/items"``.
        :returns: A response object exposing ``status_code`` and
            ``json()`` matching the subset the runner reads.
        """
        del kwargs

        class _Response:
            """Minimal httpx-like response with the fields the runner reads."""

            def __init__(self, payload: dict[str, Any]) -> None:
                """:param payload: JSON body returned by ``json()``."""
                self.status_code = 200
                self._payload = payload

            def json(self) -> dict[str, Any]:
                """:returns: The canned JSON payload."""
                return self._payload

        if url.endswith("/items"):
            return _Response({"data": [], "has_more": False})
        return _Response({"labels": {BRIDGE_ID_LABEL_KEY: self._bridge_id_label}})


_AUTO_CREATE_SCENARIOS = [
    # Rotation target: the bridge's active session (conv_old) still owns
    # the live terminal that is about to be transferred onto conv_new.
    _AutoCreateScenario(
        case_id="clear_rotation_target_skips",
        active_session_id="conv_old",
        terminal_under="conv_old",
        bridge_id_label="bridge_shared",
        expect_auto_create=False,
    ),
    # Fresh host-spawned session: its own bridge has no recorded active
    # session and no terminal, so it must bootstrap its own Claude.
    _AutoCreateScenario(
        case_id="fresh_session_creates",
        active_session_id=None,
        terminal_under=None,
        bridge_id_label="conv_new",
        expect_auto_create=True,
    ),
    # The bridge's active session is conv_new itself (e.g. a relaunch
    # after the terminal died) — not a rotation, so auto-create proceeds.
    _AutoCreateScenario(
        case_id="active_is_self_creates",
        active_session_id="conv_new",
        terminal_under=None,
        bridge_id_label="bridge_shared",
        expect_auto_create=True,
    ),
    # The bridge names an active sibling (conv_old) but no live terminal
    # exists under it — nothing to transfer in, so auto-create proceeds.
    _AutoCreateScenario(
        case_id="dead_terminal_under_active_creates",
        active_session_id="conv_old",
        terminal_under=None,
        bridge_id_label="bridge_shared",
        expect_auto_create=True,
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario", _AUTO_CREATE_SCENARIOS, ids=[s.case_id for s in _AUTO_CREATE_SCENARIOS]
)
async def test_create_session_auto_create_guard_skips_rotation_targets(
    scenario: _AutoCreateScenario,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The claude-native auto-create guard skips ``/clear`` rotation targets.

    A ``/clear`` or ``/fork`` rotation binds the runner to a fresh AP
    session, then transfers the existing Claude terminal onto it. The
    bind reaches the runner's ``POST /v1/sessions`` before the transfer
    runs, so the new session momentarily has no terminal. Previously,
    ``create_session`` always auto-created a second Claude here, which
    made the subsequent transfer 409 and looped the rotation into
    unbounded session/process spawning. The guard now skips auto-create
    when the new session's bridge already has a *different* session
    owning a live ``claude:main`` terminal — the one about to be
    transferred in.

    Drives the real route with the real guard. Each scenario seeds the
    shared bridge's ``active_session_id`` and the terminal registry, then
    asserts whether ``_auto_create_claude_terminal`` ran. Reverting the
    guard turns the ``clear_rotation_target_skips`` case red (auto-create
    fires for a rotation target again).
    """
    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path / "root")

    # Seed the shared bridge config so the guard reads the original
    # (terminal-owning) session as the bridge's active session.
    if scenario.active_session_id is not None:
        prepare_bridge_dir(
            scenario.active_session_id,
            bridge_id=scenario.bridge_id_label,
            workspace=tmp_path,
        )

    # Seed a live claude:main terminal under the original session so the
    # guard's registry probe finds the terminal that would be transferred.
    # Poking ``_by_conversation`` directly is the established registry-test
    # idiom (see tests/terminals/test_registry.py) — a real TerminalInstance
    # without launching tmux.
    terminal_registry = TerminalRegistry()
    if scenario.terminal_under is not None:
        instance = TerminalInstance(
            name="claude",
            session_key="main",
            socket_path=tmp_path / "claude.sock",
            private_dir=tmp_path / "claude",
            running=True,
        )
        terminal_registry._by_conversation[scenario.terminal_under] = {
            ("claude", "main"): instance
        }

    created: list[str] = []

    async def _recording_auto_create(
        session_id: str, resource_registry: Any, publish_event: Any, **_kwargs: Any
    ) -> None:
        """
        Record the auto-create call instead of launching a real Claude.

        :param session_id: Session id the guard chose to auto-create for,
            e.g. ``"conv_new"``.
        :param resource_registry: Unused — the real launch path is stubbed.
        :param publish_event: Unused — the real launch path is stubbed.
        :param _kwargs: Absorbs keyword args added to the real function
            (e.g. ``server_client``).
        :returns: None.
        """
        del resource_registry, publish_event
        created.append(session_id)

    monkeypatch.setattr("omnigent.runner.app._auto_create_claude_terminal", _recording_auto_create)

    native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """
        Return the claude-native spec for any agent id.

        :param agent_id: Requested agent id (unused — fixed spec).
        :param session_id: Requested session id (unused — fixed spec).
        :returns: The claude-native :class:`AgentSpec`.
        """
        del agent_id, session_id
        return native_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=_LabelsAndEmptyHistoryServerClient(  # type: ignore[arg-type]
            scenario.bridge_id_label
        ),
        terminal_registry=terminal_registry,
    )

    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions",
            json={"session_id": "conv_new", "agent_id": "ag_1"},
        )
    assert resp.status_code == 201, resp.text

    if scenario.expect_auto_create:
        # Fresh / no-live-sibling sessions must still bootstrap their own
        # Claude — the guard only suppresses true rotation targets. An
        # empty ``created`` here would mean the guard over-fired and a
        # host-spawned session would never get a terminal.
        assert created == ["conv_new"], (
            f"Expected auto-create for {scenario.case_id}; got {created}"
        )
    else:
        # The rotation target's terminal arrives via transfer. Auto-create
        # here is the regression: it 409s the transfer and loops the
        # rotation into unbounded session spawning.
        assert created == [], f"Auto-create must be skipped for {scenario.case_id}; got {created}"


@dataclass
class _AntigravityAutoCreateScenario:
    """
    One parametrized case for the antigravity-native auto-create guard.

    :param case_id: Human-readable scenario id used as the pytest id,
        e.g. ``"clear_rotation_target_skips"``.
    :param bridge_state_session: ``session_id`` to seed into the shared
        bridge state, e.g. ``"conv_old"``. ``None`` seeds no bridge state
        at all (models a genuinely fresh session).
    :param terminal_under: Session id to seed a live ``antigravity:main``
        terminal under in the registry, e.g. ``"conv_old"``. ``None``
        seeds no terminal (models a dead/absent original terminal).
    :param bridge_id_label: Value returned for the new session's
        :data:`ANTIGRAVITY_NATIVE_BRIDGE_ID_LABEL_KEY` label, e.g.
        ``"bridge_shared"`` for a rotation target (shares the original's
        bridge) or ``"conv_new"`` for a fresh session (own bridge).
    :param expect_auto_create: Whether the guard should invoke
        ``_auto_create_antigravity_terminal`` for the new session.
    """

    case_id: str
    bridge_state_session: str | None
    terminal_under: str | None
    bridge_id_label: str
    expect_auto_create: bool


class _AntigravitySnapshotServerClient:
    """
    Server-client stub for the antigravity auto-create guard route test.

    Answers the two GETs the antigravity branch issues for ``conv_new``: the
    session snapshot (``/v1/sessions/conv_new`` — non-``None`` so
    ``_session_payload_for_host_spawn_check`` reports the session needs a
    terminal) and the labels lookup (``/v1/sessions/conv_new/labels`` — returns
    the bridge-id label so the transfer-inbound check can resolve the shared
    bridge dir). A real stub class — not ``MagicMock`` — so an unexpected call
    shape fails loudly instead of silently returning a mock.
    """

    def __init__(self, bridge_id_label: str) -> None:
        """
        :param bridge_id_label: Bridge id to report on the session's
            ``labels``, e.g. ``"bridge_shared"``.
        """
        self._bridge_id_label = bridge_id_label

    async def get(self, url: str, **kwargs: Any) -> Any:
        """
        Return a canned snapshot or labels payload for *url*.

        :param url: Request path, e.g. ``"/v1/sessions/conv_new"`` or
            ``"/v1/sessions/conv_new/labels"``.
        :returns: A response object exposing ``status_code`` and ``json()``
            matching the subset the runner reads.
        """
        del kwargs
        labels = {ANTIGRAVITY_NATIVE_BRIDGE_ID_LABEL_KEY: self._bridge_id_label}

        class _Response:
            """Minimal httpx-like response with the fields the runner reads."""

            def __init__(self, payload: dict[str, Any]) -> None:
                """:param payload: JSON body returned by ``json()``."""
                self.status_code = 200
                self._payload = payload

            def json(self) -> dict[str, Any]:
                """:returns: The canned JSON payload."""
                return self._payload

        if url.endswith("/labels"):
            return _Response({"labels": labels})
        # The session snapshot: non-None so the host-spawn check reports the
        # session needs a terminal, and carries the same labels.
        return _Response({"id": "conv_new", "labels": labels})


_ANTIGRAVITY_AUTO_CREATE_SCENARIOS = [
    # Rotation target: the bridge's active session (conv_old) still owns the
    # live agy terminal that is about to be transferred onto conv_new.
    _AntigravityAutoCreateScenario(
        case_id="clear_rotation_target_skips",
        bridge_state_session="conv_old",
        terminal_under="conv_old",
        bridge_id_label="bridge_shared",
        expect_auto_create=False,
    ),
    # Fresh host-spawned session: its own bridge has no recorded state and no
    # terminal, so it must bootstrap (cold-start) its own agy.
    _AntigravityAutoCreateScenario(
        case_id="fresh_session_creates",
        bridge_state_session=None,
        terminal_under=None,
        bridge_id_label="conv_new",
        expect_auto_create=True,
    ),
    # The bridge's recorded session is conv_new itself (e.g. a relaunch after
    # the terminal died) — not a rotation, so auto-create proceeds.
    _AntigravityAutoCreateScenario(
        case_id="active_is_self_creates",
        bridge_state_session="conv_new",
        terminal_under=None,
        bridge_id_label="bridge_shared",
        expect_auto_create=True,
    ),
    # The bridge names a sibling (conv_old) but no live terminal exists under
    # it — nothing to transfer in, so auto-create proceeds.
    _AntigravityAutoCreateScenario(
        case_id="dead_terminal_under_active_creates",
        bridge_state_session="conv_old",
        terminal_under=None,
        bridge_id_label="bridge_shared",
        expect_auto_create=True,
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario",
    _ANTIGRAVITY_AUTO_CREATE_SCENARIOS,
    ids=[s.case_id for s in _ANTIGRAVITY_AUTO_CREATE_SCENARIOS],
)
async def test_create_session_antigravity_auto_create_guard_skips_rotation_targets(
    scenario: _AntigravityAutoCreateScenario,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The antigravity-native auto-create guard skips ``/clear`` rotation targets.

    A ``/clear`` rotation binds the runner to a fresh Omnigent session, then
    transfers the existing agy terminal onto it — agy is one long-lived process
    hosting many cascades, so the rotation re-homes the SAME process. The bind
    reaches the runner's ``POST /v1/sessions`` before the transfer runs, so the
    new session momentarily has no terminal. Auto-creating a second agy here
    cold-starts a brand-new process whose own ``external_session_id`` then 400s
    the rotation's PATCH and loops it into unbounded session/process spawning
    (the bug found by live e2e). The guard now skips auto-create when the new
    session's bridge already has a *different* session owning a live
    ``antigravity:main`` terminal — the one about to be transferred in. Mirrors
    the claude-native guard test above.

    Drives the real route with the real guard. Each scenario seeds the shared
    bridge state's ``session_id`` and the terminal registry, then asserts whether
    ``_auto_create_antigravity_terminal`` ran. Reverting the guard turns the
    ``clear_rotation_target_skips`` case red (auto-create fires for a rotation
    target again).
    """
    import omnigent.antigravity_native_bridge as bridge_mod

    monkeypatch.setattr(bridge_mod, "_BRIDGE_ROOT", tmp_path / "antigravity-native")

    # Seed the shared bridge state so the guard reads the original
    # (terminal-owning) session as the bridge's active session.
    if scenario.bridge_state_session is not None:
        seed_dir = bridge_mod.prepare_bridge_dir(scenario.bridge_id_label)
        bridge_mod.write_bridge_state(
            seed_dir,
            bridge_mod.AntigravityNativeBridgeState(
                session_id=scenario.bridge_state_session,
                conversation_id="cascade_old",
            ),
        )

    # Seed a live antigravity:main terminal under the original session so the
    # guard's registry probe finds the terminal that would be transferred.
    # Poking ``_by_conversation`` directly is the established registry-test
    # idiom — a real TerminalInstance without launching tmux.
    terminal_registry = TerminalRegistry()
    if scenario.terminal_under is not None:
        instance = TerminalInstance(
            name="antigravity",
            session_key="main",
            socket_path=tmp_path / "antigravity.sock",
            private_dir=tmp_path / "antigravity",
            running=True,
        )
        terminal_registry._by_conversation[scenario.terminal_under] = {
            ("antigravity", "main"): instance
        }

    created: list[str] = []

    async def _recording_auto_create(
        session_id: str, resource_registry: Any, publish_event: Any, **_kwargs: Any
    ) -> None:
        """
        Record the auto-create call instead of launching a real agy.

        :param session_id: Session id the guard chose to auto-create for,
            e.g. ``"conv_new"``.
        :param resource_registry: Unused — the real launch path is stubbed.
        :param publish_event: Unused — the real launch path is stubbed.
        :param _kwargs: Absorbs keyword args added to the real function
            (e.g. ``server_client``).
        :returns: None.
        """
        del resource_registry, publish_event
        created.append(session_id)

    monkeypatch.setattr(
        "omnigent.runner.app._auto_create_antigravity_terminal", _recording_auto_create
    )

    native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "antigravity-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """
        Return the antigravity-native spec for any agent id.

        :param agent_id: Requested agent id (unused — fixed spec).
        :param session_id: Requested session id (unused — fixed spec).
        :returns: The antigravity-native :class:`AgentSpec`.
        """
        del agent_id, session_id
        return native_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=_AntigravitySnapshotServerClient(  # type: ignore[arg-type]
            scenario.bridge_id_label
        ),
        terminal_registry=terminal_registry,
    )

    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions",
            json={"session_id": "conv_new", "agent_id": "ag_1"},
        )
    assert resp.status_code == 201, resp.text

    if scenario.expect_auto_create:
        # Fresh / no-live-sibling sessions must still bootstrap their own agy —
        # the guard only suppresses true rotation targets. An empty ``created``
        # here would mean the guard over-fired and a host-spawned session would
        # never get a terminal.
        assert created == ["conv_new"], (
            f"Expected auto-create for {scenario.case_id}; got {created}"
        )
    else:
        # The rotation target's terminal arrives via transfer. Auto-create here
        # is the regression: it cold-starts a redundant agy that 400s the
        # rotation's external_session_id PATCH and loops the rotation.
        assert created == [], f"Auto-create must be skipped for {scenario.case_id}; got {created}"


@dataclass
class _EnsureTerminalCase:
    """
    One routing case for the claude-native ``create_session_terminal``
    ensure-path branch.

    :param case_id: Human-readable id for the parametrize label.
    :param body: The ``POST /resources/terminals`` JSON body.
    :param existing: Whether a live ``claude``/``main`` terminal already
        exists (drives the stubbed ``get_terminal_resource``).
    :param expect_auto_create: Whether the request must route to
        ``_auto_create_claude_terminal`` (the ensure path).
    :param expect_launch: Whether the request must route to the generic
        terminal launch path instead.
    :param expect_name: ``name`` of the resource the route must return —
        identifies which collaborator produced the response.
    """

    case_id: str
    body: dict[str, object]
    existing: bool
    expect_auto_create: bool
    expect_launch: bool
    expect_name: str


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        _EnsureTerminalCase(
            case_id="ensure_no_terminal_auto_creates",
            body={"terminal": "claude", "session_key": "main", "ensure_native_terminal": True},
            existing=False,
            expect_auto_create=True,
            expect_launch=False,
            expect_name="auto-created",
        ),
        _EnsureTerminalCase(
            case_id="ensure_existing_returns_live",
            body={"terminal": "claude", "session_key": "main", "ensure_native_terminal": True},
            existing=True,
            expect_auto_create=False,
            expect_launch=False,
            expect_name="existing",
        ),
        _EnsureTerminalCase(
            # No ensure marker => a plain claude/main launch must take the
            # generic path, NOT the ensure branch. This is the exact body
            # test_comment_relay's plain launch sends; keying on the marker
            # (not on absent spec/bridge) is what keeps that path intact.
            case_id="no_marker_uses_generic_launch",
            body={"terminal": "claude", "session_key": "main"},
            existing=False,
            expect_auto_create=False,
            expect_launch=True,
            expect_name="launched",
        ),
    ],
    ids=lambda c: c.case_id,
)
async def test_create_session_terminal_ensure_routes_claude_native(
    case: _EnsureTerminalCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``POST /resources/terminals`` routes a claude/main request correctly.

    Guards the resume "ensure" branch added so a reattach onto a reused
    daemon runner re-creates the torn-down Claude terminal: a request with
    no ``spec`` and no ``bridge_inject_dir`` must go to the full native
    ``_auto_create_claude_terminal`` (or return the live terminal if one
    exists), while a request carrying ``spec``/``bridge_inject_dir`` (the
    fresh-launch wrapper path) must still use the generic terminal launch.

    The three collaborators are stubbed so the routing decision is the only
    thing under test; each returns a distinctly-named real
    :class:`SessionResourceView`, so the response ``name`` proves which path
    handled the request. Remove the ensure branch and the auto-create cases
    fall through to the generic terminal launch path (wrong name,
    ``launched`` recorded); drop the ``not spec`` guard and the explicit-spec
    case wrongly auto-creates — either way this test fails.

    :param case: The parametrized routing scenario.
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    sid = "conv_ensure"
    auto_create_calls: list[str] = []
    launch_calls: list[str] = []

    async def _stub_auto_create(
        session_id: str,
        resource_registry: object,
        publish_event: object,
        **_kwargs: object,
    ) -> SessionResourceView:
        """Record the ensure-path call and return a tagged terminal view."""
        del resource_registry, publish_event
        auto_create_calls.append(session_id)
        return SessionResourceView(
            id="terminal_claude_main", type="terminal", session_id=session_id, name="auto-created"
        )

    async def _stub_get_terminal(
        self: object, session_id: str, terminal_id: str
    ) -> SessionResourceView | None:
        """Return a live view only when the case seeds an existing terminal."""
        del self
        if case.existing and terminal_id == "terminal_claude_main":
            return SessionResourceView(
                id=terminal_id, type="terminal", session_id=session_id, name="existing"
            )
        return None

    async def _stub_launch_auxiliary_terminal(
        self: object, *, session_id: str, terminal_name: str, session_key: str, **_kwargs: object
    ) -> SessionResourceView:
        """Record the generic-launch call and return a tagged terminal view."""
        del self, _kwargs
        launch_calls.append(f"{terminal_name}:{session_key}")
        return SessionResourceView(
            id=terminal_resource_id(terminal_name, session_key),
            type="terminal",
            session_id=session_id,
            name="launched",
        )

    monkeypatch.setattr("omnigent.runner.app._auto_create_claude_terminal", _stub_auto_create)
    monkeypatch.setattr(SessionResourceRegistry, "get_terminal_resource", _stub_get_terminal)
    monkeypatch.setattr(
        SessionResourceRegistry,
        "launch_auxiliary_terminal",
        _stub_launch_auxiliary_terminal,
    )

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
        terminal_registry=TerminalRegistry(),
    )

    async with _runner_client(app) as client:
        resp = await client.post(f"/v1/sessions/{sid}/resources/terminals", json=case.body)

    assert resp.status_code == 200, resp.text
    # The response name identifies the path taken: only the routed
    # collaborator's view reaches the client.
    assert resp.json()["name"] == case.expect_name, (
        f"{case.case_id}: response came from the wrong path "
        f"(expected {case.expect_name!r}, got {resp.json()['name']!r})"
    )
    # auto_create fires iff this is an ensure request with no live terminal.
    # If empty when expected, the ensure branch did not route to the native
    # auto-create; if populated when not expected, the spec discriminator
    # leaked and a wrapper launch was hijacked.
    assert (auto_create_calls == [sid]) == case.expect_auto_create, (
        f"{case.case_id}: auto_create_calls={auto_create_calls}, "
        f"expected_auto_create={case.expect_auto_create}"
    )
    # The generic launch fires iff the request carried a spec (wrapper path).
    assert (launch_calls == ["claude:main"]) == case.expect_launch, (
        f"{case.case_id}: launch_calls={launch_calls}, expected_launch={case.expect_launch}"
    )


@pytest.mark.asyncio
async def test_create_session_terminal_ensure_failure_returns_json_without_live_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Native terminal ensure failures are reported to AP, not published live.

    ``ensure_native_terminal`` is called by the Omnigent server while handling a
    user message. Omnigent owns that failed transcript turn: it persists the
    consumed user message, appends the sibling ``error`` item, and
    publishes the live banner. If the runner endpoint also publishes
    ``response.error`` before returning its structured 500, the same
    terminal failure is rendered twice live and can be persisted twice by
    the relay.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    sid = "conv_ensure_failure"

    async def _failing_auto_create(
        session_id: str,
        resource_registry: object,
        publish_event: object,
        **_kwargs: object,
    ) -> SessionResourceView:
        """Raise the native startup error the endpoint must return as JSON."""
        del session_id, resource_registry, publish_event, _kwargs
        raise ImportError("Native Claude requires the 'claude' CLI on PATH.")

    def _unexpected_live_publish(*_args: object, **_kwargs: object) -> None:
        """Fail if the ensure endpoint tries to publish the live banner."""
        raise AssertionError("ensure endpoint must not publish response.error")

    monkeypatch.setattr("omnigent.runner.app._auto_create_claude_terminal", _failing_auto_create)
    monkeypatch.setattr(
        "omnigent.runner.app._publish_native_terminal_start_error",
        _unexpected_live_publish,
    )

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
        terminal_registry=TerminalRegistry(),
    )

    async with _runner_client(app) as client:
        resp = await client.post(
            f"/v1/sessions/{sid}/resources/terminals",
            json={"terminal": "claude", "session_key": "main", "ensure_native_terminal": True},
        )

    assert resp.status_code == 500
    # Structured code is preserved; the message is a fixed client-safe
    # string. The raw ImportError text ("requires the 'claude' CLI") must
    # not appear in the HTTP body — it is logged on the runner instead.
    body = resp.json()
    assert body["error"]["code"] == "native_terminal_start_failed"
    assert body["error"]["message"] == (
        "Native Claude terminal failed to start; see runner logs for details."
    )
    assert "requires the 'claude' CLI" not in body["error"]["message"]


@dataclass
class _EnsureCodexTerminalCase:
    """
    One routing case for the codex-native ensure terminal branch.

    :param case_id: Human-readable id for the parametrized case.
    :param body: ``POST /resources/terminals`` JSON body.
    :param existing: Whether a live ``codex``/``main`` terminal exists.
    :param existing_native: Whether the existing terminal metadata looks
        like a runner-owned Codex remote TUI.
    :param expect_auto_create: Whether the route should call the full
        codex-native auto-create helper.
    :param expect_launch: Whether the route should fall through to the
        generic terminal launch path.
    :param expect_close: Whether the existing terminal should be closed
        before native auto-create.
    :param expect_name: Resource ``name`` expected in the HTTP response.
    """

    case_id: str
    body: dict[str, object]
    existing: bool
    existing_native: bool
    expect_auto_create: bool
    expect_launch: bool
    expect_close: bool
    expect_name: str


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        _EnsureCodexTerminalCase(
            case_id="ensure_no_terminal_auto_creates",
            body={"terminal": "codex", "session_key": "main", "ensure_native_terminal": True},
            existing=False,
            existing_native=False,
            expect_auto_create=True,
            expect_launch=False,
            expect_close=False,
            expect_name="auto-created",
        ),
        _EnsureCodexTerminalCase(
            case_id="ensure_existing_returns_live",
            body={"terminal": "codex", "session_key": "main", "ensure_native_terminal": True},
            existing=True,
            existing_native=True,
            expect_auto_create=False,
            expect_launch=False,
            expect_close=False,
            expect_name="existing",
        ),
        _EnsureCodexTerminalCase(
            case_id="ensure_existing_bash_terminal_replaces",
            body={"terminal": "codex", "session_key": "main", "ensure_native_terminal": True},
            existing=True,
            existing_native=False,
            expect_auto_create=True,
            expect_launch=False,
            expect_close=True,
            expect_name="auto-created",
        ),
        _EnsureCodexTerminalCase(
            case_id="no_marker_uses_generic_launch",
            body={"terminal": "codex", "session_key": "main"},
            existing=False,
            existing_native=False,
            expect_auto_create=False,
            expect_launch=True,
            expect_close=False,
            expect_name="launched",
        ),
    ],
    ids=lambda c: c.case_id,
)
async def test_create_session_terminal_ensure_routes_codex_native(
    case: _EnsureCodexTerminalCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``POST /resources/terminals`` routes a codex/main ensure request.

    The ensure marker must invoke the runner-owned Codex setup
    (app-server, forwarder, and TUI terminal) or return an existing
    terminal. Without the marker, a plain codex terminal launch remains a
    generic terminal request. Removing this branch makes the auto-create
    cases return ``"launched"``; over-broad routing makes the generic case
    return ``"auto-created"``.

    :param case: Parametrized routing scenario.
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    sid = "conv_codex_ensure"
    auto_create_calls: list[str] = []
    auto_create_kwargs: list[dict[str, object]] = []
    launch_calls: list[str] = []
    close_calls: list[str] = []
    route_events: list[str] = []

    async def _stub_auto_create(
        session_id: str,
        resource_registry: object,
        publish_event: object,
        **kwargs: object,
    ) -> SessionResourceView:
        """
        Record the codex-native ensure path.

        :param session_id: Session id being ensured, e.g.
            ``"conv_codex_ensure"``.
        :param resource_registry: Runner resource registry collaborator.
        :param publish_event: Runner event publisher collaborator.
        :param kwargs: Additional keyword arguments such as
            ``server_client`` and ``agent_spec``.
        :returns: Tagged terminal resource view.
        """
        del resource_registry, publish_event
        route_events.append("auto-create")
        auto_create_calls.append(session_id)
        auto_create_kwargs.append(kwargs)
        return SessionResourceView(
            id="terminal_codex_main",
            type="terminal",
            session_id=session_id,
            name="auto-created",
        )

    async def _stub_get_terminal(
        self: object,
        session_id: str,
        terminal_id: str,
    ) -> SessionResourceView | None:
        """
        Return an existing terminal only for seeded cases.

        :param self: Bound registry instance.
        :param session_id: Session id being queried, e.g.
            ``"conv_codex_ensure"``.
        :param terminal_id: Terminal resource id, e.g.
            ``"terminal_codex_main"``.
        :returns: Existing resource view or ``None``.
        """
        del self
        if case.existing and terminal_id == "terminal_codex_main":
            return SessionResourceView(
                id=terminal_id,
                type="terminal",
                session_id=session_id,
                name="existing",
                metadata={
                    "terminal_name": "codex",
                    "session_key": "main",
                },
            )
        return None

    def _stub_terminal_resource_role(
        self: object,
        session_id: str,
        terminal_id: str,
    ) -> str | None:
        """
        Return the private resource role for seeded existing terminals.

        :param self: Bound registry instance.
        :param session_id: Session id being queried, e.g.
            ``"conv_codex_ensure"``.
        :param terminal_id: Terminal resource id, e.g.
            ``"terminal_codex_main"``.
        :returns: ``"codex-native"`` for native seeded terminals.
        """
        del self, session_id
        if case.existing and case.existing_native and terminal_id == "terminal_codex_main":
            return CODEX_NATIVE_TERMINAL_ROLE
        return None

    async def _stub_close_terminal(
        self: object,
        session_id: str,
        terminal_id: str,
    ) -> bool:
        """
        Record stale terminal replacement closes.

        :param self: Bound registry instance.
        :param session_id: Session id being modified, e.g.
            ``"conv_codex_ensure"``.
        :param terminal_id: Terminal resource id, e.g.
            ``"terminal_codex_main"``.
        :returns: ``True`` to allow replacement.
        """
        del self
        assert route_events == []
        route_events.append("close")
        close_calls.append(f"{session_id}:{terminal_id}")
        return True

    async def _stub_launch_auxiliary_terminal(
        self: object,
        *,
        session_id: str,
        terminal_name: str,
        session_key: str,
        **kwargs: object,
    ) -> SessionResourceView:
        """
        Record generic terminal launch calls.

        :param self: Bound registry instance.
        :param session_id: Session id being launched, e.g.
            ``"conv_codex_ensure"``.
        :param terminal_name: Terminal name, e.g. ``"codex"``.
        :param session_key: Terminal session key, e.g. ``"main"``.
        :param kwargs: Additional launch keyword arguments.
        :returns: Tagged terminal resource view.
        """
        del self, kwargs
        launch_calls.append(f"{terminal_name}:{session_key}")
        return SessionResourceView(
            id=terminal_resource_id(terminal_name, session_key),
            type="terminal",
            session_id=session_id,
            name="launched",
        )

    monkeypatch.setattr("omnigent.runner.app._auto_create_codex_terminal", _stub_auto_create)
    monkeypatch.setattr(SessionResourceRegistry, "get_terminal_resource", _stub_get_terminal)
    monkeypatch.setattr(
        SessionResourceRegistry,
        "terminal_resource_role",
        _stub_terminal_resource_role,
    )
    monkeypatch.setattr(SessionResourceRegistry, "close_terminal", _stub_close_terminal)
    monkeypatch.setattr(
        SessionResourceRegistry,
        "launch_auxiliary_terminal",
        _stub_launch_auxiliary_terminal,
    )

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
        terminal_registry=TerminalRegistry(),
    )

    async with _runner_client(app) as client:
        resp = await client.post(f"/v1/sessions/{sid}/resources/terminals", json=case.body)

    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == case.expect_name
    assert (auto_create_calls == [sid]) == case.expect_auto_create
    if case.expect_auto_create:
        assert auto_create_kwargs[0]["server_client"] is not None
        assert "agent_spec" in auto_create_kwargs[0]
    else:
        assert auto_create_kwargs == []
    assert (launch_calls == ["codex:main"]) == case.expect_launch
    assert (close_calls == [f"{sid}:terminal_codex_main"]) == case.expect_close
    expected_events = ["close", "auto-create"] if case.expect_close else []
    if case.expect_auto_create and not case.expect_close:
        expected_events = ["auto-create"]
    assert route_events == expected_events


@pytest.mark.asyncio
async def test_late_status_for_deleted_sub_agent_child_is_not_a_spurious_503() -> None:
    """
    A terminal status arriving after a sub-agent child is deleted is a no-op.

    A child created with ``sub_agent_name`` is tracked in the runner's
    sub-agent name map; that registration is what turns a no-work-entry
    terminal status into a 503 (preserve-the-handoff — see
    ``test_known_subagent_status_without_work_entry_returns_503``). Once the
    child is deleted there is nothing to preserve, so ``delete_session`` must
    drop the name. Without the pop, the lingering name makes the late status
    read ``is_runner_known_subagent=True`` with no work entry → a spurious
    ``503 subagent_delivery_not_confirmed`` (which Omnigent then retries) plus an
    unbounded leak of the name map across deleted sessions.
    """
    child_id = "conv_child_late_status_after_delete"
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={
                "session_id": child_id,
                "agent_id": "ag_late_status_after_delete",
                "sub_agent_name": "worker",
            },
        )
        assert create_resp.status_code == 201, create_resp.text

        del_resp = await client.delete(f"/v1/sessions/{child_id}")
        assert del_resp.status_code == 200, del_resp.text

        late_status = await client.post(
            f"/v1/sessions/{child_id}/events",
            json={
                "type": "external_session_status",
                "data": {"status": "idle", "output": "LATE_AFTER_DELETE"},
            },
        )

    # The fix: delete drops the runner-known name, so the late status is a
    # clean 204 no-op. Without the pop the name lingers and this returns a
    # spurious 503 subagent_delivery_not_confirmed.
    assert late_status.status_code == 204, late_status.text


# ── Omnigent REPL terminal auto-create (SDK sessions) ────────────────


@dataclass
class _RecordedPatch:
    """
    A PATCH captured from the REPL terminal auto-create helper.

    :param url: Request path, e.g. ``"/v1/sessions/conv_repl"``.
    :param json: JSON body, e.g. ``{"labels": {"omnigent.ui": "terminal"}}``.
    """

    url: str
    json: dict[str, Any]


@pytest.mark.asyncio
async def test_auto_create_repl_terminal_launches_attach_and_stamps_label(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The REPL terminal hosts ``omnigent attach`` and stamps the UI label.

    The web UI embeds the framework's own TUI for SDK sessions through
    this terminal: the spec must run ``omnigent attach <session_id>
    --server <runner's server URL>`` (a co-drive client of the live
    session), defer the process to first attach, pin the cwd to the
    runner workspace, stamp the ``omnigent.ui: terminal`` label that
    gates the web Chat/Terminal pill, and publish the resource on the
    live stream. Each wrong value maps to a distinct user-facing break:
    wrong command/args → dead pane or wrong session; missing label →
    no pill; missing publish → pill stays gray until refresh.

    :param tmp_path: Temporary directory for the fake runner workspace.
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    from omnigent._wrapper_labels import UI_MODE_LABEL_KEY, UI_MODE_TERMINAL_VALUE

    session_id = "conv_repl"
    workspace = tmp_path / "workspace"
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(workspace))
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://ap.example")

    launched_specs: list[Any] = []

    class _FakeResourceRegistry:
        """Resource registry that records the launched REPL terminal spec."""

        async def launch_auxiliary_terminal(
            self,
            *,
            session_id: str,
            terminal_name: str,
            session_key: str,
            spec: Any,
            resource_role: str | None = None,
            parent_os_env: Any = None,
        ) -> SessionResourceView:
            """
            Record the terminal launch request.

            :param session_id: Session id being launched.
            :param terminal_name: Terminal name, e.g. ``"tui"``.
            :param session_key: Terminal session key, e.g. ``"main"``.
            :param spec: Terminal launch spec.
            :param resource_role: Private runner resource marker.
            :returns: Terminal resource view.
            """
            assert session_id == "conv_repl"
            assert terminal_name == "tui"
            assert session_key == "main"
            # The REPL role marks the pane for recreate-on-attach (a
            # dead REPL pane is relaunched instead of rejected with
            # 4404). It is distinct from CLAUDE_NATIVE_TERMINAL_ROLE,
            # so the pane's activity still does not drive the
            # session's working status.
            assert resource_role == OMNIGENT_REPL_TERMINAL_ROLE
            launched_specs.append(spec)
            return SessionResourceView(
                id="terminal_tui_main",
                type="terminal",
                session_id=session_id,
                name="tui",
            )

    class _PatchRecordingServerClient:
        """Server client that records label PATCHes from the helper."""

        def __init__(self) -> None:
            """:returns: None."""
            self.patches: list[_RecordedPatch] = []

        async def patch(self, url: str, **kwargs: Any) -> httpx.Response:
            """
            Record the PATCH and return a 200.

            :param url: Request path, e.g. ``"/v1/sessions/conv_repl"``.
            :param kwargs: Request keyword arguments carrying ``json``.
            :returns: HTTP 200 response.
            """
            self.patches.append(_RecordedPatch(url=url, json=kwargs.get("json") or {}))
            return httpx.Response(200, json={}, request=httpx.Request("PATCH", url))

    published_events: list[dict[str, Any]] = []
    server_client = _PatchRecordingServerClient()

    terminal_view = await _auto_create_repl_terminal(
        session_id,
        _FakeResourceRegistry(),  # type: ignore[arg-type]
        lambda _sid, event: published_events.append(event),
        server_client=server_client,  # type: ignore[arg-type]
    )

    assert terminal_view.id == "terminal_tui_main"
    assert len(launched_specs) == 1
    launched = launched_specs[0]
    # The hosted TUI is the framework's own REPL joining THIS session on
    # THIS server. A wrong interpreter/module means the pane dies at
    # first attach; a wrong session id or --server URL attaches the REPL
    # to the wrong place.
    assert launched.command == sys.executable
    assert launched.args == [
        "-m",
        "omnigent",
        "attach",
        session_id,
        "--server",
        "http://ap.example",
    ]
    # Deferred start: the REPL process must not run until the first web
    # client attaches — never-opened terminals stay an idle tmux pane,
    # and the session is fully live by first attach.
    assert launched.tmux_start_on_attach is True
    # cwd pins to the runner workspace (same convention as the
    # claude-native terminal); a wrong cwd drops the REPL into $HOME.
    assert launched.os_env.cwd == str(workspace)
    # The presentation label gates the web Chat/Terminal pill
    # (TerminalFirstContext); without this PATCH the embedded terminal
    # is unreachable from the UI.
    assert server_client.patches == [
        _RecordedPatch(
            url=f"/v1/sessions/{session_id}",
            json={"labels": {UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE}},
        )
    ]
    # The live resource event enables the toggle without a refresh
    # (snapshot-on-connect only covers clients that connect later).
    assert published_events[0]["type"] == "session.resource.created"
    assert published_events[0]["resource"]["id"] == "terminal_tui_main"


@pytest.mark.asyncio
async def test_auto_create_repl_terminal_inherits_agent_sandbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The REPL terminal honours the agent's ``os_env.sandbox``.

    Regression for the same sandbox-override bug #175 fixed on the
    codex/claude auto-create paths but missed on the REPL path: the
    REPL auto-create built a fresh ``TerminalEnvSpec`` whose ``os_env``
    carried no ``sandbox`` and passed no ``parent_os_env``, so
    ``launch_terminal`` fell back to ``_default_sandbox_for_platform``
    (``linux_bwrap`` / ``darwin_seatbelt``) and ignored the agent YAML.
    An SDK-harness agent that declares ``os_env.sandbox.type: none``
    (relying on the outer container/VM as the boundary) was wrongly
    forced into bwrap, which then failed to start in a hardened
    container with ``native_terminal_start_failed``.

    Asserts the launched terminal spec's ``os_env.sandbox`` is the
    agent's ``none`` sandbox (not the platform default) and that the
    agent's ``os_env`` is threaded through as the launch
    ``parent_os_env`` so the rest of the policy is inherited too.

    :param tmp_path: Temporary directory for the fake runner workspace.
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec

    session_id = "conv_repl_sandbox_none"
    workspace = tmp_path / "workspace"
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(workspace))
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://ap.example")

    captured: dict[str, Any] = {}

    class _FakeResourceRegistry:
        """Captures the launched REPL terminal spec and parent os_env."""

        async def launch_auxiliary_terminal(
            self,
            *,
            session_id: str,
            terminal_name: str,
            session_key: str,
            spec: Any,
            resource_role: str | None = None,
            parent_os_env: Any = None,
        ) -> SessionResourceView:
            """Record the spec + parent_os_env and return a resource view."""
            del terminal_name, session_key, resource_role
            captured["spec"] = spec
            captured["parent_os_env"] = parent_os_env
            return SessionResourceView(
                id="terminal_tui_main",
                type="terminal",
                session_id=session_id,
                name="tui",
            )

    class _PatchRecordingServerClient:
        """Server client that absorbs the label PATCH from the helper."""

        async def patch(self, url: str, **kwargs: Any) -> httpx.Response:
            """Return a 200 for the presentation-label PATCH."""
            del kwargs
            return httpx.Response(200, json={}, request=httpx.Request("PATCH", url))

    # An agent that declares sandbox: none (runs unconfined; the outer
    # container/VM is the boundary).
    agent_os_env = OSEnvSpec(
        type="caller_process",
        cwd=".",
        sandbox=OSEnvSandboxSpec(type="none"),
    )
    agent_spec = AgentSpec(
        spec_version=1,
        name="sdk_worker",
        executor=ExecutorSpec(
            type="omnigent",
            config={"harness": "openai-agents", "model": "claude-default"},
        ),
        os_env=agent_os_env,
    )

    await _auto_create_repl_terminal(
        session_id,
        _FakeResourceRegistry(),  # type: ignore[arg-type]
        lambda _sid, _evt: None,
        server_client=_PatchRecordingServerClient(),  # type: ignore[arg-type]
        agent_spec=agent_spec,
    )

    launched_sandbox = captured["spec"].os_env.sandbox
    assert launched_sandbox is not None, (
        "REPL auto-create dropped the agent's sandbox; launch_terminal will "
        "fall back to _default_sandbox_for_platform (bwrap), overriding "
        "sandbox: none"
    )
    assert launched_sandbox.type == "none"
    # The whole os_env is threaded through as the inheritance parent.
    assert captured["parent_os_env"] is agent_os_env


@pytest.mark.parametrize(
    ("harness", "sub_agent_name", "expect_created"),
    [
        # SDK harness, top-level → REPL terminal auto-creates.
        ("openai-agents", None, True),
        # Sub-agent sessions surface through the parent transcript — no
        # REPL pane of their own.
        ("openai-agents", "worker", False),
        # Native harnesses own a dedicated terminal (the vendor TUI); the
        # REPL pane must not double up next to it.
        ("codex-native", None, False),
    ],
)
@pytest.mark.asyncio
async def test_create_session_repl_terminal_dispatch(
    harness: str,
    sub_agent_name: str | None,
    expect_created: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``POST /v1/sessions`` auto-creates the REPL terminal for SDK sessions only.

    Exercises the route-level dispatch condition: non-native harness AND
    top-level session AND a terminal registry present. If the condition
    regresses, either SDK sessions lose their embedded web TUI (no
    create) or native / sub-agent sessions grow a spurious second
    terminal (over-create).

    :param harness: Harness id resolved from the agent spec,
        e.g. ``"openai-agents"``.
    :param sub_agent_name: ``sub_agent_name`` in the POST body, or
        ``None`` for a top-level session.
    :param expect_created: Whether the REPL auto-create must fire.
    :param tmp_path: Temporary directory isolating bridge state.
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    import omnigent.runner.app as runner_app_mod

    # Keep the codex-native branch's bridge writes inside tmp_path.
    monkeypatch.setattr(codex_native_bridge, "_BRIDGE_ROOT", tmp_path / "codex-bridge")

    spec = AgentSpec(
        spec_version=1,
        name="dispatch-agent",
        executor=ExecutorSpec(type="omnigent", config={"harness": harness}),
    )
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the parametrized spec for any agent id."""
        del agent_id, session_id
        return spec

    created_sessions: list[str] = []

    async def _fake_auto_create_repl(
        session_id: str,
        resource_registry: Any,
        publish_event: Any,
        *,
        server_client: Any,
        agent_spec: Any = None,
    ) -> SessionResourceView:
        """Record the dispatch instead of launching a real tmux pane."""
        del resource_registry, publish_event, server_client, agent_spec
        created_sessions.append(session_id)
        return SessionResourceView(
            id="terminal_tui_main",
            type="terminal",
            session_id=session_id,
            name="tui",
        )

    monkeypatch.setattr(runner_app_mod, "_auto_create_repl_terminal", _fake_auto_create_repl)

    async def _fake_codex_needs(server_client: Any, session_id: str) -> bool:
        """Neutralize the codex-native terminal branch (out of scope here)."""
        del server_client, session_id
        return False

    monkeypatch.setattr(runner_app_mod, "_codex_session_needs_runner_terminal", _fake_codex_needs)

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
        # A real (empty) registry: the dispatch gate requires one, and
        # ``get()`` on it reports no existing REPL terminal.
        terminal_registry=TerminalRegistry(),
    )

    body: dict[str, Any] = {"session_id": "conv_dispatch", "agent_id": "ag_dispatch"}
    if sub_agent_name is not None:
        body["sub_agent_name"] = sub_agent_name
    async with _runner_client(app) as client:
        resp = await client.post("/v1/sessions", json=body)

    assert resp.status_code == 201, resp.text
    # Dispatch fired exactly for the SDK top-level case. An unexpected
    # entry here means natives/sub-agents grew a REPL pane; a missing
    # one means SDK sessions lost the embedded web TUI.
    assert created_sessions == (["conv_dispatch"] if expect_created else [])


# ── Sub-agent wake-POST status check + bounded retry ──────────────────


@dataclass
class _WakePost:
    """
    A single recorded POST made by ``_QueuedResponseServerClient``.

    :param url: The path the wake notice was POSTed to, e.g.
        ``"/v1/sessions/conv_parent123/events"``.
    :param notice: The injected notice text pulled out of the request body.
    """

    url: str
    notice: str


class _QueuedResponseServerClient:
    """
    Omnigent HTTP client stub that returns a fixed queue of real responses.

    A real stub (NOT ``MagicMock``) so that an unexpected attribute access or
    an extra POST beyond the queue fails the test loudly instead of silently
    returning a truthy mock. Each ``post`` pops the next pre-built
    :class:`httpx.Response` (so ``raise_for_status`` runs its real logic —
    a 503 raises, a 200 does not) and records the call for assertions.

    :param responses: Responses to return in order, one per ``post`` call,
        e.g. ``[httpx.Response(503, ...), httpx.Response(200, ...)]``.
    """

    def __init__(self, responses: list[httpx.Response]) -> None:
        """
        Store the response queue and an empty call log.

        :param responses: Responses to return in order, one per POST.
        :returns: None.
        """
        self._responses = list(responses)
        self.calls: list[_WakePost] = []

    async def post(self, url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        """
        Record the POST and return the next queued response.

        :param url: Target path, e.g. ``"/v1/sessions/conv_p/events"``.
        :param json: Wake-notice request body in the ingest message shape.
        :param timeout: Per-request timeout (recorded only, not enforced).
        :returns: The next pre-built response from the queue.
        :raises AssertionError: If more POSTs are made than responses queued.
        """
        notice = json["data"]["content"][0]["text"]
        self.calls.append(_WakePost(url=url, notice=notice))
        assert self._responses, (
            f"Wake POST made {len(self.calls)} call(s) but only "
            f"{len(self.calls) - 1} response(s) were queued — the retry "
            f"loop exceeded its bound."
        )
        return self._responses.pop(0)


def _wake_response(status_code: int, parent_id: str) -> httpx.Response:
    """
    Build a real ``httpx.Response`` for a wake POST to ``parent_id``.

    A request is attached so ``raise_for_status`` can construct a proper
    ``HTTPStatusError`` on non-2xx, matching what httpx does in production.

    :param status_code: HTTP status to simulate, e.g. ``503``.
    :param parent_id: Parent session id used to build the request URL.
    :returns: A response carrying a representative JSON body.
    """
    request = httpx.Request("POST", f"http://test/v1/sessions/{parent_id}/events")
    body = (
        {"error": {"code": "RUNNER_UNAVAILABLE", "message": "runner reconnecting"}}
        if status_code >= 400
        else {"id": "evt_1", "object": "session.event"}
    )
    return httpx.Response(status_code, request=request, json=body)


@pytest.fixture
def _no_wake_backoff(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """
    Replace the wake retry sleep with a deterministic recorder.

    Patches the module-level ``_wake_retry_sleep`` indirection helper (NOT
    the global ``asyncio.sleep``, which the ``no-global-asyncio-patch`` hook
    bans) so retries do not actually wait, and exposes the requested backoff
    delays so a test can assert how many retries occurred.

    :param monkeypatch: pytest monkeypatch fixture.
    :returns: A list that accumulates the backoff delays requested, in order.
    """
    recorded: list[float] = []

    async def _record(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr("omnigent.runner.app._wake_retry_sleep", _record)
    return recorded


async def test_wake_post_retries_transient_503_then_succeeds(
    _no_wake_backoff: list[float],
) -> None:
    """
    A transient 503 wake response is retried and the next 200 succeeds.

    Guards the core bug: Omnigent returns a genuine 503 ``RUNNER_UNAVAILABLE``
    *response* (not a transport exception) while the parent's runner tunnel
    reconnects. The wake POST must treat that as a failure and retry, not
    accept it as delivered.
    """
    parent_id = "conv_parent_503_then_ok"
    client = _QueuedResponseServerClient(
        [_wake_response(503, parent_id), _wake_response(200, parent_id)]
    )

    delivered = await _deliver_subagent_wake_post(
        client,  # type: ignore[arg-type]
        parent_id,
        "[System: worker completed]",
    )

    # Returns True only because the retry re-POSTed after the 503 and got a
    # 200. If the status check were missing, the first 503 would be treated
    # as success and there would be exactly one call with delivered already
    # True — so both the count and the value below pin the fix.
    assert delivered is True
    # Exactly two POSTs: the 503 attempt + the 200 retry. One call would mean
    # the 503 was silently accepted; three would mean it retried past success.
    assert len(client.calls) == 2, (
        f"Expected 2 wake POSTs (503 then 200 retry), got {len(client.calls)}."
    )
    # Both POSTs targeted the parent's events endpoint with the same notice.
    assert client.calls[0].url == f"/v1/sessions/{parent_id}/events"
    assert client.calls[1].notice == "[System: worker completed]"
    # Exactly one backoff slept (between the two attempts) — proves the retry
    # path ran rather than the call being retried zero or two+ times.
    assert len(_no_wake_backoff) == 1, (
        f"Expected one backoff before the single retry, got {_no_wake_backoff}."
    )


async def test_wake_post_persistent_503_returns_failure(
    _no_wake_backoff: list[float],
) -> None:
    """
    A 503 on every attempt exhausts the retry budget and reports failure.

    This is the regression guard for the silent-strand bug: a 503 must be
    surfaced as a delivery failure (so the caller releases the debounce flag
    and logs), never swallowed as a success.
    """
    parent_id = "conv_parent_always_503"
    client = _QueuedResponseServerClient(
        [_wake_response(503, parent_id) for _ in range(_WAKE_POST_MAX_ATTEMPTS)]
    )

    delivered = await _deliver_subagent_wake_post(
        client,  # type: ignore[arg-type]
        parent_id,
        "[System: worker completed]",
    )

    # False = the non-2xx response was treated as a failure. Before the fix
    # this returned (implicitly) success and the wake was considered delivered.
    assert delivered is False
    # Attempted exactly the bounded budget — not once (no retry) and not
    # unbounded. The stub would have asserted on a call past the queue.
    assert len(client.calls) == _WAKE_POST_MAX_ATTEMPTS, (
        f"Expected {_WAKE_POST_MAX_ATTEMPTS} attempts on persistent 503, got {len(client.calls)}."
    )
    # One backoff fewer than attempts: we don't sleep after the final attempt.
    assert len(_no_wake_backoff) == _WAKE_POST_MAX_ATTEMPTS - 1, (
        f"Expected {_WAKE_POST_MAX_ATTEMPTS - 1} backoffs between "
        f"{_WAKE_POST_MAX_ATTEMPTS} attempts, got {_no_wake_backoff}."
    )


async def test_wake_post_permanent_4xx_not_retried(
    _no_wake_backoff: list[float],
) -> None:
    """
    A permanent 4xx wake rejection fails immediately without retrying.

    A 400 is a client-side rejection that retrying cannot fix, so the loop
    must give up after one attempt rather than burn the whole budget.
    """
    parent_id = "conv_parent_400"
    client = _QueuedResponseServerClient([_wake_response(400, parent_id)])

    delivered = await _deliver_subagent_wake_post(
        client,  # type: ignore[arg-type]
        parent_id,
        "[System: worker completed]",
    )

    # Permanent rejection => failure, no delivery.
    assert delivered is False
    # Exactly one attempt: a permanent 4xx is not retried. Two+ would mean the
    # classifier wrongly treated 400 as transient.
    assert len(client.calls) == 1, (
        f"Expected a single attempt on permanent 400, got {len(client.calls)}."
    )
    # No backoff at all — the loop exited before any sleep.
    assert _no_wake_backoff == []


@pytest.mark.parametrize(
    "status_code,expected_retryable",
    [
        (503, True),  # RUNNER_UNAVAILABLE — the routine reconnect case
        (500, True),  # generic server error
        (429, True),  # rate limit — explicitly transient
        (409, True),  # conflict — explicitly transient
        (400, False),  # bad request — permanent
        (404, False),  # not found — permanent
    ],
)
def test_wake_post_is_retryable_status_classification(
    status_code: int, expected_retryable: bool
) -> None:
    """
    The status classifier retries 5xx + transient 4xx, not permanent 4xx.

    :param status_code: Simulated HTTP status on the wake response.
    :param expected_retryable: Whether that status should be retried.
    """
    request = httpx.Request("POST", "http://test/v1/sessions/p/events")
    exc = httpx.HTTPStatusError(
        "wake rejected",
        request=request,
        response=httpx.Response(status_code, request=request),
    )
    # Pins which statuses cost a retry vs. fail fast; a wrong verdict here
    # would either waste the budget on permanent errors or give up on a 503.
    assert _wake_post_is_retryable(exc) is expected_retryable


def test_wake_post_transport_error_is_retryable() -> None:
    """
    A transport-level error (no response) is always retryable.

    A ``ConnectError`` carries no HTTP response — the POST may never have
    reached Omnigent — so the wake should be retried.
    """
    request = httpx.Request("POST", "http://test/v1/sessions/p/events")
    exc = httpx.ConnectError("connection refused", request=request)
    # True because a transport failure is not a definitive server rejection.
    assert _wake_post_is_retryable(exc) is True


# ── Cost advisor v3 turn-path: application + note + label ─────────────────────


_ADVISOR_TIERS_YAML: dict[str, Any] = {  # type: ignore[explicit-any]  # YAML-shaped marker
    "mode": "optimize",
    "tiers": {
        "cheap": ["model-cheap"],
        "expensive": ["model-pricey"],
    },
}


def _advisor_orchestrator_spec(*, mode: str = "optimize") -> AgentSpec:
    """Build an opted-in claude-sdk orchestrator spec for the advisor tests.

    The brain harness is ``claude-sdk`` (the only harness the advisor
    APPLIES to); the fake process manager serves any harness name, so the
    scripted client still handles the turn. One sub-agent is declared so a
    realistic orchestrator spec is exercised.

    :param mode: The advisor mode baked into the marker, ``"optimize"`` or
        ``"advise"``.
    :returns: An :class:`AgentSpec` with the ``cost_optimize`` marker.
    """
    return AgentSpec(
        spec_version=1,
        name="advisor-orchestrator",
        executor=ExecutorSpec(
            type="omnigent",
            config={
                "harness": "claude-sdk",
                "cost_optimize": {**_ADVISOR_TIERS_YAML, "mode": mode},
            },
        ),
        sub_agents=[
            AgentSpec(
                spec_version=1,
                name="worker",
                executor=ExecutorSpec(type="omnigent", config={"harness": "codex"}),
            ),
        ],
    )


def _patch_judge_returns_pricey(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Replace the production LLM judge with a deterministic stub.

    The stub always sizes the turn to the expensive tier (``model-pricey``)
    so the application path is observable; the judge's own prompt/parse
    behavior is covered in ``test_cost_judge.py``. Patches the symbol the
    advisor imports lazily inside ``maybe_run_advisor``.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    from omnigent.cost_plan import AdvisorVerdict
    from omnigent.runner import cost_advisor as cost_advisor_mod

    class _PriceyJudge:
        """Judge stub returning a fixed expensive-tier verdict."""

        async def judge(self, *, query: str, turn_anchor: str) -> AdvisorVerdict:
            """:returns: An expensive-tier verdict anchored to the turn."""
            del query
            return AdvisorVerdict(
                tier="expensive",
                model="model-pricey",
                applied=False,
                rationale="hard work",
                turn_anchor=turn_anchor,
            )

    def _build_stub_judge(**kwargs: Any) -> _PriceyJudge:  # type: ignore[explicit-any]
        """:returns: The deterministic judge stub regardless of config."""
        del kwargs
        return _PriceyJudge()

    monkeypatch.setattr(cost_advisor_mod, "build_llm_judge", _build_stub_judge)


class _LabelPatchRecordingServerClient(_FakeServerClient):
    """``_FakeServerClient`` that also records label PATCHes.

    The advisor's verdict persist is a ``PATCH /v1/sessions/{id}`` with a
    ``labels`` body; recording it lets the turn-path tests assert the
    verdict label actually reached the server.

    :param items: History items served by the inherited GET handler.
    """

    def __init__(
        self,
        items: list[dict[str, Any]],
        *,
        session_snapshot: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(items, session_snapshot=session_snapshot)
        self.label_patches: list[dict[str, Any]] = []  # type: ignore[explicit-any]  # JSON bodies

    async def patch(
        self,
        url: str,
        *,
        json: dict[str, Any],  # type: ignore[explicit-any]  # JSON body
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Record the PATCH body and answer 200."""
        del url, headers, timeout
        self.label_patches.append(json)

        class _Resp:
            status_code = 200

        return _Resp()


def _advisor_note_items(content: list[dict[str, Any]]) -> list[str]:  # type: ignore[explicit-any]
    """Extract the v3 advisor-note texts from a harness body's content.

    Handles both body shapes the advisor merges into: history-shaped
    message items (background-turn path) and raw content blocks
    (``?stream=true`` path).

    :param content: The ``content`` list POSTed to the harness.
    :returns: The texts of blocks whose text starts with the v3 note
        marker, e.g. ``["[Cost advisor: this turn runs on ...]"]``.
    """
    texts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        # A message item carries blocks; a raw block IS the text carrier.
        blocks = item.get("content") or [] if item.get("type") == "message" else [item]
        for block in blocks:
            text = block.get("text") if isinstance(block, dict) else None
            if isinstance(text, str) and text.startswith("[Cost advisor: "):
                texts.append(text)
    return texts


def _latest_user_texts(content: list[dict[str, Any]]) -> list[str]:  # type: ignore[explicit-any]
    """Extract the text blocks of the message the executor would deliver.

    Mirrors the claude-sdk executor's latest-user-message selection: for
    history-shaped content, the LAST ``role == "user"`` item's block
    texts; for raw content blocks, the whole list (it IS one message).

    :param content: The ``content`` list POSTed to the harness.
    :returns: The latest user message's block texts in order, e.g.
        ``["refactor the auth flow", "[Cost advisor: ...]"]``.
    """
    if any(isinstance(it, dict) and it.get("type") == "message" for it in content):
        for item in reversed(content):
            if isinstance(item, dict) and item.get("role") == "user":
                return [
                    block.get("text")
                    for block in item.get("content") or []
                    if isinstance(block, dict) and isinstance(block.get("text"), str)
                ]
        return []
    return [
        block.get("text")
        for block in content
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    ]


@pytest.mark.asyncio
async def test_optimize_turn_applies_model_and_injects_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An optimize-mode turn on a claude-sdk brain runs THIS turn on the
    verdict model and announces it.

    The core v3 proof: the harness body the runner POSTs carries
    ``model_override == "model-pricey"`` (the per-turn brain-model switch,
    which the claude-sdk executor honors via ``set_model``) AND a single
    ``[Cost advisor:`` note, and the persisted label is ``applied=True``.

    :param monkeypatch: Replaces the production judge with the stub.
    """
    from omnigent.cost_plan import parse_verdict

    _patch_judge_returns_pricey(monkeypatch)
    spec = _advisor_orchestrator_spec()
    sse_frames = [
        _sse({"type": "response.created", "response": {"id": "resp_o1"}}),
        _sse({"type": "response.completed", "response": {"id": "resp_o1"}}),
    ]
    hc = _ScriptedHarnessClient(sse_frames)
    pm = _FakeProcessManager(hc)
    server_client = _LabelPatchRecordingServerClient([])

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server_client,  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions",
            json={"session_id": "conv_adv_opt", "agent_id": "ag_adv"},
        )
        assert resp.status_code == 201
        resp2 = await client.post(
            "/v1/sessions/conv_adv_opt/events?stream=true",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "ag_adv",
                "model": "test",
                "content": [{"type": "input_text", "text": "refactor the auth flow"}],
            },
        )
        assert resp2.status_code == 200
        assert "response.completed" in resp2.text

    # The verdict label persisted with applied=True (the advisor decided to
    # apply, so the label must say so — applied=False would mean the runner
    # silently didn't switch the model the label claims).
    label_bodies = [body for body in server_client.label_patches if "labels" in body]
    assert len(label_bodies) == 1
    verdict = parse_verdict(label_bodies[0]["labels"])
    assert verdict is not None
    assert verdict.model == "model-pricey"
    assert verdict.applied is True

    # The harness body the runner POSTed carries the per-turn model switch.
    assert len(hc.posted_bodies) == 1
    body = hc.posted_bodies[0]
    assert body.get("model_override") == "model-pricey", (
        "the optimize turn did not stamp the verdict model on the harness body; "
        "the brain would have run on the spec/gateway default, not the verdict."
    )
    # ...and exactly one v3 note announcing it.
    notes = _advisor_note_items(body.get("content") or [])
    assert notes == ["[Cost advisor: this turn runs on model-pricey (expensive)]"]
    # REGRESSION (live 2026-06-11): the note must ride INSIDE the user's
    # message, after the question — a trailing note-only user message
    # shadows the question entirely (claude-sdk sends only the latest user
    # message on resumed sessions, so the brain answers the note: "Got it,
    # the model is now set to ..." and the user's task is dropped).
    assert _latest_user_texts(body.get("content") or []) == [
        "refactor the auth flow",
        "[Cost advisor: this turn runs on model-pricey (expensive)]",
    ], (
        "the advisor note displaced the user's question from the message the "
        "executor delivers; the question must stay primary with the note "
        "riding along in the same message."
    )


@pytest.mark.asyncio
async def test_advise_turn_records_but_does_not_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An advise-mode turn shadows: the verdict is recorded (applied=False)
    but the brain model is untouched and no note is injected.

    :param monkeypatch: Replaces the production judge with the stub.
    """
    from omnigent.cost_plan import parse_verdict

    _patch_judge_returns_pricey(monkeypatch)
    spec = _advisor_orchestrator_spec(mode="advise")
    sse_frames = [
        _sse({"type": "response.created", "response": {"id": "resp_a1"}}),
        _sse({"type": "response.completed", "response": {"id": "resp_a1"}}),
    ]
    hc = _ScriptedHarnessClient(sse_frames)
    pm = _FakeProcessManager(hc)
    server_client = _LabelPatchRecordingServerClient([])

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server_client,  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions",
            json={"session_id": "conv_adv_shadow", "agent_id": "ag_adv"},
        )
        assert resp.status_code == 201
        resp2 = await client.post(
            "/v1/sessions/conv_adv_shadow/events?stream=true",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "ag_adv",
                "model": "test",
                "content": [{"type": "input_text", "text": "refactor the auth flow"}],
            },
        )
        assert resp2.status_code == 200
        assert "response.completed" in resp2.text

    # Advise mode: recorded but not applied.
    label_bodies = [body for body in server_client.label_patches if "labels" in body]
    assert len(label_bodies) == 1
    verdict = parse_verdict(label_bodies[0]["labels"])
    assert verdict is not None
    assert verdict.model == "model-pricey"
    assert verdict.applied is False

    # Shadow: no model_override, no note.
    assert len(hc.posted_bodies) == 1
    body = hc.posted_bodies[0]
    assert body.get("model_override") is None
    assert _advisor_note_items(body.get("content") or []) == []


@pytest.mark.asyncio
async def test_user_pin_suppresses_sticky_model_on_background_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A user-pinned turn on the BACKGROUND path carries NO advisor model.

    Live precedence bug this guards: ``_run_turn_bg`` rebuilds the harness
    body without the inbound ``model_override``, so after an applied
    optimize turn the sticky carry-forward stamped the advisor's last model
    onto the pinned turn — and ``cfg.model`` beats the env-carried user pin
    in the claude-sdk executor, silently running the turn on the advisor's
    model instead of the user's. (The stream path keeps the inbound body,
    so only the background path exposes this.)

    :param monkeypatch: Replaces the production judge with the stub.
    """
    import asyncio as _aio

    from omnigent.cost_plan import parse_verdict

    _patch_judge_returns_pricey(monkeypatch)
    spec = _advisor_orchestrator_spec()
    sse_frames = [
        _sse({"type": "response.created", "response": {"id": "resp_pin"}}),
        _sse({"type": "response.completed", "response": {"id": "resp_pin"}}),
    ]
    hc = _ScriptedHarnessClient(sse_frames)
    pm = _FakeProcessManager(hc)
    server_client = _LabelPatchRecordingServerClient([])

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server_client,  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions",
            json={"session_id": "conv_adv_pin", "agent_id": "ag_adv"},
        )
        assert resp.status_code == 201
        # Turn 1 (no pin): optimize applies the verdict model → sticky set.
        resp1 = await client.post(
            "/v1/sessions/conv_adv_pin/events",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "ag_adv",
                "model": "test",
                "content": [{"type": "input_text", "text": "refactor the auth flow"}],
            },
        )
        assert resp1.status_code == 202
        for _ in range(500):  # event-driven wait, no fixed sleep
            if hc.posted_bodies:
                break
            await _aio.sleep(0.01)
        assert len(hc.posted_bodies) == 1, "turn 1 never reached the harness"
        # Turn 2: the server forwards the session's user pin on the message.
        resp2 = await client.post(
            "/v1/sessions/conv_adv_pin/events",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "ag_adv",
                "model": "test",
                "model_override": "user-pinned-model",
                "content": [{"type": "input_text", "text": "now do something hard"}],
            },
        )
        assert resp2.status_code == 202
        for _ in range(500):
            if len(hc.posted_bodies) >= 2:
                break
            await _aio.sleep(0.01)
        assert len(hc.posted_bodies) == 2, "turn 2 never reached the harness"

    body1, body2 = hc.posted_bodies
    # Turn 1 applied the verdict model (sticky state now holds it).
    assert body1.get("model_override") == "model-pricey"
    # Turn 2: the sticky model must NOT be stamped — body model_override
    # (cfg.model) would beat the env-carried user pin in the executor.
    # "model-pricey" here = the pre-fix bug (advisor silently overrode /model).
    assert body2.get("model_override") is None, (
        f"pinned turn carried model_override={body2.get('model_override')!r}; "
        "the advisor's sticky model must never override a user pin."
    )
    # ...and no note: nothing was applied on the pinned turn.
    assert _advisor_note_items(body2.get("content") or []) == []
    # Both verdicts persisted; the pinned turn's is shadow (applied=False).
    label_bodies = [body for body in server_client.label_patches if "labels" in body]
    assert len(label_bodies) == 2
    pinned_verdict = parse_verdict(label_bodies[1]["labels"])
    assert pinned_verdict is not None
    assert pinned_verdict.applied is False, (
        "the pinned turn's verdict must be recorded as NOT applied"
    )


# ── Per-session transcript-forwarder registry (double-mirror regression) ──


@dataclass
class _ForwarderRun:
    """
    One spawned transcript-forwarder stub run.

    :param task: The asyncio task executing this run, captured via
        ``asyncio.current_task()`` when the stub body starts. Used for
        registry-independent cleanup.
    :param cancelled: ``True`` once the parked run observed
        :class:`asyncio.CancelledError`.
    """

    task: asyncio.Task[Any] | None = None
    cancelled: bool = False


async def _drain_forwarder_runs(runs: list[_ForwarderRun]) -> None:
    """
    Cancel and await any still-parked forwarder stub runs.

    Test cleanup helper so a failed assertion never leaks a parked task
    (or a registry entry) into the next test.

    :param runs: Stub runs recorded by a parking forwarder fake.
    :returns: None.
    """
    leftovers = [run.task for run in runs if run.task is not None and not run.task.done()]
    for task in leftovers:
        task.cancel()
    if leftovers:
        await asyncio.wait(leftovers)


@pytest.mark.asyncio
async def test_cancel_auto_forwarder_task_cancels_and_awaits_registered_task() -> None:
    """
    Cancelling a session's registered forwarder awaits its completion.

    This is the ordering guarantee the claude re-create path relies on:
    ``_cancel_auto_forwarder_task`` must not return while the old task can
    still post items (it runs right before the bridge's forward-cursor
    state is wiped).
    """
    import omnigent.runner.app as runner_app_mod

    session_id = "conv_fwd_cancel_awaits"
    run = _ForwarderRun()

    async def _parked() -> None:
        """Park forever like the restart-forever supervisor."""
        run.task = asyncio.current_task()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            run.cancelled = True
            raise

    try:
        task = asyncio.create_task(_parked())
        runner_app_mod._register_auto_forwarder_task(session_id, task)
        # Yield so the coroutine body starts (a never-started task would be
        # dropped without ever entering the except branch).
        await asyncio.sleep(0)
        assert not task.done()

        await runner_app_mod._cancel_auto_forwarder_task(session_id)

        # cancelled() is only True for a FINISHED cancelled task, proving
        # the helper awaited completion rather than fire-and-forgetting.
        assert task.cancelled(), (
            "Registered forwarder task must be finished-cancelled after "
            "_cancel_auto_forwarder_task returns; a live task here means the "
            "helper did not await the cancellation."
        )
        # The coroutine body observed the cancel — the parked await was
        # actually interrupted, not skipped.
        assert run.cancelled is True
        # The slot is freed for the successor registration.
        assert session_id not in runner_app_mod._AUTO_FORWARDER_TASKS
        # Idempotent: a second cancel with no registered task is a no-op.
        await runner_app_mod._cancel_auto_forwarder_task(session_id)
    finally:
        runner_app_mod._AUTO_FORWARDER_TASKS.pop(session_id, None)
        await _drain_forwarder_runs([run])


@pytest.mark.asyncio
async def test_register_auto_forwarder_task_replaces_incumbent_and_survives_stale_evict() -> None:
    """
    Re-registration cancels the incumbent; its done-callback can't evict the successor.

    Two claims:

    1. Registering task B for a session that already holds live task A
       cancels A (no session ever runs two forwarders).
    2. A's done-callback fires AFTER B occupies the slot; the eviction is
       identity-checked, so the stale callback must leave B registered.
       Without the identity check, B would lose its strong reference and
       the registry would report no forwarder for a session that has one.
    """
    import omnigent.runner.app as runner_app_mod

    session_id = "conv_fwd_stale_evict"
    run_a = _ForwarderRun()
    run_b = _ForwarderRun()

    async def _parked(run: _ForwarderRun) -> None:
        """Park forever; record cancellation on the given run."""
        run.task = asyncio.current_task()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            run.cancelled = True
            raise

    try:
        task_a = asyncio.create_task(_parked(run_a))
        runner_app_mod._register_auto_forwarder_task(session_id, task_a)
        await asyncio.sleep(0)

        task_b = asyncio.create_task(_parked(run_b))
        runner_app_mod._register_auto_forwarder_task(session_id, task_b)

        # Claim 1: the incumbent was cancelled by the replacement.
        await asyncio.wait({task_a})
        assert run_a.cancelled is True, (
            "Registering a successor must cancel the live incumbent; a "
            "surviving incumbent is exactly the double-mirror bug."
        )
        # Let task_a's done-callback (the stale evict) run.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        # Claim 2: the stale callback did not evict the successor.
        assert runner_app_mod._AUTO_FORWARDER_TASKS.get(session_id) is task_b, (
            "Task A's done-callback evicted task B — eviction must be "
            "identity-checked so a predecessor's completion cannot drop the "
            "live successor's registration."
        )
        # The successor must still be running — done here means A's cancel hit B.
        assert not task_b.done()
    finally:
        runner_app_mod._AUTO_FORWARDER_TASKS.pop(session_id, None)
        await _drain_forwarder_runs([run_a, run_b])


@pytest.mark.asyncio
async def test_auto_forwarder_registry_isolates_sessions_and_evicts_completed() -> None:
    """
    Per-session keying: cancelling one session leaves another's forwarder running.

    Also pins the natural-completion eviction: a registered task that
    finishes on its own removes its entry (the dict must not leak entries
    the way the old set relied on ``discard`` for).
    """
    import omnigent.runner.app as runner_app_mod

    run_a = _ForwarderRun()
    run_b = _ForwarderRun()

    async def _parked(run: _ForwarderRun) -> None:
        """Park forever; record cancellation on the given run."""
        run.task = asyncio.current_task()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            run.cancelled = True
            raise

    try:
        task_a = asyncio.create_task(_parked(run_a))
        task_b = asyncio.create_task(_parked(run_b))
        runner_app_mod._register_auto_forwarder_task("conv_fwd_sess_a", task_a)
        runner_app_mod._register_auto_forwarder_task("conv_fwd_sess_b", task_b)
        await asyncio.sleep(0)

        await runner_app_mod._cancel_auto_forwarder_task("conv_fwd_sess_a")

        assert run_a.cancelled is True
        # Session B's forwarder is untouched by session A's cancel — keying
        # by session id must not regress to whole-registry cancellation.
        assert run_b.cancelled is False
        assert not task_b.done()
        assert runner_app_mod._AUTO_FORWARDER_TASKS.get("conv_fwd_sess_b") is task_b

        # Natural completion evicts the entry (no leak for finished tasks).
        task_b.cancel()
        await asyncio.wait({task_b})
        await asyncio.sleep(0)
        assert "conv_fwd_sess_b" not in runner_app_mod._AUTO_FORWARDER_TASKS
    finally:
        runner_app_mod._AUTO_FORWARDER_TASKS.pop("conv_fwd_sess_a", None)
        runner_app_mod._AUTO_FORWARDER_TASKS.pop("conv_fwd_sess_b", None)
        await _drain_forwarder_runs([run_a, run_b])


@pytest.mark.asyncio
async def test_auto_create_claude_terminal_recreate_cancels_prior_forwarder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Re-running claude terminal auto-create leaves exactly one live forwarder.

    Regression for the recovery path: ``create_session_terminal``'s
    ensure branch re-runs ``_auto_create_claude_terminal`` after a bridge
    closure, but the prior ``supervise_forwarder`` task is restart-forever
    and survives pane death (it re-resolves the transcript path each loop).
    Before the per-session registry, the second create wiped the shared
    forward cursor and spawned a second forwarder, so both tasks mirrored
    every post-recovery transcript record into the session — each item
    persisted twice (the server has no external-item dedup).

    :param tmp_path: Pytest-provided temporary directory.
    :param monkeypatch: Pytest monkeypatch fixture.
    """
    import omnigent.runner.app as runner_app_mod

    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")

    session_id = "conv_fwd_recreate"
    runs: list[_ForwarderRun] = []

    async def _parking_forwarder(**kwargs: Any) -> None:
        """Park forever like the real restart-forever supervisor."""
        del kwargs
        run = _ForwarderRun(task=asyncio.current_task())
        runs.append(run)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            run.cancelled = True
            raise

    monkeypatch.setattr(
        "omnigent.claude_native_forwarder.supervise_forwarder",
        _parking_forwarder,
    )

    class _FakeResourceRegistry:
        """Captures terminal launches; no live terminal registry."""

        terminal_registry = None

        async def launch_required_terminal(
            self,
            *,
            session_id: str,
            terminal_name: str,
            session_key: str,
            spec: Any,
            resource_role: str | None = None,
            parent_os_env: Any = None,
        ) -> SessionResourceView:
            """Return a terminal resource view without launching tmux."""
            del terminal_name, session_key, spec, resource_role
            return SessionResourceView(
                id="terminal_claude_main",
                type="terminal",
                session_id=session_id,
                name="claude:main",
                metadata={"terminal_name": "claude", "session_key": "main", "running": True},
            )

    try:
        await _auto_create_claude_terminal(
            session_id,
            _FakeResourceRegistry(),  # type: ignore[arg-type]
            lambda _sid, _evt: None,
            server_client=NullServerClient(),  # type: ignore[arg-type]
        )
        # Let forwarder A start and park — in production the recovery
        # re-create fires long after the original create's task is running.
        await asyncio.sleep(0)

        # The recovery path: terminal resource gone, ensure re-creates.
        await _auto_create_claude_terminal(
            session_id,
            _FakeResourceRegistry(),  # type: ignore[arg-type]
            lambda _sid, _evt: None,
            server_client=NullServerClient(),  # type: ignore[arg-type]
        )
        await asyncio.sleep(0)

        # Both creates spawned a forwarder; the recovery one is the survivor.
        assert len(runs) == 2, (
            f"Expected 2 forwarder spawns (one per auto-create), got {len(runs)}."
        )
        # The first forwarder was cancelled by the re-create — a False here
        # is the production bug: two live tasks double-posting every record.
        assert runs[0].cancelled is True, (
            "Re-creating the claude terminal must cancel the prior session "
            "forwarder; it survived, so every post-recovery transcript "
            "record would be mirrored twice."
        )
        # The recovery's own forwarder survives — cancelled here means the
        # re-create killed its replacement and the session mirrors nothing.
        assert runs[1].cancelled is False
        live_runs = [run for run in runs if not run.cancelled]
        # Exactly one live forwarder mirrors the transcript for the session.
        assert len(live_runs) == 1
        # The registry holds exactly the live task for this session, keyed
        # by session id — this is the strong reference that keeps it alive.
        registered = runner_app_mod._AUTO_FORWARDER_TASKS.get(session_id)
        assert registered is live_runs[0].task
        # Still running: a done survivor would leave the session unmirrored.
        assert not registered.done()
    finally:
        runner_app_mod._AUTO_FORWARDER_TASKS.pop(session_id, None)
        await _drain_forwarder_runs(runs)


@pytest.mark.asyncio
async def test_auto_create_codex_terminal_recreate_cancels_prior_forwarder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Re-running codex terminal auto-create leaves exactly one live forwarder.

    Codex flavor of the claude double-mirror regression: the codex spawn
    registered its forwarder task in the same unkeyed set, so an ensure
    re-create for an existing session leaked the prior known-thread
    forwarder alongside the new one.

    :param tmp_path: Temporary directory for isolated bridge state.
    :param monkeypatch: Pytest monkeypatch fixture.
    """
    import omnigent.codex_native_app_server as codex_app_mod
    import omnigent.runner.app as runner_app_mod

    session_id = "conv_codex_fwd_recreate"
    thread_id = "019e96aa-0be2-7343-8d3b-6f914d60936b"
    monkeypatch.setattr(codex_native_bridge, "_BRIDGE_ROOT", tmp_path / "codex-bridge")
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://ap.example")
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
    monkeypatch.setattr("omnigent.runner._entry._make_auth_token_factory", lambda: None)

    class _SnapshotServerClient:
        """Server client returning a persisted resume thread + one item."""

        async def get(self, url: str, **kwargs: Any) -> httpx.Response:
            """
            Return the session snapshot / items consumed by the helper.

            :param url: Request path, e.g. ``"/v1/sessions/conv_..."``.
            :param kwargs: Request keyword arguments (ignored).
            :returns: HTTP 200 response carrying launch config or items.
            """
            del kwargs
            if url == f"/v1/sessions/{session_id}/items":
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "id": "msg_user_1",
                                "response_id": "codex_turn_1",
                                "type": "message",
                                "role": "user",
                                "content": [{"type": "input_text", "text": "remember this"}],
                            }
                        ],
                        "has_more": False,
                    },
                    request=httpx.Request("GET", url),
                )
            return httpx.Response(
                200,
                json={"external_session_id": thread_id},
                request=httpx.Request("GET", url),
            )

    class _FakeCodexAppServer:
        """Minimal app-server object used by ``codex_terminal_env``."""

        codex_path = "/opt/codex/bin/codex"

        def __init__(self) -> None:
            """:returns: None."""
            self.env = {"OPENAI_API_KEY": "sk-test"}
            self.codex_home = tmp_path / "unconfigured-codex-home"
            self.listen_url: str | None = None
            self.config_overrides: list[str] = []

        async def start(self) -> None:
            """:returns: None."""

        async def close(self) -> None:
            """:returns: None."""

    def _fake_build_codex_native_server(**kwargs: Any) -> _FakeCodexAppServer:
        """
        Build a fresh fake app-server per create call.

        :param kwargs: Keyword arguments passed by the runner helper.
        :returns: Fake app-server bound to the requested CODEX_HOME.
        """
        app_server = _FakeCodexAppServer()
        app_server.codex_home = kwargs["codex_home"]
        return app_server

    class _UnexpectedDiscoveryClient:
        """App-server client that must not connect on a known-thread resume."""

        def __init__(self, *, ws_url: str, client_name: str) -> None:
            """
            :param ws_url: App-server WebSocket URL.
            :param client_name: JSON-RPC client name.
            """
            self.ws_url = ws_url
            self.client_name = client_name

        async def connect(self) -> None:
            """Fail if the resume path tries to discover a fresh thread."""
            raise AssertionError("resume path must not connect discovery client")

        async def close(self) -> None:
            """:returns: None."""

    async def _fake_preload_thread(transport: str, loaded_thread_id: str) -> None:
        """
        No-op thread preload.

        :param transport: App-server transport URL.
        :param loaded_thread_id: Thread id passed to ``thread/resume``.
        :returns: None.
        """
        del transport, loaded_thread_id

    runs: list[_ForwarderRun] = []

    async def _parking_forward_known_thread(**kwargs: Any) -> None:
        """Park forever in place of the known-thread forwarder."""
        del kwargs
        run = _ForwarderRun(task=asyncio.current_task())
        runs.append(run)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            run.cancelled = True
            raise

    class _FakeResourceRegistry:
        """Returns a terminal resource view without launching tmux."""

        async def launch_auxiliary_terminal(
            self,
            *,
            session_id: str,
            terminal_name: str,
            session_key: str,
            spec: Any,
            resource_role: str | None = None,
            parent_os_env: Any = None,
        ) -> SessionResourceView:
            """Return a terminal resource view for the codex TUI."""
            del terminal_name, session_key, spec, resource_role
            return SessionResourceView(
                id="terminal_codex_main",
                type="terminal",
                session_id=session_id,
                name="Codex",
            )

    monkeypatch.setattr(
        codex_app_mod,
        "build_codex_native_server",
        _fake_build_codex_native_server,
    )
    monkeypatch.setattr(codex_app_mod, "CodexAppServerClient", _UnexpectedDiscoveryClient)
    monkeypatch.setattr(codex_app_mod, "preload_codex_thread_for_resume", _fake_preload_thread)
    monkeypatch.setattr(
        runner_app_mod,
        "_codex_forward_known_thread",
        _parking_forward_known_thread,
    )

    agent_spec = AgentSpec(
        spec_version=1,
        name="codex",
        executor=ExecutorSpec(
            type="omnigent",
            config={"harness": "codex-native", "model": "gpt-5-default"},
        ),
    )

    try:
        await _auto_create_codex_terminal(
            session_id,
            _FakeResourceRegistry(),  # type: ignore[arg-type]
            lambda _sid, _evt: None,
            agent_spec=agent_spec,
            server_client=_SnapshotServerClient(),  # type: ignore[arg-type]
        )
        await asyncio.sleep(0)

        await _auto_create_codex_terminal(
            session_id,
            _FakeResourceRegistry(),  # type: ignore[arg-type]
            lambda _sid, _evt: None,
            agent_spec=agent_spec,
            server_client=_SnapshotServerClient(),  # type: ignore[arg-type]
        )
        await asyncio.sleep(0)

        assert len(runs) == 2, (
            f"Expected 2 forwarder spawns (one per auto-create), got {len(runs)}."
        )
        # The first forwarder was cancelled by the re-create — a False here
        # means two live tasks mirror the same codex thread into the session.
        assert runs[0].cancelled is True, (
            "Re-creating the codex terminal must cancel the prior session "
            "forwarder; it survived, so transcript records would be "
            "double-posted."
        )
        # The recovery's own forwarder survives — cancelled here means the
        # re-create killed its replacement and the session mirrors nothing.
        assert runs[1].cancelled is False
        live_runs = [run for run in runs if not run.cancelled]
        # Exactly one live forwarder mirrors the thread for the session.
        assert len(live_runs) == 1
        registered = runner_app_mod._AUTO_FORWARDER_TASKS.get(session_id)
        assert registered is live_runs[0].task
        # Still running: a done survivor would leave the session unmirrored.
        assert not registered.done()
    finally:
        runner_app_mod._AUTO_FORWARDER_TASKS.pop(session_id, None)
        runner_app_mod._AUTO_CODEX_APP_SERVERS.pop(session_id, None)
        await _drain_forwarder_runs(runs)
