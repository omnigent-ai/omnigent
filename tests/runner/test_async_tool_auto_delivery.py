"""
Unit tests for async-tool auto-delivery (SESSION_REARCHITECTURE Step 7).

Verifies that ``_auto_deliver_async_completion`` and
``_schedule_auto_delivery`` in :mod:`omnigent.runner.tool_dispatch`
POST the formatted completion as a ``[System: task ...]`` user
message to ``/v1/sessions/{id}/events``, with retry on transient
failures and graceful degradation when the server is unreachable.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from omnigent.runner.tool_dispatch import (
    _auto_deliver_async_completion,
    _format_async_task_item,
    _schedule_auto_delivery,
)


def _completed_payload(handle_id: str = "handle_abc", tool: str = "my_tool") -> dict:
    return {
        "handle_id": handle_id,
        "tool_name": tool,
        "status": "completed",
        "output": "RESULT_MARKER_42",
    }


def _mock_server_client(status_code: int = 200) -> AsyncMock:
    mock = AsyncMock(spec=httpx.AsyncClient)
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error",
            request=httpx.Request("POST", "http://x"),
            response=httpx.Response(status_code),
        )
    mock.post.return_value = resp
    return mock


# ─── _auto_deliver_async_completion ────────────────────────────


@pytest.mark.asyncio
async def test_auto_deliver_posts_formatted_message() -> None:
    """The auto-delivery POSTs the formatted task item as a user message."""
    payload = _completed_payload()
    client = _mock_server_client()
    conv_id = "conv_test123"

    await _auto_deliver_async_completion(
        payload,
        server_client=client,
        conversation_id=conv_id,
    )

    client.post.assert_called_once()
    call_args = client.post.call_args
    assert call_args[0][0] == f"/v1/sessions/{conv_id}/events"

    body = call_args[1]["json"]
    assert body["type"] == "message"
    assert body["data"]["role"] == "user"
    text = body["data"]["content"][0]["text"]
    expected = _format_async_task_item(payload)
    assert text == expected
    assert "RESULT_MARKER_42" in text
    assert text.startswith("[System: task ")


@pytest.mark.asyncio
async def test_auto_deliver_retries_on_5xx() -> None:
    """Transient 5xx failures trigger retries with exponential backoff."""
    payload = _completed_payload()
    client = _mock_server_client(503)

    with patch("omnigent.runner.tool_dispatch._sleep", new_callable=AsyncMock):
        await _auto_deliver_async_completion(
            payload,
            server_client=client,
            conversation_id="conv_retry",
        )

    # Should have retried up to MAX_ATTEMPTS (3)
    assert client.post.call_count == 3


@pytest.mark.asyncio
async def test_auto_deliver_no_retry_on_4xx() -> None:
    """Permanent 4xx failures stop immediately without retrying."""
    payload = _completed_payload()
    client = _mock_server_client(400)

    await _auto_deliver_async_completion(
        payload,
        server_client=client,
        conversation_id="conv_no_retry",
    )

    assert client.post.call_count == 1


@pytest.mark.asyncio
async def test_auto_deliver_handles_timeout() -> None:
    """asyncio.TimeoutError is treated as retryable."""
    payload = _completed_payload()
    client = AsyncMock(spec=httpx.AsyncClient)
    client.post.side_effect = asyncio.TimeoutError()

    with patch("omnigent.runner.tool_dispatch._sleep", new_callable=AsyncMock):
        await _auto_deliver_async_completion(
            payload,
            server_client=client,
            conversation_id="conv_timeout",
        )

    assert client.post.call_count == 3


@pytest.mark.asyncio
async def test_auto_deliver_formats_failed_payload() -> None:
    """Failed tool results are auto-delivered with the error marker."""
    payload = {
        "handle_id": "handle_fail",
        "tool_name": "boom_tool",
        "status": "failed",
        "output": "BOOM_ERROR_MARKER",
    }
    client = _mock_server_client()

    await _auto_deliver_async_completion(
        payload,
        server_client=client,
        conversation_id="conv_fail",
    )

    text = client.post.call_args[1]["json"]["data"]["content"][0]["text"]
    assert "failed" in text
    assert "BOOM_ERROR_MARKER" in text


@pytest.mark.asyncio
async def test_auto_deliver_formats_cancelled_payload() -> None:
    """Cancelled tasks are auto-delivered with cancelled status."""
    payload = {
        "handle_id": "handle_cancel",
        "tool_name": "slow_tool",
        "status": "cancelled",
        "output": "",
    }
    client = _mock_server_client()

    await _auto_deliver_async_completion(
        payload,
        server_client=client,
        conversation_id="conv_cancel",
    )

    text = client.post.call_args[1]["json"]["data"]["content"][0]["text"]
    assert "cancelled" in text


# ─── _schedule_auto_delivery ────────────────────────────────


def test_schedule_noop_without_server_client() -> None:
    """No task is created when server_client is None."""
    payload = _completed_payload()
    _schedule_auto_delivery(
        payload,
        server_client=None,
        conversation_id="conv_x",
    )
    # No error, no task created — silent no-op.


def test_schedule_noop_without_conversation_id() -> None:
    """No task is created when conversation_id is None."""
    payload = _completed_payload()
    _schedule_auto_delivery(
        payload,
        server_client=_mock_server_client(),
        conversation_id=None,
    )


@pytest.mark.asyncio
async def test_schedule_creates_background_task() -> None:
    """_schedule_auto_delivery creates an asyncio task that POSTs."""
    payload = _completed_payload()
    client = _mock_server_client()

    _schedule_auto_delivery(
        payload,
        server_client=client,
        conversation_id="conv_schedule",
    )

    # Let the event loop process the task.
    await asyncio.sleep(0)

    assert client.post.called
    text = client.post.call_args[1]["json"]["data"]["content"][0]["text"]
    assert "RESULT_MARKER_42" in text
