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
        response = client.post(
            "/v1/responses",
            json={
                "model": "gpt-5.4",
                "stream": True,
                "tools": [
                    {"type": "function", "name": call["name"]}
                    for call in queued_response.get("tool_calls", [])
                ],
            },
        )
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


@pytest.mark.parametrize(
    "endpoint,tool",
    [
        ("/v1/messages", {"name": "Agent", "input_schema": {"type": "object"}}),
        ("/v1/responses", {"type": "function", "name": "Agent", "parameters": {"type": "object"}}),
        (
            "/v1/chat/completions",
            {"type": "function", "function": {"name": "Agent", "parameters": {"type": "object"}}},
        ),
    ],
)
def test_independent_content_queues_and_title_isolation(monkeypatch, endpoint, tool):
    monkeypatch.setattr(mock_llm_server, "_state", MockState())
    with TestClient(mock_llm_server.app) as client:
        keys = []
        for marker in ("parent-prompt", "worker-prompt", "followup-prompt"):
            response = client.post(
                "/mock/configure",
                json={
                    "match": marker,
                    "required_tools": ["Agent"],
                    "responses": [
                        {"tool_calls": [{"call_id": marker, "name": "Agent", "arguments": "{}"}]}
                    ],
                },
            )
            assert response.status_code == 200
            keys.append(response.json()["key"])
        assert len(set(keys)) == 3
        for marker, key in zip(
            ("parent-prompt", "worker-prompt", "followup-prompt"), keys, strict=True
        ):
            body = {"model": "any-model", "messages": [{"role": "user", "content": marker}]}
            if endpoint == "/v1/responses":
                body = {"model": "any-model", "input": marker}
            # A title request sees the same user nonce but lacks agent tools.
            assert client.post(endpoint, json=body).status_code == 200
            assert mock_llm_server._state.queues[key].index == 0
            response = client.post(endpoint, json={**body, "tools": [tool]})
            assert response.status_code == 200
            assert marker in response.text
        assert all(mock_llm_server._state.queues[key].index == 1 for key in keys)


@pytest.mark.parametrize("key", [None, "default", "custom-model"])
def test_explicit_keys_and_unrouted_default_still_replace(monkeypatch, key):
    monkeypatch.setattr(mock_llm_server, "_state", MockState())
    with TestClient(mock_llm_server.app) as client:
        for text in ("old", "new"):
            body = {"responses": [{"text": text}]}
            if key is not None:
                body.update(key=key, match="nonce")
            assert client.post("/mock/configure", json=body).status_code == 200
        response = client.post("/v1/responses", json={"input": "nonce"})
        assert response.json()["output"][0]["content"][0]["text"] == "new"
        assert len(mock_llm_server._state.queues) == 1


def test_tool_guard_cannot_be_bypassed_by_model_or_default(monkeypatch):
    monkeypatch.setattr(mock_llm_server, "_state", MockState())
    with TestClient(mock_llm_server.app) as client:
        for key in ("default", "native-model"):
            client.post(
                "/mock/configure",
                json={
                    "key": key,
                    "required_tools": ["Task"],
                    "responses": [{"text": "protected"}],
                },
            )
        for model in ("unknown", "native-model"):
            response = client.post("/v1/responses", json={"model": model, "input": "title"})
            assert "protected" not in response.text
        assert all(queue.index == 0 for queue in mock_llm_server._state.queues.values())


@pytest.mark.parametrize("key,model", [("default", "unknown"), ("native-model", "native-model")])
def test_tool_guard_protects_configured_fallback(monkeypatch, key, model):
    monkeypatch.setattr(mock_llm_server, "_state", MockState())
    with TestClient(mock_llm_server.app) as client:
        client.post(
            "/mock/configure",
            json={"key": key, "required_tools": ["Task"], "responses": [{"text": "scripted"}]},
        ).raise_for_status()
        client.post(
            "/mock/set_fallback", json={"key": key, "text": "protected fallback"}
        ).raise_for_status()
        request = {"model": model, "input": "title"}
        for tools, expected, remaining in [
            ([], "Mock LLM response", 1),
            ([{"name": "Task"}], "scripted", 0),
            ([], "Mock LLM response", 0),
            ([{"name": "Task"}], "protected fallback", 0),
        ]:
            response = client.post("/v1/responses", json={**request, "tools": tools})
            response.raise_for_status()
            assert response.json()["output"][0]["content"][0]["text"] == expected
            assert (
                len(mock_llm_server._state.queues[key].responses)
                - mock_llm_server._state.queues[key].index
                == remaining
            )


@pytest.mark.parametrize(
    "invalid", [{"required_tools": "Task"}, {"required_tools": [1]}, {"match": ""}]
)
def test_invalid_routing_is_rejected_before_replacing_queue(monkeypatch, invalid):
    monkeypatch.setattr(mock_llm_server, "_state", MockState())
    with TestClient(mock_llm_server.app) as client:
        client.post("/mock/configure", json={"responses": [{"text": "kept"}]})
        assert client.post("/mock/configure", json=invalid).status_code == 400
        assert mock_llm_server._state.queues["default"].index == 0


def test_tool_results_cannot_steal_parent_content_routing():
    state = MockState()
    parent = state.get_queue("parent")
    parent.match = "parent-token"
    child = state.get_queue("child")
    child.match = "longer-child-token"
    request = {
        "messages": [
            {"role": "user", "content": "parent-token"},
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "call", "content": "longer-child-token"}
                ],
            },
        ]
    }
    assert state.resolve_queue_for_request(request) is parent


def test_same_implicit_selector_replaces_and_different_purpose_coexists(monkeypatch):
    monkeypatch.setattr(mock_llm_server, "_state", MockState())
    with TestClient(mock_llm_server.app) as client:
        keys = []
        for text in ("old", "new"):
            keys.append(
                client.post(
                    "/mock/configure",
                    json={
                        "match": "nonce",
                        "required_tools": ["Task"],
                        "responses": [{"text": text}],
                    },
                ).json()["key"]
            )
        other = client.post(
            "/mock/configure",
            json={"match": "nonce", "required_tools": ["Read"], "responses": [{"text": "worker"}]},
        ).json()["key"]
        assert keys[0] == keys[1] != other
        response = client.post(
            "/v1/responses", json={"input": "nonce", "tools": [{"name": "Task"}]}
        )
        assert response.json()["output"][0]["content"][0]["text"] == "new"


@pytest.mark.parametrize("routing", [{}, {"key": "native-model"}, {"match": "user-nonce"}])
def test_unspecified_guard_preserves_next_tool_call_for_capable_request(monkeypatch, routing):
    monkeypatch.setattr(mock_llm_server, "_state", MockState())
    with TestClient(mock_llm_server.app) as client:
        key = client.post(
            "/mock/configure",
            json={
                **routing,
                "responses": [
                    {
                        "tool_calls": [
                            {"call_id": "skill-call", "name": "Skill", "arguments": "{}"}
                        ]
                    },
                    {"text": "after skill"},
                ],
            },
        ).json()["key"]
        body = {"model": "native-model", "input": "user-nonce"}
        for no_tools in ({}, {"tools": []}):
            response = client.post("/v1/responses", json={**body, **no_tools})
            assert "skill-call" not in response.text
            assert mock_llm_server._state.queues[key].index == 0
        response = client.post("/v1/responses", json={**body, "tools": [{"name": "Skill"}]})
        assert "skill-call" in response.text
        assert mock_llm_server._state.queues[key].index == 1
        # Inference applies to the next tool response, not later text entries.
        response = client.post("/v1/responses", json=body)
        assert response.json()["output"][0]["content"][0]["text"] == "after skill"


@pytest.mark.parametrize("required_tools", [[], ["Read"]])
def test_explicit_guard_allows_intentionally_unadvertised_tool(monkeypatch, required_tools):
    monkeypatch.setattr(mock_llm_server, "_state", MockState())
    with TestClient(mock_llm_server.app) as client:
        client.post(
            "/mock/configure",
            json={
                "required_tools": required_tools,
                "responses": [
                    {"tool_calls": [{"call_id": "bad-call", "name": "Unknown", "arguments": "{}"}]}
                ],
            },
        ).raise_for_status()
        response = client.post(
            "/v1/responses", json={"tools": [{"name": name} for name in required_tools]}
        )
        assert "bad-call" in response.text


def test_inferred_guard_preserves_unadvertised_calls_and_tracks_next_response():
    state = MockState()
    queue = state.get_queue("default")
    queue.responses = [
        mock_llm_server.QueuedResponse(tool_calls=[{"name": "Read"}, {"name": "Write"}]),
        mock_llm_server.QueuedResponse(tool_calls=[{"name": "Skill"}]),
    ]
    assert state.resolve_queue_for_request({"tools": []}) is not queue
    assert queue.index == 0
    # Deliberately unavailable calls still reach tests of tool-not-found behavior.
    first = state.resolve_queue_for_request({"tools": [{"name": "Read"}]})
    assert first is queue
    first.next()
    assert state.resolve_queue_for_request({"tools": []}) is not queue
    assert state.resolve_queue_for_request({"tools": [{"name": "Skill"}]}) is queue


def test_reset_and_reconfigure_restore_inference(monkeypatch):
    monkeypatch.setattr(mock_llm_server, "_state", MockState())
    with TestClient(mock_llm_server.app) as client:
        script = {"tool_calls": [{"call_id": "skill-call", "name": "Skill", "arguments": "{}"}]}
        client.post("/mock/configure", json={"required_tools": [], "responses": [script]})
        client.post("/mock/set_fallback", json={"text": "persistent fallback"})
        client.post("/mock/reset")
        client.post("/mock/configure", json={"responses": [script]})
        response = client.post("/v1/responses", json={"input": "title"})
        assert "skill-call" not in response.text
        assert mock_llm_server._state.queues["default"].index == 0


@pytest.mark.parametrize("stream", [True, False], ids=["stream", "json"])
@pytest.mark.parametrize("endpoint", ["/v1/responses", "/v1/chat/completions"])
def test_scripted_response_headers_ride_on_openai_endpoints(
    monkeypatch: pytest.MonkeyPatch, endpoint: str, stream: bool
) -> None:
    """A scripted reply's extra headers reach the client; unscripted ones carry none."""
    monkeypatch.setattr(mock_llm_server, "_state", MockState())
    with TestClient(mock_llm_server.app) as client:
        configured = client.post(
            "/mock/configure",
            json={
                "key": "mock-model",
                "responses": [
                    {"text": "priced", "response_headers": {"x-litellm-response-cost": "0.0123"}},
                    {"text": "unpriced"},
                ],
            },
        )
        assert configured.status_code == 200
        body: dict[str, Any] = {"model": "mock-model", "stream": stream}
        if endpoint == "/v1/chat/completions":
            body["messages"] = [{"role": "user", "content": "hi"}]
        else:
            body["input"] = "hi"
        priced = client.post(endpoint, json=body)
        unpriced = client.post(endpoint, json=body)
    assert priced.status_code == 200
    assert priced.headers["x-litellm-response-cost"] == "0.0123"
    assert unpriced.status_code == 200
    assert "x-litellm-response-cost" not in unpriced.headers
