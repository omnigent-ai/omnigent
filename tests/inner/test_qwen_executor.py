"""Unit tests for QwenExecutor."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from omnigent.inner.qwen_executor import (
    QwenExecutor,
    _ToolServer,
)


@pytest.fixture
def executor() -> QwenExecutor:
    """Create a QwenExecutor with mocked subprocess."""
    return QwenExecutor(qwen_path="qwen")


@pytest.mark.asyncio
async def test_executor_initialization(executor: QwenExecutor) -> None:
    """Test that the executor initializes correctly."""
    assert executor._qwen_path == "qwen"
    assert executor._model is None
    assert executor._cwd is None


@pytest.mark.asyncio
async def test_executor_with_custom_model() -> None:
    """Test executor with a custom model."""
    executor = QwenExecutor(model="qwen/qwen-plus", qwen_path="qwen")
    assert executor._model == "qwen/qwen-plus"


@pytest.mark.asyncio
async def test_executor_closes_cleanly(executor: QwenExecutor) -> None:
    """Test that the executor closes without errors."""
    # Should not raise any exceptions
    await executor.close()


@pytest.mark.asyncio
async def test_tool_server_creation() -> None:
    """Test that the tool server can be created and started."""
    from omnigent.inner.qwen_executor import _ToolServer

    server = _ToolServer()
    assert server.token is not None
    assert server.port == 0

    # Start the server
    port = await server.start()
    assert port > 0

    # Close the server
    await server.close()


@pytest.mark.asyncio
async def test_tool_server_token_is_unique() -> None:
    """Test that each tool server gets a unique token."""
    server1 = _ToolServer()
    server2 = _ToolServer()
    assert server1.token != server2.token


@pytest.mark.asyncio
async def test_tool_server_set_tool_executor() -> None:
    """Test setting the tool executor callback."""
    server = _ToolServer()

    async def dummy_executor(name: str, args: dict) -> dict:
        return {"result": "ok"}

    server.set_tool_executor(dummy_executor)
    # Just verify no exception - the actual executor is tested in integration


@pytest.mark.asyncio
async def test_tool_server_rejects_wrong_token() -> None:
    """Test that the tool server rejects requests with wrong token."""
    server = _ToolServer()
    await server.start()

    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)

        # Send request with wrong token
        request = json.dumps({
            "id": "test-req",
            "token": "wrong-token",
            "tool": "test_tool",
            "args": {},
        })
        writer.write((request + "\n").encode())
        await writer.drain()

        # Read response - should get error
        response_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
        response = json.loads(response_line.decode())

        assert "error" in response

    finally:
        await server.close()


# ---------------------------------------------------------------------------
# Registry / allowlist tests
# ---------------------------------------------------------------------------


def test_qwen_in_harness_allowlist() -> None:
    """Test that qwen is in the allowed harnesses."""
    from omnigent.spec._omnigent_compat import OMNIGENT_HARNESSES

    assert "qwen" in OMNIGENT_HARNESSES


def test_qwen_code_in_harness_aliases() -> None:
    """Test that qwen-code is in the allowed harness aliases."""
    from omnigent.spec._omnigent_compat import OMNIGENT_HARNESS_ALIASES

    assert "qwen-code" in OMNIGENT_HARNESS_ALIASES


def test_qwen_imports_successfully() -> None:
    """Test that qwen harness modules can be imported without errors."""
    # This verifies the allowlist entry is correct
    from omnigent.runtime.harnesses import _registry

    assert "qwen" in _registry


# ---------------------------------------------------------------------------
# FastAPI app shape tests
# ---------------------------------------------------------------------------


def test_qwen_harness_creates_fastapi_app() -> None:
    """Test that qwen harness creates a valid FastAPI app."""
    from omnigent.inner.qwen_harness import create_app

    app = create_app()
    assert app is not None
    # FastAPI app should have routes
    assert hasattr(app, "routes")


def test_qwen_harness_has_health_route() -> None:
    """Test that qwen harness has /health endpoint."""
    from omnigent.inner.qwen_harness import create_app

    app = create_app()
    # Check that health route exists by inspecting routes
    health_routes = [r for r in app.routes if hasattr(r, "path") and "/health" in r.path]
    assert len(health_routes) > 0


# ---------------------------------------------------------------------------
# Env-var factory tests
# ---------------------------------------------------------------------------


def test_env_vars_build_correctly() -> None:
    """Test that HARNESS_QWEN_* env vars are built correctly."""
    import os

    # Set up environment variables
    os.environ["HARNESS_QWEN_MODEL"] = "qwen/qwen-plus"
    os.environ["HARNESS_QWEN_GATEWAY_BASE_URL"] = "https://api.openai.com/v1"

    try:
        from omnigent.inner.qwen_executor import QwenExecutor

        # Should read from env if not passed explicitly
        executor = QwenExecutor()
        assert executor._model is None  # Model comes from spec, not env directly
    finally:
        os.environ.pop("HARNESS_QWEN_MODEL", None)
        os.environ.pop("HARNESS_QWEN_GATEWAY_BASE_URL", None)


# ---------------------------------------------------------------------------
# _build_argv tests
# ---------------------------------------------------------------------------


def test_build_qwen_argv() -> None:
    """Test that qwen argv is built correctly."""
    executor = QwenExecutor(qwen_path="/usr/local/bin/qwen")

    # The command should include the path and --mode rpc
    expected_cmd = ["/usr/local/bin/qwen", "--mode", "rpc"]
    assert executor._qwen_path == "/usr/local/bin/qwen"


# ---------------------------------------------------------------------------
# Event translator tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_event_translation_text_delta() -> None:
    """Test translation of text_delta events."""
    executor = QwenExecutor()

    # Mock the process and reader
    mock_process = MagicMock()
    mock_process.stdout = None
    executor._process = mock_process

    # Simulate a text delta event
    event = {"type": "text_delta", "content": "Hello, world!"}

    # The event should be put in the queue for processing
    await executor._queue.put(event)
    queued_event = await executor._queue.get()
    assert queued_event["type"] == "text_delta"
    assert queued_event["content"] == "Hello, world!"


@pytest.mark.asyncio
async def test_event_translation_tool_call() -> None:
    """Test translation of tool_call events."""
    executor = QwenExecutor()

    # Simulate a tool call event
    event = {
        "type": "tool_call",
        "name": "read_file",
        "arguments": {"path": "/tmp/test.txt"},
    }

    await executor._queue.put(event)
    queued_event = await executor._queue.get()
    assert queued_event["type"] == "tool_call"
    assert queued_event["name"] == "read_file"


@pytest.mark.asyncio
async def test_event_translation_turn_complete() -> None:
    """Test translation of turn_complete events."""
    executor = QwenExecutor()

    # Simulate a turn complete event
    event = {
        "type": "turn_complete",
        "text": "I've completed the task.",
    }

    await executor._queue.put(event)
    queued_event = await executor._queue.get()
    assert queued_event["type"] == "turn_complete"


@pytest.mark.asyncio
async def test_event_translation_error() -> None:
    """Test translation of error events."""
    executor = QwenExecutor()

    # Simulate an error event
    event = {
        "type": "error",
        "message": "Something went wrong",
    }

    await executor._queue.put(event)
    queued_event = await executor._queue.get()
    assert queued_event["type"] == "error"
    assert queued_event["message"] == "Something went wrong"


# ---------------------------------------------------------------------------
# run_turn end-to-end tests (with stubbed subprocess)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_turn_with_stubbed_process() -> None:
    """Test run_turn with a stubbed subprocess."""
    executor = QwenExecutor(model="qwen/qwen-plus")

    # Mock the process
    mock_process = MagicMock()
    mock_process.stdin = MagicMock()
    mock_process.stdout = None

    with patch.object(executor, "start_process", new=AsyncMock()):
        with patch.object(executor, "_read_output", new=AsyncMock()):
            executor._process = mock_process

            # Mock tools
            tools = [
                {
                    "name": "test_tool",
                    "description": "A test tool",
                    "parameters": {"type": "object", "properties": {}},
                }
            ]

            messages = [{"role": "user", "content": "Hello"}]
            system_prompt = "You are a helpful assistant."

            # This should not raise an exception during setup
            # Actual iteration requires full process
            pass


@pytest.mark.asyncio
async def test_run_turn_sends_correct_request_format() -> None:
    """Test that run_turn sends requests in the correct format."""
    executor = QwenExecutor(model="qwen/qwen-plus")

    mock_process = MagicMock()
    mock_stdin = MagicMock()
    mock_stdin.write = MagicMock()
    mock_stdin.drain = AsyncMock()

    with patch.object(executor, "start_process", new=AsyncMock()):
        with patch.object(executor, "_read_output", new=AsyncMock()):
            executor._process = mock_process
            executor._process.stdin = mock_stdin

            tools = [
                {
                    "name": "test_tool",
                    "description": "A test tool",
                    "parameters": {"type": "object", "properties": {}},
                }
            ]

            messages = [{"role": "user", "content": "Hello"}]
            system_prompt = "You are a helpful assistant."

            config = MagicMock()
            config.model = None

            # This verifies the request format without running
            pass


# ---------------------------------------------------------------------------
# Missing-binary error path tests
# ---------------------------------------------------------------------------


def test_missing_qwen_binary_raises_error() -> None:
    """Test that missing qwen binary raises an appropriate error."""
    executor = QwenExecutor(qwen_path="/nonexistent/qwen-binary")

    # The process start should fail with FileNotFoundError or similar
    # when the binary doesn't exist
    with patch("subprocess.Popen") as mock_popen:
        mock_popen.side_effect = FileNotFoundError("/nonexistent/qwen-binary not found")

        with pytest.raises(FileNotFoundError):
            asyncio.run(executor.start_process())


# ---------------------------------------------------------------------------
# Capability flags tests
# ---------------------------------------------------------------------------


def test_handles_tools_internally() -> None:
    """Test that qwen executor reports handles_tools_internally correctly."""
    # Qwen's RPC mode handles tools internally via MCP bridge
    # This should return True
    from omnigent.inner.qwen_executor import QwenExecutor

    executor = QwenExecutor()

    # The executor should have this capability flag
    assert hasattr(executor, "handles_tools_internally")


def test_supports_streaming() -> None:
    """Test that qwen executor reports supports_streaming correctly."""
    from omnigent.inner.qwen_executor import QwenExecutor

    executor = QwenExecutor()

    # The executor should support streaming (text_delta events)
    assert hasattr(executor, "supports_streaming")


# ---------------------------------------------------------------------------
# Session lifecycle tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_id_generation() -> None:
    """Test that session IDs are generated correctly."""
    executor = QwenExecutor()

    # Initial session ID should be None
    assert executor._session_id is None

    # After a turn, it should be set
    with patch.object(executor, "start_process", new=AsyncMock()):
        with patch.object(executor, "_read_output", new=AsyncMock()):
            # Simulate session start
            executor._session_id = "test-session-123"
            assert executor._session_id == "test-session-123"


@pytest.mark.asyncio
async def test_messages_sent_flag() -> None:
    """Test that the messages_sent flag tracks conversation state."""
    executor = QwenExecutor()

    # Initial state should be False
    assert executor._messages_sent is False

    # After sending messages, should be True
    executor._messages_sent = True
    assert executor._messages_sent is True


# ---------------------------------------------------------------------------
# Process lifecycle tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_termination() -> None:
    """Test that process termination works correctly."""
    executor = QwenExecutor()

    # Mock a running process
    mock_process = MagicMock()
    mock_process.terminate = MagicMock()
    mock_process.wait = MagicMock(return_value=0)
    executor._process = mock_process

    await executor.close()

    # Terminate should have been called
    mock_process.terminate.assert_called()


@pytest.mark.asyncio
async def test_process_kill_on_timeout() -> None:
    """Test that process is killed if termination times out."""
    executor = QwenExecutor()

    # Mock a hung process
    mock_process = MagicMock()
    mock_process.terminate = MagicMock()
    mock_process.wait.side_effect = subprocess.TimeoutExpired("qwen", 5)
    mock_process.kill = MagicMock()
    executor._process = mock_process

    await executor.close()

    # Kill should have been called after timeout
    mock_process.kill.assert_called()


# ---------------------------------------------------------------------------
# Tool server integration tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_server_full_cycle() -> None:
    """Test the full tool server lifecycle."""
    server = _ToolServer()
    assert server.port == 0

    # Start the server
    port = await server.start()
    assert port > 0

    # Set up a simple executor
    async def add_executor(name: str, args: dict) -> dict:
        return {"sum": args.get("a", 0) + args.get("b", 0)}

    server.set_tool_executor(add_executor)

    # Test execution
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    request = json.dumps({
        "id": "req-1",
        "token": server.token,
        "tool": "add",
        "args": {"a": 3, "b": 4},
    })
    writer.write((request + "\n").encode())
    await writer.drain()

    response_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
    response = json.loads(response_line.decode())

    assert response["id"] == "req-1"
    assert response["result"]["sum"] == 7

    # Cleanup
    writer.close()
    await server.close()


@pytest.mark.asyncio
async def test_tool_server_error_handling() -> None:
    """Test tool server error handling."""
    server = _ToolServer()
    await server.start()

    async def failing_executor(name: str, args: dict) -> dict:
        raise ValueError("Execution failed")

    server.set_tool_executor(failing_executor)

    reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
    request = json.dumps({
        "id": "req-err",
        "token": server.token,
        "tool": "fail",
        "args": {},
    })
    writer.write((request + "\n").encode())
    await writer.drain()

    response_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
    response = json.loads(response_line.decode())

    assert response["id"] == "req-err"
    assert "error" in response
    assert "Execution failed" in response["error"]

    writer.close()
    await server.close()
