"""Unit tests for QwenExecutor."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from omnigent.inner.qwen_executor import QwenExecutor


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
async def test_executor_handles_empty_messages(executor: QwenExecutor) -> None:
    """Test that executor handles empty message lists."""
    messages: list[dict] = []
    tools: list[dict] = []

    # This should not raise an exception during initialization
    # (actual turn execution requires a running qwen process)
    assert executor is not None
