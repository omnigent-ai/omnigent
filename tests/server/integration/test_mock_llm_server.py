from tests.server.integration.mock_llm_server import MockState


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
