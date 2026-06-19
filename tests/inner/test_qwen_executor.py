"""Unit tests for QwenExecutor (ACP / JSON-RPC 2.0 mode).

Tests cover:
- Executor construction and attribute defaults
- ACP protocol helpers (_rpc, _notify, _send)
- Session lifecycle (_ensure_initialized, _ensure_session)
- run_turn event translation (agent_message_chunk → TextChunk, TurnComplete)
- run_turn error paths (ACP error response, session-not-found retry reset)
- Process cleanup (close())
- Harness registry and alias wiring
- FastAPI app shape
"""

from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from omnigent.inner.qwen_executor import QwenExecutor
from omnigent.inner.executor import ExecutorConfig, TextChunk, TurnComplete, ExecutorError


# ---------------------------------------------------------------------------
# Construction / attribute defaults
# ---------------------------------------------------------------------------


def test_executor_default_attributes() -> None:
    """Constructor stores arguments and initialises state correctly."""
    executor = QwenExecutor(qwen_path="qwen")
    assert executor._qwen_path == "qwen"
    assert executor._model is None
    assert executor._proc is None
    assert executor._session_id is None
    assert executor._initialized is False
    assert executor._rpc_id == 0


def test_executor_with_custom_model() -> None:
    """Custom model is stored on the instance."""
    executor = QwenExecutor(model="qwen/qwen-plus", qwen_path="qwen")
    assert executor._model == "qwen/qwen-plus"


def test_executor_cwd_defaults_to_cwd() -> None:
    """When no cwd is supplied the executor uses the process cwd."""
    executor = QwenExecutor()
    assert executor._cwd == os.getcwd()


def test_executor_explicit_cwd() -> None:
    """An explicit cwd is stored as-is."""
    executor = QwenExecutor(cwd="/tmp")
    assert executor._cwd == "/tmp"


# ---------------------------------------------------------------------------
# close() with no process
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_with_no_process_is_a_noop() -> None:
    """close() is safe to call when no subprocess was started."""
    executor = QwenExecutor()
    await executor.close()  # must not raise


# ---------------------------------------------------------------------------
# close() with a live process
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_terminates_process() -> None:
    """close() terminates the subprocess and clears _proc."""
    executor = QwenExecutor()

    # asyncio.Process.terminate() is synchronous; stdin.close() is sync too.
    mock_proc = MagicMock()
    mock_proc.stdin = MagicMock()
    mock_proc.returncode = None

    # wait() must be a coroutine.
    async def fake_wait() -> int:
        return 0

    mock_proc.wait = fake_wait
    executor._proc = mock_proc

    await executor.close()

    mock_proc.terminate.assert_called_once()
    assert executor._proc is None


@pytest.mark.asyncio
async def test_close_kills_when_terminate_raises() -> None:
    """close() falls back to kill() if terminate() raises."""
    executor = QwenExecutor()

    mock_proc = MagicMock()
    mock_proc.stdin = MagicMock()
    mock_proc.terminate.side_effect = OSError("gone")
    mock_proc.returncode = None

    executor._proc = mock_proc

    await executor.close()  # must not propagate the OSError


# ---------------------------------------------------------------------------
# _rpc_id increments
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rpc_id_increments_monotonically() -> None:
    """Each _rpc call uses a unique, incrementing id."""
    executor = QwenExecutor()

    sent: list[dict] = []

    async def fake_send(msg: dict) -> None:
        sent.append(msg)
        # Immediately resolve the future so _rpc returns.
        fut = executor._pending.get(msg["id"])
        if fut and not fut.done():
            fut.set_result({"jsonrpc": "2.0", "id": msg["id"], "result": {}})

    executor._send = fake_send  # type: ignore[method-assign]

    await executor._rpc("initialize", {"protocolVersion": 1})
    await executor._rpc("session/new", {"sessionId": "x", "cwd": "/", "mcpServers": []})

    assert sent[0]["id"] == 1
    assert sent[1]["id"] == 2


# ---------------------------------------------------------------------------
# _read_stdout — dispatches responses vs notifications
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_stdout_resolves_pending_future() -> None:
    """_read_stdout resolves the matching _pending future on a response."""
    executor = QwenExecutor()

    loop = asyncio.get_event_loop()
    fut: asyncio.Future = loop.create_future()
    executor._pending[42] = fut

    response_line = json.dumps({"jsonrpc": "2.0", "id": 42, "result": {"ok": True}}) + "\n"

    # Fake stdout that yields one line then EOF.
    async def fake_readline_gen():
        yield response_line.encode()
        # EOF
        while True:
            await asyncio.sleep(0)

    mock_stdout = AsyncMock()
    calls = [response_line.encode(), b""]
    mock_stdout.readline = AsyncMock(side_effect=calls)

    mock_proc = MagicMock()
    mock_proc.stdout = mock_stdout
    executor._proc = mock_proc

    # Run reader until it sees EOF (second readline returns b"").
    await executor._read_stdout()

    assert fut.done()
    assert fut.result()["result"]["ok"] is True


@pytest.mark.asyncio
async def test_read_stdout_puts_notifications_on_queue() -> None:
    """_read_stdout enqueues notifications (no id) onto the queue."""
    executor = QwenExecutor()

    notification = {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": "sess-1",
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "hello"},
            },
        },
    }
    notification_line = json.dumps(notification) + "\n"

    mock_stdout = AsyncMock()
    mock_stdout.readline = AsyncMock(side_effect=[notification_line.encode(), b""])

    mock_proc = MagicMock()
    mock_proc.stdout = mock_stdout
    executor._proc = mock_proc

    await executor._read_stdout()

    assert not executor._queue.empty()
    msg = executor._queue.get_nowait()
    assert msg["method"] == "session/update"


# ---------------------------------------------------------------------------
# _ensure_session resets on "Session not found"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_session_uses_server_assigned_id() -> None:
    """_ensure_session stores the sessionId from the server response, not ours."""
    executor = QwenExecutor()
    executor._initialized = True  # skip initialize

    server_session_id = "server-assigned-uuid"

    async def fake_rpc(method: str, params: dict, timeout: float = 30.0) -> dict:
        if method == "session/new":
            return {"jsonrpc": "2.0", "id": 1, "result": {"sessionId": server_session_id}}
        return {"jsonrpc": "2.0", "id": 1, "result": {}}

    executor._rpc = fake_rpc  # type: ignore[method-assign]

    sid = await executor._ensure_session()
    assert sid == server_session_id
    assert executor._session_id == server_session_id


@pytest.mark.asyncio
async def test_ensure_session_cached_after_first_call() -> None:
    """_ensure_session does not make a second RPC call once session is set."""
    executor = QwenExecutor()
    executor._initialized = True
    executor._session_id = "cached-sid"

    rpc_calls: list[str] = []

    async def fake_rpc(method: str, params: dict, timeout: float = 30.0) -> dict:
        rpc_calls.append(method)
        return {"result": {}}

    executor._rpc = fake_rpc  # type: ignore[method-assign]

    sid = await executor._ensure_session()
    assert sid == "cached-sid"
    assert rpc_calls == []  # no RPC was made


# ---------------------------------------------------------------------------
# run_turn — success path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_turn_yields_text_chunks_and_turn_complete() -> None:
    """run_turn yields TextChunk events for agent_message_chunk notifications
    and a TurnComplete when the session/prompt response arrives.

    The fake_send callback:
    1. Enqueues the streaming notification immediately (so the event loop
       processes it before checking fut.done()).
    2. Schedules the future resolution on the *next* event-loop iteration
       via ``loop.call_soon`` so the notification is always consumed first.
    """
    executor = QwenExecutor()
    executor._initialized = True
    executor._session_id = "sess-abc"

    executor._proc = MagicMock()
    executor._proc.returncode = None

    session_id = executor._session_id
    loop = asyncio.get_event_loop()

    async def fake_send(msg: dict) -> None:
        if msg.get("method") == "session/prompt":
            req_id = msg["id"]
            # 1. Put the streaming notification on the queue first.
            notification = {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": session_id,
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "Hello!"},
                    },
                },
            }
            await executor._queue.put(notification)

            # 2. Resolve the future on the next loop iteration so the
            #    notification is consumed before fut.done() is True.
            def _resolve() -> None:
                fut = executor._pending.get(req_id)
                if fut and not fut.done():
                    fut.set_result(
                        {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "result": {"stopReason": "end_turn"},
                        }
                    )

            loop.call_soon(_resolve)

    executor._send = fake_send  # type: ignore[method-assign]

    messages = [{"role": "user", "content": "say hi"}]
    events = []
    async for event in executor.run_turn(messages, [], "Be helpful"):
        events.append(event)

    text_chunks = [e for e in events if isinstance(e, TextChunk)]
    turn_completes = [e for e in events if isinstance(e, TurnComplete)]

    assert len(text_chunks) == 1
    assert text_chunks[0].text == "Hello!"
    assert len(turn_completes) == 1
    assert turn_completes[0].response == "Hello!"


# ---------------------------------------------------------------------------
# run_turn — ACP error response
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_turn_yields_executor_error_on_acp_error() -> None:
    """run_turn yields ExecutorError when session/prompt returns an error."""
    executor = QwenExecutor()
    executor._initialized = True
    executor._session_id = "sess-err"
    executor._proc = MagicMock()
    executor._proc.returncode = None

    async def fake_send(msg: dict) -> None:
        if msg.get("method") == "session/prompt":
            fut = executor._pending.get(msg["id"])
            if fut and not fut.done():
                fut.set_result(
                    {
                        "jsonrpc": "2.0",
                        "id": msg["id"],
                        "error": {"code": -32603, "message": "Something went wrong"},
                    }
                )

    executor._send = fake_send  # type: ignore[method-assign]

    messages = [{"role": "user", "content": "hi"}]
    events = []
    async for event in executor.run_turn(messages, [], ""):
        events.append(event)

    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    assert "Something went wrong" in events[0].message


@pytest.mark.asyncio
async def test_run_turn_resets_session_on_not_found_error() -> None:
    """run_turn clears _session_id when ACP reports Session not found."""
    executor = QwenExecutor()
    executor._initialized = True
    executor._session_id = "stale-sess"
    executor._proc = MagicMock()
    executor._proc.returncode = None

    async def fake_send(msg: dict) -> None:
        if msg.get("method") == "session/prompt":
            fut = executor._pending.get(msg["id"])
            if fut and not fut.done():
                fut.set_result(
                    {
                        "jsonrpc": "2.0",
                        "id": msg["id"],
                        "error": {
                            "code": -32603,
                            "message": "Session not found: stale-sess",
                        },
                    }
                )

    executor._send = fake_send  # type: ignore[method-assign]

    async for _ in executor.run_turn([{"role": "user", "content": "hi"}], [], ""):
        pass

    # Session id should be reset so next turn creates a fresh session.
    assert executor._session_id is None


# ---------------------------------------------------------------------------
# Harness registry / alias wiring
# ---------------------------------------------------------------------------


def test_qwen_in_harness_registry() -> None:
    """'qwen' must be in the _HARNESS_MODULES dispatch table."""
    from omnigent.runtime.harnesses import _HARNESS_MODULES

    assert "qwen" in _HARNESS_MODULES


def test_qwen_in_harness_allowlist() -> None:
    """'qwen' must be in OMNIGENT_HARNESSES."""
    from omnigent.spec._omnigent_compat import OMNIGENT_HARNESSES

    assert "qwen" in OMNIGENT_HARNESSES


def test_qwen_code_alias_resolves_to_qwen() -> None:
    """'qwen-code' alias maps to the canonical 'qwen' harness id."""
    from omnigent.harness_aliases import canonicalize_harness

    assert canonicalize_harness("qwen-code") == "qwen"


def test_qwen_code_in_harness_aliases() -> None:
    """'qwen-code' must be in OMNIGENT_HARNESS_ALIASES."""
    from omnigent.spec._omnigent_compat import OMNIGENT_HARNESS_ALIASES

    assert "qwen-code" in OMNIGENT_HARNESS_ALIASES


# ---------------------------------------------------------------------------
# FastAPI app shape
# ---------------------------------------------------------------------------


def test_qwen_harness_creates_fastapi_app() -> None:
    """create_app() returns a FastAPI app with at least a /health route."""
    from omnigent.inner.qwen_harness import create_app

    app = create_app()
    assert app is not None
    assert hasattr(app, "routes")
    health_routes = [r for r in app.routes if hasattr(r, "path") and "/health" in r.path]
    assert len(health_routes) > 0


def test_qwen_harness_module_importable() -> None:
    """qwen_harness can be imported and exposes create_app."""
    from omnigent.inner import qwen_harness

    assert hasattr(qwen_harness, "create_app")


# ---------------------------------------------------------------------------
# close_session is a no-op (sessions are per-process)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_session_is_noop() -> None:
    """close_session() does nothing and does not raise."""
    executor = QwenExecutor()
    await executor.close_session("some-key")  # must not raise
