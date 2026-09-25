import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.server.integration import mock_llm_server
from tests.server.integration.mock_llm_server import (
    MockState,
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


@pytest.mark.parametrize(
    "queued_response",
    [
        pytest.param({"text": "hello world"}, id="text"),
        pytest.param({"text": "hello world", "stream": True}, id="text-deltas"),
        pytest.param(
            {"tool_calls": [{"call_id": "call-1", "name": "read", "arguments": "{}"}]},
            id="tool-call",
        ),
        pytest.param(
            {"native_items": [{"type": "web_search_call", "id": "search-1"}]},
            id="native-items",
        ),
    ],
)
@pytest.mark.parametrize(
    "usage",
    [
        None,
        {
            "input_tokens": 2_000_000,
            "output_tokens": 100,
            "input_tokens_details": {"cached_tokens": 0},
        },
    ],
    ids=["default-usage", "scripted-usage"],
)
def test_responses_stream_uses_scripted_token_usage(
    monkeypatch: pytest.MonkeyPatch,
    queued_response: dict[str, Any],
    usage: dict[str, Any] | None,
) -> None:
    monkeypatch.setattr(mock_llm_server, "_state", MockState())
    with TestClient(mock_llm_server.app) as client:
        configured = client.post(
            "/mock/configure",
            json={"key": "gpt-5.4", "responses": [{**queued_response, "usage": usage}]},
        )
        assert configured.status_code == 200
        response = client.post("/v1/responses", json={"model": "gpt-5.4", "stream": True})
    assert response.status_code == 200
    events = [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
    ]
    expected_usage = usage or {"input_tokens": 10, "output_tokens": 5}
    expected_usage = {
        **expected_usage,
        "total_tokens": expected_usage["input_tokens"] + expected_usage["output_tokens"],
    }
    for event_type in ("response.created", "response.completed"):
        event = next(event for event in events if event.get("type") == event_type)
        assert event["response"]["model"] == "mock-model"
        assert event["response"]["usage"] == expected_usage


def test_responses_json_merges_partial_token_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mock_llm_server, "_state", MockState())
    with TestClient(mock_llm_server.app) as client:
        configured = client.post(
            "/mock/configure",
            json={"responses": [{"text": "hello", "usage": {"input_tokens": 1000}}]},
        )
        assert configured.status_code == 200
        response = client.post("/v1/responses", json={"model": "gpt-5.4"})
    assert response.status_code == 200
    assert response.json()["usage"] == {
        "input_tokens": 1000,
        "output_tokens": 5,
        "total_tokens": 1005,
    }
