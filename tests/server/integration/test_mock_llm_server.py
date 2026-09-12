import json
import time
from collections.abc import Iterator

import pytest
from starlette.requests import Request

from tests.server.integration.mock_llm_server import (
    MockState,
    QueuedResponse,
    _state,
    create_message,
    create_response,
    sse_text_response,
    truncate_sse,
)


def test_user_input_text_accepts_responses_string_input() -> None:
    assert MockState._user_input_text({"input": "route-native-codex"}) == ("route-native-codex")


def test_user_input_text_walks_nested_user_content() -> None:
    request = {
        "messages": [
            {"role": "system", "content": {"text": "ignore-system"}},
            {
                "role": "user",
                "content": {
                    "type": "message",
                    "content": [{"type": "text", "text": "route-native-claude"}],
                },
            },
        ]
    }

    assert MockState._user_input_text(request) == "route-native-claude"


def test_content_routing_prefers_latest_equal_length_marker() -> None:
    state = MockState()
    first = state.get_queue("turn-one")
    first.match = "usr-1-aaaaaaaa"
    second = state.get_queue("turn-two")
    second.match = "usr-2-bbbbbbbb"
    request = {
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "first usr-1-aaaaaaaa"},
                    {"type": "input_text", "text": "then usr-2-bbbbbbbb"},
                ],
            }
        ]
    }

    assert state.resolve_queue_for_request(request) is second


def _count_events(body: str) -> int:
    return len([seg for seg in body.split("\n\n") if seg])


def test_truncate_sse_keeps_prefix_and_drops_completion() -> None:
    full = sse_text_response("hello world")
    total = _count_events(full)
    assert "response.completed" in full
    assert total > 2

    truncated = truncate_sse(full, 2)
    assert _count_events(truncated) == 2
    # The dropped tail includes the terminal completion event, so a client
    # reading the truncated stream never sees the turn complete.
    assert "response.completed" not in truncated
    # Kept events are byte-identical prefixes, still ``\n\n``-terminated.
    assert full.startswith(truncated)
    assert truncated.endswith("\n\n")


def test_truncate_sse_zero_yields_empty_body() -> None:
    full = sse_text_response("hello world")
    assert truncate_sse(full, 0) == ""


def test_truncate_sse_beyond_length_is_a_noop() -> None:
    full = sse_text_response("hello world")
    assert truncate_sse(full, _count_events(full) + 5) == full


@pytest.fixture()
def clean_mock_state() -> Iterator[None]:
    """Isolate tests that drive the module-global queue state."""
    _state.reset()
    yield
    _state.reset()


def _post_request(path: str, payload: dict) -> Request:
    body = json.dumps(payload).encode()

    async def receive() -> dict:
        return {"type": "http.request", "body": body, "more_body": False}

    return Request({"type": "http", "method": "POST", "path": path, "headers": []}, receive)


async def test_chunk_delay_paces_responses_sse_event_by_event(clean_mock_state: None) -> None:
    # A scroll/layout-while-streaming bug is only drivable when the mock
    # delivers deltas incrementally; a single-chunk body renders instantly.
    chunk_delay = 0.02
    _state.get_queue("default").responses.append(
        QueuedResponse(
            text="pace the stream so the page renders deltas", stream=True, chunk_delay=chunk_delay
        )
    )

    response = await create_response(
        _post_request("/v1/responses", {"model": "gpt-4o-mini", "stream": True, "input": "hi"})
    )

    started = time.monotonic()
    chunks = [chunk async for chunk in response.body_iterator]
    elapsed = time.monotonic() - started

    assert len(chunks) > 1, "paced stream must deliver events one at a time, not one batch"
    for chunk in chunks:
        assert _count_events(chunk) == 1
        assert chunk.endswith("\n\n")
    assert "response.completed" in "".join(chunks)
    # Lower bound only (robust under load): a sleep separates consecutive events.
    assert elapsed >= chunk_delay * (len(chunks) - 1)


async def test_responses_without_chunk_delay_keeps_single_chunk_body(
    clean_mock_state: None,
) -> None:
    _state.get_queue("default").responses.append(QueuedResponse(text="hello world", stream=True))

    response = await create_response(
        _post_request("/v1/responses", {"model": "gpt-4o-mini", "stream": True, "input": "hi"})
    )

    chunks = [chunk async for chunk in response.body_iterator]
    assert len(chunks) == 1


async def test_chunk_delay_paces_messages_sse_event_by_event(clean_mock_state: None) -> None:
    _state.get_queue("default").responses.append(
        QueuedResponse(text="pace the anthropic stream too", chunk_delay=0.01)
    )

    response = await create_message(
        _post_request(
            "/v1/messages",
            {
                "model": "claude-mock",
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    )

    chunks = [chunk async for chunk in response.body_iterator]
    assert len(chunks) > 1
    for chunk in chunks:
        assert _count_events(chunk) == 1
