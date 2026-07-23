"""Shared scaffolding for runner sessions-native tests."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from omnigent.runner import create_runner_app
from omnigent.runner.mcp_manager import McpSchemasResult
from omnigent.spec.types import AgentSpec, ExecutorSpec, MCPServerConfig
from tests.runner.helpers import NullServerClient


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


def _interrupt_markers(histories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the synthetic ``[System: interrupted]`` marker messages."""
    return [
        h
        for h in histories
        if h.get("type") == "message"
        and h.get("role") == "user"
        and any("interrupted" in (b.get("text") or "").lower() for b in h.get("content", []))
    ]


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
        # In-flight tracking the runner wires up on response.created /
        # stream end. Recorded so tests can assert the
        # idle reaper's guard is actually populated for a live turn.
        self.marked_in_flight: list[tuple[str, str]] = []
        self.cleared_in_flight: list[str] = []

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

    def mark_in_flight(self, conversation_id: str, response_id: str) -> None:
        """Record a live turn, mirroring the real manager's reaper guard."""
        self.marked_in_flight.append((conversation_id, response_id))
        self._active_turns.add(conversation_id)

    def clear_in_flight(self, conversation_id: str) -> None:
        """Clear the live-turn marker at stream end."""
        self.cleared_in_flight.append(conversation_id)
        self._active_turns.discard(conversation_id)

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
            payload={
                "id": "07b38328508bae2010c8b9933a310846",
                "filename": "photo.png",
                "content_type": "image/png",
            }
        )


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
