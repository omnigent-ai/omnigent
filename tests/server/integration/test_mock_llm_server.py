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
