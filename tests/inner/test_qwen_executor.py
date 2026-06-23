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
from unittest.mock import AsyncMock, MagicMock

import pytest

from omnigent.inner.executor import ExecutorError, TextChunk, TurnComplete
from omnigent.inner.qwen_executor import QwenExecutor

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

    # Fake stdout that yields one line then EOF (b"" on the second readline).
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


def test_wrap_passes_gateway_env_to_executor(monkeypatch: pytest.MonkeyPatch) -> None:
    """_build_qwen_executor threads HARNESS_QWEN_GATEWAY_* into the executor."""
    from omnigent.inner import qwen_harness

    monkeypatch.setenv("HARNESS_QWEN_MODEL", "qwen/qwen3-coder")
    monkeypatch.setenv("HARNESS_QWEN_GATEWAY_BASE_URL", "https://gw.example/v1")
    monkeypatch.setenv("HARNESS_QWEN_GATEWAY_AUTH_COMMAND", "printf '%s' sk-x")

    executor = qwen_harness._build_qwen_executor()
    assert isinstance(executor, QwenExecutor)
    assert executor._gateway_base_url == "https://gw.example/v1"
    assert executor._gateway_auth_command == "printf '%s' sk-x"


def test_wrap_gateway_env_absent_leaves_executor_ungated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the gateway env vars, the executor has no gateway config."""
    from omnigent.inner import qwen_harness

    monkeypatch.delenv("HARNESS_QWEN_GATEWAY_BASE_URL", raising=False)
    monkeypatch.delenv("HARNESS_QWEN_GATEWAY_AUTH_COMMAND", raising=False)

    executor = qwen_harness._build_qwen_executor()
    assert isinstance(executor, QwenExecutor)
    assert executor._gateway_base_url is None
    assert executor._gateway_auth_command is None


# ---------------------------------------------------------------------------
# close_session is a no-op (sessions are per-process)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_session_is_noop() -> None:
    """close_session() does nothing and does not raise."""
    executor = QwenExecutor()
    await executor.close_session("some-key")  # must not raise


# ---------------------------------------------------------------------------
# system_prompt folded into the first turn (ACP has no system field)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_turn_prepends_system_prompt_on_first_turn() -> None:
    """The system prompt is prepended to the first user turn's text.

    ACP has no dedicated system-prompt field, so the spec ``prompt:`` would
    otherwise never reach qwen. The second turn must NOT repeat it (the
    session retains context).
    """
    executor = QwenExecutor()
    executor._initialized = True
    executor._session_id = "sess-sp"
    executor._proc = MagicMock()
    executor._proc.returncode = None
    loop = asyncio.get_event_loop()

    sent_prompts: list[str] = []

    async def fake_send(msg: dict) -> None:
        if msg.get("method") == "session/prompt":
            sent_prompts.append(msg["params"]["prompt"][0]["text"])
            req_id = msg["id"]

            def _resolve() -> None:
                fut = executor._pending.get(req_id)
                if fut and not fut.done():
                    fut.set_result(
                        {"jsonrpc": "2.0", "id": req_id, "result": {"stopReason": "end_turn"}}
                    )

            loop.call_soon(_resolve)

    executor._send = fake_send  # type: ignore[method-assign]

    messages = [{"role": "user", "content": "first"}]
    async for _ in executor.run_turn(messages, [], "SYSTEM RULES"):
        pass
    async for _ in executor.run_turn([{"role": "user", "content": "second"}], [], "SYSTEM RULES"):
        pass

    assert sent_prompts[0] == "SYSTEM RULES\n\nfirst"
    assert sent_prompts[1] == "second"  # not repeated
    assert executor._system_prompt_sent is True


@pytest.mark.asyncio
async def test_run_turn_resends_system_prompt_after_session_reset() -> None:
    """After a 'Session not found' reset, the next turn re-folds the system prompt.

    Losing the session clears ``_system_prompt_sent`` so the fresh session
    receives the spec prompt again (qwen no longer holds the earlier context).
    """
    executor = QwenExecutor()
    executor._initialized = True
    executor._session_id = "sess-1"
    executor._proc = MagicMock()
    executor._proc.returncode = None

    sent_prompts: list[str] = []
    fail_next = {"flag": True}  # turn 1's prompt is rejected with "Session not found"

    async def fake_send(msg: dict) -> None:
        method = msg.get("method")
        fut = executor._pending.get(msg["id"])
        if method == "session/new":
            # Fresh session created after the reset.
            if fut and not fut.done():
                fut.set_result(
                    {"jsonrpc": "2.0", "id": msg["id"], "result": {"sessionId": "sess-2"}}
                )
        elif method == "session/prompt":
            sent_prompts.append(msg["params"]["prompt"][0]["text"])
            if fut and not fut.done():
                if fail_next["flag"]:
                    fail_next["flag"] = False
                    fut.set_result(
                        {
                            "jsonrpc": "2.0",
                            "id": msg["id"],
                            "error": {"code": -32603, "message": "Session not found: sess-1"},
                        }
                    )
                else:
                    fut.set_result(
                        {"jsonrpc": "2.0", "id": msg["id"], "result": {"stopReason": "end_turn"}}
                    )

    executor._send = fake_send  # type: ignore[method-assign]

    # Turn 1: folds the prompt, then errors with "Session not found" → reset.
    async for _ in executor.run_turn([{"role": "user", "content": "first"}], [], "SYSTEM"):
        pass
    # Turn 2: fresh session — the system prompt must be re-folded.
    async for _ in executor.run_turn([{"role": "user", "content": "second"}], [], "SYSTEM"):
        pass

    assert sent_prompts[0] == "SYSTEM\n\nfirst"  # turn 1 folded
    assert sent_prompts[1] == "SYSTEM\n\nsecond"  # re-folded into the new session
    assert executor._session_id == "sess-2"


# ---------------------------------------------------------------------------
# Server-initiated requests: permission, unsupported methods (incl. fs/*)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_respond_to_fs_read_text_file_is_unsupported() -> None:
    """fs/* is intentionally unsupported (no clientCapabilities.fs advertised).

    qwen never delegates file ops to us, so the handlers were removed; an
    ``fs/read_text_file`` request must get a JSON-RPC method-not-found error
    rather than a fabricated (and dangerous) empty/real-file response.
    """
    executor = QwenExecutor()
    sent: list[dict] = []
    executor._send = AsyncMock(side_effect=lambda m: sent.append(m))  # type: ignore[method-assign]

    await executor._respond_to_agent_request(
        {"jsonrpc": "2.0", "id": 7, "method": "fs/read_text_file", "params": {"path": "/x"}}
    )

    assert sent[0]["id"] == 7
    assert sent[0]["error"]["code"] == -32601
    assert "result" not in sent[0]


# Realistic qwen session/request_permission payload (from the ACP probe).
def _perm_request(req_id: int = 9) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": "session/request_permission",
        "params": {
            "sessionId": "s",
            "options": [
                {"optionId": "proceed_always_project", "kind": "allow_always"},
                {"optionId": "proceed_once", "kind": "allow_once"},
                {"optionId": "cancel", "kind": "reject_once"},
            ],
            "toolCall": {
                "kind": "execute",
                "rawInput": {"command": "rm -f victim.txt"},
                "_meta": {"toolName": "run_shell_command"},
            },
        },
    }


@pytest.mark.asyncio
async def test_respond_to_permission_allows_when_no_gates_wired() -> None:
    """With no policy/elicitation bridge wired, permission falls back to allow.

    Prefers the once-scoped grant (``allow_once``), never ``allow_always``.
    """
    executor = QwenExecutor()  # no _policy_evaluator / _elicitation_handler
    sent: list[dict] = []
    executor._send = AsyncMock(side_effect=lambda m: sent.append(m))  # type: ignore[method-assign]

    await executor._respond_to_agent_request(_perm_request())

    assert sent[0]["result"]["outcome"] == {"outcome": "selected", "optionId": "proceed_once"}


@pytest.mark.asyncio
async def test_respond_to_permission_denied_by_policy() -> None:
    """A POLICY_ACTION_DENY verdict selects a reject option — no elicitation."""
    executor = QwenExecutor()
    executor._policy_evaluator = AsyncMock(  # type: ignore[attr-defined]
        return_value=MagicMock(action="POLICY_ACTION_DENY")
    )
    executor._elicitation_handler = AsyncMock(return_value=True)  # type: ignore[attr-defined]
    sent: list[dict] = []
    executor._send = AsyncMock(side_effect=lambda m: sent.append(m))  # type: ignore[method-assign]

    await executor._respond_to_agent_request(_perm_request())

    assert sent[0]["result"]["outcome"] == {"outcome": "selected", "optionId": "cancel"}
    executor._elicitation_handler.assert_not_called()  # policy DENY short-circuits
    # Policy saw the real tool name + args extracted from the payload.
    phase, data = executor._policy_evaluator.call_args.args
    assert phase == "PHASE_TOOL_CALL"
    assert data == {"name": "run_shell_command", "arguments": {"command": "rm -f victim.txt"}}


@pytest.mark.asyncio
async def test_respond_to_permission_elicitation_allow_and_deny() -> None:
    """With only elicitation wired, the user's accept/deny maps to allow/reject."""
    # Accept → allow_once.
    allow_exec = QwenExecutor()
    allow_exec._elicitation_handler = AsyncMock(return_value=True)  # type: ignore[attr-defined]
    sent_a: list[dict] = []
    allow_exec._send = AsyncMock(side_effect=lambda m: sent_a.append(m))  # type: ignore[method-assign]
    await allow_exec._respond_to_agent_request(_perm_request())
    assert sent_a[0]["result"]["outcome"] == {"outcome": "selected", "optionId": "proceed_once"}
    allow_exec._elicitation_handler.assert_awaited_once_with(
        "run_shell_command", {"command": "rm -f victim.txt"}
    )

    # Deny → reject_once.
    deny_exec = QwenExecutor()
    deny_exec._elicitation_handler = AsyncMock(return_value=False)  # type: ignore[attr-defined]
    sent_d: list[dict] = []
    deny_exec._send = AsyncMock(side_effect=lambda m: sent_d.append(m))  # type: ignore[method-assign]
    await deny_exec._respond_to_agent_request(_perm_request())
    assert sent_d[0]["result"]["outcome"] == {"outcome": "selected", "optionId": "cancel"}


@pytest.mark.asyncio
async def test_respond_to_unknown_method_returns_jsonrpc_error() -> None:
    """An unsupported server request yields a method-not-found error, not {}."""
    executor = QwenExecutor()
    sent: list[dict] = []
    executor._send = AsyncMock(side_effect=lambda m: sent.append(m))  # type: ignore[method-assign]

    await executor._respond_to_agent_request(
        {"jsonrpc": "2.0", "id": 11, "method": "terminal/create", "params": {}}
    )

    assert sent[0]["error"]["code"] == -32601
    assert "result" not in sent[0]


# ---------------------------------------------------------------------------
# stderr is drained so a chatty CLI can't wedge the pipe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_stderr_drains_until_eof() -> None:
    """_read_stderr consumes lines and exits cleanly on EOF."""
    executor = QwenExecutor()
    mock_stderr = AsyncMock()
    mock_stderr.readline = AsyncMock(side_effect=[b"warn: something\n", b""])
    mock_proc = MagicMock()
    mock_proc.stderr = mock_stderr
    executor._proc = mock_proc

    await executor._read_stderr()  # must terminate at EOF, not hang

    assert mock_stderr.readline.await_count == 2


# ---------------------------------------------------------------------------
# Missing-binary path surfaces a clear error on first turn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_turn_missing_binary_yields_retryable_error() -> None:
    """A non-existent qwen binary surfaces as an ExecutorError, not a crash."""
    executor = QwenExecutor(qwen_path="/nonexistent/qwen-binary-xyz")

    events = []
    async for event in executor.run_turn([{"role": "user", "content": "hi"}], [], "be helpful"):
        events.append(event)

    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)


# ---------------------------------------------------------------------------
# Content-block extraction (file attachments must not drop the message)
# ---------------------------------------------------------------------------


def test_text_from_blocks_recovers_input_text_and_marks_file() -> None:
    """A message with input_text + input_file keeps the text and notes the file.

    Regression: the old ``type == "text"`` filter matched neither ``input_text``
    nor ``input_file``, so attaching a file dropped the ENTIRE message.
    """
    blocks = [
        {"type": "input_text", "text": "review this"},
        {"type": "input_file", "file_id": "f_1", "filename": "foo.py"},
    ]
    out = QwenExecutor._text_from_blocks(blocks)
    assert "review this" in out
    assert "[attached file: foo.py]" in out


def test_text_from_blocks_inlines_text_file_data() -> None:
    """A text input_file with a base64 data URI is inlined into the prompt."""
    import base64

    payload = base64.b64encode(b"print('hi')").decode()
    blocks = [
        {"type": "input_text", "text": "summarize"},
        {
            "type": "input_file",
            "file_data": f"data:text/x-python;base64,{payload}",
            "filename": "a.py",
        },
    ]
    out = QwenExecutor._text_from_blocks(blocks)
    assert "summarize" in out
    assert "print('hi')" in out
    # Content is fenced with a labeled header/footer so weaker models read it as
    # an attachment, not instructions (regression: bare-appended file content
    # derailed qwen3-coder:free into narrating the tool call as prose).
    assert "--- attached file: a.py ---" in out
    assert "--- end of a.py ---" in out


def test_text_from_blocks_skips_image_and_marks_binary_file() -> None:
    """Images are skipped (deferred); binary files fall back to a name marker."""
    import base64

    payload = base64.b64encode(b"%PDF-1.4").decode()
    blocks = [
        {"type": "input_image", "file_id": "img"},
        {
            "type": "input_file",
            "file_data": f"data:application/pdf;base64,{payload}",
            "filename": "d.pdf",
        },
    ]
    out = QwenExecutor._text_from_blocks(blocks)
    assert out == "[attached file: d.pdf]"


def test_image_blocks_from_content_builds_acp_image_block() -> None:
    """An input_image with a resolved data URI becomes an ACP image block."""
    import base64

    payload = base64.b64encode(b"\x89PNG...").decode()
    content = [
        {"type": "input_text", "text": "what is this"},
        {"type": "input_image", "image_url": f"data:image/png;base64,{payload}"},
    ]
    blocks = QwenExecutor._image_blocks_from_content(content)
    assert blocks == [{"type": "image", "mimeType": "image/png", "data": payload}]


def test_image_blocks_from_content_skips_non_image_and_external_urls() -> None:
    """Only inline image data URIs are forwarded; URLs/non-images are skipped."""
    content = [
        {"type": "input_image", "image_url": "https://example.com/cat.png"},
        {"type": "input_file", "file_data": "data:text/plain;base64,aGk="},
        {"type": "input_image", "image_url": "data:application/pdf;base64,JVBE"},
    ]
    assert QwenExecutor._image_blocks_from_content(content) == []


def test_image_blocks_from_content_uses_file_data_fallback() -> None:
    """An input_image carrying its data URI in file_data (not image_url) works."""
    content = [
        {"type": "input_image", "file_data": "data:image/jpeg;base64,/9j/4AAQ"},
    ]
    assert QwenExecutor._image_blocks_from_content(content) == [
        {"type": "image", "mimeType": "image/jpeg", "data": "/9j/4AAQ"}
    ]


def test_parse_image_data_uri_edge_cases() -> None:
    """Malformed / non-image data URIs return None rather than raising."""
    from omnigent.inner.qwen_executor import _parse_image_data_uri

    assert _parse_image_data_uri("data:image/png;base64") is None  # no comma
    assert _parse_image_data_uri("data:image/png;base64,") is None  # empty payload
    assert _parse_image_data_uri("https://example.com/x.png") is None  # not a data URI
    assert _parse_image_data_uri(None) is None
    assert _parse_image_data_uri("data:image/webp;base64,UklGR") == ("image/webp", "UklGR")


def test_text_from_blocks_marks_image_only_when_requested() -> None:
    """Image markers appear only with emit_image_marker (capability-off path)."""
    content = [
        {"type": "input_text", "text": "what is this"},
        {"type": "input_image", "image_url": "data:image/png;base64,iVBOR", "filename": "p.png"},
    ]
    # Default: image handled as a real block elsewhere → no marker here.
    assert QwenExecutor._text_from_blocks(content) == "what is this"
    # Capability off: leave a marker so the image isn't silently dropped.
    marked = QwenExecutor._text_from_blocks(content, emit_image_marker=True)
    assert "what is this" in marked
    assert "[attached image: p.png]" in marked


@pytest.mark.asyncio
async def test_resolve_gateway_env_runs_auth_command() -> None:
    """A wired gateway → OPENAI_* env with the token from the auth command."""
    executor = QwenExecutor(
        model="qwen/qwen3-coder",
        gateway_base_url="https://gw.example/v1",
        gateway_auth_command="printf '%s' sk-tok-123",
    )
    env = await executor._resolve_gateway_env()
    assert env == {
        "OPENAI_BASE_URL": "https://gw.example/v1",
        "OPENAI_API_KEY": "sk-tok-123",
        "OPENAI_MODEL": "qwen/qwen3-coder",
    }


@pytest.mark.asyncio
async def test_resolve_gateway_env_empty_without_config() -> None:
    """No gateway configured → no OPENAI_* overrides (ambient auth path)."""
    assert await QwenExecutor(model="m")._resolve_gateway_env() == {}
    # base URL without an auth command is also inert.
    only_url = QwenExecutor(gateway_base_url="https://gw/v1")
    assert await only_url._resolve_gateway_env() == {}


@pytest.mark.asyncio
async def test_resolve_gateway_env_raises_on_command_failure() -> None:
    """A failing auth command surfaces a clear error rather than an empty key."""
    executor = QwenExecutor(
        gateway_base_url="https://gw/v1",
        gateway_auth_command="exit 3",
    )
    with pytest.raises(RuntimeError, match="auth command failed"):
        await executor._resolve_gateway_env()


@pytest.mark.asyncio
async def test_resolve_gateway_env_raises_on_empty_token() -> None:
    """An auth command that prints nothing is treated as a failure."""
    executor = QwenExecutor(
        gateway_base_url="https://gw/v1",
        gateway_auth_command="true",  # exits 0, no stdout
    )
    with pytest.raises(RuntimeError, match="empty token"):
        await executor._resolve_gateway_env()


@pytest.mark.asyncio
async def test_resolve_gateway_env_omits_model_when_unset() -> None:
    """Without a model, only base URL + key are exported (no OPENAI_MODEL)."""
    executor = QwenExecutor(
        gateway_base_url="https://gw/v1",
        gateway_auth_command="printf '%s' k",
    )
    env = await executor._resolve_gateway_env()
    assert env == {"OPENAI_BASE_URL": "https://gw/v1", "OPENAI_API_KEY": "k"}


@pytest.mark.asyncio
async def test_ensure_initialized_captures_image_capability() -> None:
    """initialize handshake records promptCapabilities.image on the executor."""
    executor = QwenExecutor(model="m")
    executor._rpc = AsyncMock(  # type: ignore[method-assign]
        return_value={"result": {"agentCapabilities": {"promptCapabilities": {"image": True}}}}
    )
    await executor._ensure_initialized()
    assert executor._initialized is True
    assert executor._image_supported is True


@pytest.mark.asyncio
async def test_ensure_initialized_image_capability_defaults_false() -> None:
    """Absent promptCapabilities leaves image support off (degrade to marker)."""
    executor = QwenExecutor(model="m")
    executor._rpc = AsyncMock(return_value={"result": {}})  # type: ignore[method-assign]
    await executor._ensure_initialized()
    assert executor._initialized is True
    assert executor._image_supported is False
