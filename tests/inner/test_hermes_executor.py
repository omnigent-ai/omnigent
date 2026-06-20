"""
Unit tests for :class:`HermesExecutor` and its helper functions.

Tests the executor's parsing, session management, and argument
building without invoking the real Hermes CLI.  Subprocess-level
integration tests belong in the e2e suite.

HermesExecutor's ``run_turn`` method is tested with a patched
``asyncio.create_subprocess_exec`` to verify event emission
patterns and error handling.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from omnigent.inner.executor import (
    ExecutorConfig,
    ExecutorError,
    TextChunk,
    TurnComplete,
)
from omnigent.inner.hermes_executor import (
    HermesExecutor,
    _build_hermes_args,
    _extract_last_user_message,
    _parse_session_id,
    _strip_hermes_metadata,
)


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


class TestUtils:
    """Tests for standalone helper functions in hermes_executor."""

    def test_strip_hermes_metadata_removes_session_id_line(self) -> None:
        output = "session_id: 20260620_123456_abc123\nHello, world!"
        assert _strip_hermes_metadata(output) == "Hello, world!"

    def test_strip_hermes_metadata_removes_resume_notice(self) -> None:
        output = "↻ Resumed session 20260620_123456_abc123 (1 message)\n\nsession_id: 20260620_123456_abc123\nHello again!"
        assert _strip_hermes_metadata(output) == "Hello again!"

    def test_strip_hermes_metadata_removes_warnings(self) -> None:
        output = "Warning: Unknown toolsets: messaging\nsession_id: abc\nHello!"
        assert _strip_hermes_metadata(output) == "Hello!"

    def test_strip_hermes_metadata_preserves_empty_response(self) -> None:
        output = "session_id: 20260620_123456_abc123\n"
        assert _strip_hermes_metadata(output) == ""

    def test_strip_hermes_metadata_preserves_multi_line_response(self) -> None:
        output = "session_id: 123\nLine one\nLine two\nLine three"
        assert _strip_hermes_metadata(output) == "Line one\nLine two\nLine three"

    def test_parse_session_id_found(self) -> None:
        output = "Warning: something\nsession_id: 20260620_abc123_def456\nResponse text"
        assert _parse_session_id(output) == "20260620_abc123_def456"

    def test_parse_session_id_not_found(self) -> None:
        output = "No session ID here"
        assert _parse_session_id(output) is None

    def test_parse_session_id_empty_output(self) -> None:
        assert _parse_session_id("") is None

    def test_extract_last_user_message_simple(self) -> None:
        messages = [
            {"role": "user", "content": "First message"},
            {"role": "assistant", "content": "First response"},
            {"role": "user", "content": "Second message"},
        ]
        assert _extract_last_user_message(messages) == "Second message"

    def test_extract_last_user_message_content_blocks(self) -> None:
        messages = [
            {"role": "user", "content": [{"type": "input_text", "text": "Hello"}]},
        ]
        assert _extract_last_user_message(messages) == "Hello"

    def test_extract_last_user_message_empty(self) -> None:
        assert _extract_last_user_message([]) == ""

    def test_extract_last_user_message_no_user(self) -> None:
        messages = [{"role": "assistant", "content": "Hello"}]
        assert _extract_last_user_message(messages) == ""

    def test_build_hermes_args_basic(self) -> None:
        args = _build_hermes_args("/usr/bin/hermes", "Hello")
        assert args == [
            "/usr/bin/hermes",
            "chat",
            "-q",
            "Hello",
            "-Q",
            "--source",
            "tool",
        ]

    def test_build_hermes_args_with_model(self) -> None:
        args = _build_hermes_args("hermes", "Hi", model="deepseek/deepseek-chat")
        assert "-m" in args
        assert "deepseek/deepseek-chat" in args

    def test_build_hermes_args_with_session(self) -> None:
        args = _build_hermes_args(
            "hermes", "Hi", session_id="20260620_abc123"
        )
        assert "--resume" in args
        idx = args.index("--resume")
        assert args[idx + 1] == "20260620_abc123"


# ---------------------------------------------------------------------------
# HermesExecutor unit tests
# ---------------------------------------------------------------------------


@pytest.fixture
def executor() -> HermesExecutor:
    """Return a HermesExecutor with a dummy path for testing."""
    return HermesExecutor(
        hermes_path="/usr/bin/hermes-fake",
        cwd="/tmp",
    )


@pytest.mark.asyncio
async def test_run_turn_returns_text_chunk_and_turn_complete(
    executor: HermesExecutor,
) -> None:
    """A successful subprocess call yields TextChunk + TurnComplete."""
    mock_process = MagicMock()
    mock_process.returncode = 0
    mock_process.communicate = AsyncMock(
        return_value=(
            b"session_id: 20260620_test_sid\nHello, world!",
            b"",
        )
    )

    with patch.object(
        asyncio,
        "create_subprocess_exec",
        new=AsyncMock(return_value=mock_process),
    ):
        events = []
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": "Hi"}],
            tools=[],
            system_prompt="",
        ):
            events.append(event)

    assert len(events) >= 2
    assert isinstance(events[-1], TurnComplete)
    assert events[-1].response == "Hello, world!"
    # At least one TextChunk should be present
    text_chunks = [e for e in events if isinstance(e, TextChunk)]
    assert len(text_chunks) >= 1
    assert text_chunks[0].text == "Hello, world!"


@pytest.mark.asyncio
async def test_run_turn_empty_message_yields_none(
    executor: HermesExecutor,
) -> None:
    """No user message should short-circuit with TurnComplete(response=None)."""
    events = []
    async for event in executor.run_turn(
        messages=[{"role": "assistant", "content": "Hello"}],
        tools=[],
        system_prompt="",
    ):
        events.append(event)

    assert len(events) == 1
    assert isinstance(events[0], TurnComplete)
    assert events[0].response is None


@pytest.mark.asyncio
async def test_run_turn_subprocess_timeout(
    executor: HermesExecutor,
) -> None:
    """A timed-out subprocess yields ExecutorError."""
    mock_process = MagicMock()
    mock_process.communicate = AsyncMock(side_effect=asyncio.TimeoutError)

    with patch.object(
        asyncio,
        "create_subprocess_exec",
        new=AsyncMock(return_value=mock_process),
    ):
        events = []
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": "Hi"}],
            tools=[],
            system_prompt="",
        ):
            events.append(event)

    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    assert "timed out" in events[0].message


@pytest.mark.asyncio
async def test_run_turn_subprocess_error(
    executor: HermesExecutor,
) -> None:
    """A non-zero exit code yields ExecutorError."""
    mock_process = MagicMock()
    mock_process.returncode = 1
    mock_process.communicate = AsyncMock(
        return_value=(b"", b"Error: something went wrong")
    )

    with patch.object(
        asyncio,
        "create_subprocess_exec",
        new=AsyncMock(return_value=mock_process),
    ):
        events = []
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": "Hi"}],
            tools=[],
            system_prompt="",
        ):
            events.append(event)

    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    assert "Something went wrong" in events[0].message or "error" in events[0].message.lower()


@pytest.mark.asyncio
async def test_run_turn_file_not_found(
    executor: HermesExecutor,
) -> None:
    """A missing Hermes binary yields ExecutorError with install hint."""
    with patch.object(
        asyncio,
        "create_subprocess_exec",
        new=AsyncMock(side_effect=FileNotFoundError),
    ):
        events = []
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": "Hi"}],
            tools=[],
            system_prompt="",
        ):
            events.append(event)

    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    assert "Hermes CLI not found" in events[0].message
    assert "install" in events[0].message.lower()


@pytest.mark.asyncio
async def test_run_turn_stores_session_id(
    executor: HermesExecutor,
) -> None:
    """The executor captures session_id from the first turn for resume."""
    mock_process = MagicMock()
    mock_process.returncode = 0
    mock_process.communicate = AsyncMock(
        return_value=(
            b"session_id: 20260620_captured_sid\nResponse text",
            b"",
        )
    )

    with patch.object(
        asyncio,
        "create_subprocess_exec",
        new=AsyncMock(return_value=mock_process),
    ):
        events = []
        async for event in executor.run_turn(
            messages=[
                {"role": "user", "content": "Hi", "session_id": "test-session-key"}
            ],
            tools=[],
            system_prompt="",
        ):
            events.append(event)

    # Verify the session ID was stored
    assert executor._hermes_session_id("test-session-key") == "20260620_captured_sid"


@pytest.mark.asyncio
async def test_run_turn_resumes_existing_session(
    executor: HermesExecutor,
) -> None:
    """When a session_id is already stored, subsequent turns use --resume."""
    # Pre-populate the session map
    executor._session_map["test-session-key"] = "20260620_existing_sid"

    mock_process = MagicMock()
    mock_process.returncode = 0
    mock_process.communicate = AsyncMock(
        return_value=(b"session_id: 20260620_existing_sid\nFollow-up response", b"")
    )

    with patch.object(
        asyncio,
        "create_subprocess_exec",
        new=AsyncMock(return_value=mock_process),
    ) as mock_create:
        events = []
        async for event in executor.run_turn(
            messages=[
                {"role": "user", "content": "Follow up", "session_id": "test-session-key"}
            ],
            tools=[],
            system_prompt="",
        ):
            events.append(event)

        # Verify --resume was used in the subprocess args
        call_args, _ = mock_create.call_args
        assert "--resume" in call_args
        resume_idx = list(call_args).index("--resume")
        assert list(call_args)[resume_idx + 1] == "20260620_existing_sid"


@pytest.mark.asyncio
async def test_run_turn_passes_model_from_config(
    executor: HermesExecutor,
) -> None:
    """Model from ExecutorConfig.extra or config.model is threaded through."""
    mock_process = MagicMock()
    mock_process.returncode = 0
    mock_process.communicate = AsyncMock(
        return_value=(b"session_id: test\nResponse", b"")
    )
    config = ExecutorConfig(model="deepseek/deepseek-chat")

    with patch.object(
        asyncio,
        "create_subprocess_exec",
        new=AsyncMock(return_value=mock_process),
    ) as mock_create:
        events = []
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": "Hi"}],
            tools=[],
            system_prompt="",
            config=config,
        ):
            events.append(event)

        call_args, _ = mock_create.call_args
        assert "-m" in call_args
        idx = list(call_args).index("-m")
        assert list(call_args)[idx + 1] == "deepseek/deepseek-chat"


def test_handles_tools_internally(executor: HermesExecutor) -> None:
    """HermesExecutor reports it handles its own tool calls."""
    assert executor.handles_tools_internally() is True
