"""The fake Messages API supports native model validation and streamed turns."""

from __future__ import annotations

import json

import httpx
import pytest

from tests.server.integration import mock_llm_server


@pytest.fixture(autouse=True)
def isolated_mock_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mock_llm_server, "_state", mock_llm_server.MockState())


@pytest.mark.parametrize("stream", [None, False])
async def test_model_validation_gets_json_message(stream: bool | None) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_llm_server.app), base_url="http://mock"
    ) as client:
        configured = await client.post(
            "/mock/configure", json={"responses": [{"text": "Synthetic reply"}]}
        )
        configured.raise_for_status()
        payload = {
            "model": "private.catalog/model-fast",
            "max_tokens": 8,
            "messages": [{"role": "user", "content": "Validate this synthetic model."}],
        }
        if stream is not None:
            payload["stream"] = stream
        response = await client.post("/v1/messages", json=payload)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/json")
        message = response.json()
        assert message["type"] == "message"
        assert message["role"] == "assistant"
        assert message["model"] == payload["model"]
        assert message["content"] == [{"type": "text", "text": "Synthetic reply"}]
        assert message["stop_reason"] == "end_turn"
        assert message["usage"]["output_tokens"] > 0
        requests = await client.get("/mock/requests")
        assert requests.json()["requests"] == [payload]


async def test_streamed_message_keeps_anthropic_sse_events() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_llm_server.app), base_url="http://mock"
    ) as client:
        response = await client.post(
            "/v1/messages",
            json={
                "model": "private.catalog/model-fast",
                "max_tokens": 8,
                "messages": [{"role": "user", "content": "Stream a synthetic greeting."}],
                "stream": True,
            },
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert "event: message_start" in response.text
        assert "event: content_block_delta" in response.text
        assert "event: message_stop" in response.text


async def test_nonstream_tool_use_has_decoded_input() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_llm_server.app), base_url="http://mock"
    ) as client:
        configured = await client.post(
            "/mock/configure",
            json={
                "responses": [
                    {"tool_calls": [{"name": "inspect", "arguments": '{"path":"synthetic.txt"}'}]}
                ]
            },
        )
        configured.raise_for_status()
        response = await client.post(
            "/v1/messages",
            json={
                "model": "synthetic-model",
                "messages": [],
                "max_tokens": 8,
                "stream": False,
                "tools": [{"name": "inspect", "input_schema": {"type": "object"}}],
            },
        )
        response.raise_for_status()
        message = response.json()
        assert message["stop_reason"] == "tool_use"
        assert message["content"][0]["name"] == "inspect"
        assert message["content"][0]["input"] == {"path": "synthetic.txt"}


@pytest.mark.parametrize("thinking", [None, "Let me plan the long report first."])
async def test_streamed_stop_reason_override_reaches_message_delta(thinking: str | None) -> None:
    """A queued ``stop_reason`` ends plain and thinking replies alike."""
    response_spec: dict[str, object] = {"text": "Here is the start —", "stop_reason": "max_tokens"}
    if thinking is not None:
        response_spec["thinking"] = thinking
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_llm_server.app), base_url="http://mock"
    ) as client:
        configured = await client.post("/mock/configure", json={"responses": [response_spec]})
        configured.raise_for_status()
        response = await client.post(
            "/v1/messages",
            json={
                "model": "synthetic-model",
                "max_tokens": 8,
                "messages": [{"role": "user", "content": "Write the full report."}],
                "stream": True,
            },
        )
        assert response.status_code == 200
        deltas = [
            json.loads(line.removeprefix("data: "))
            for line in response.text.splitlines()
            if line.startswith("data: ") and '"message_delta"' in line
        ]
        assert [delta["delta"]["stop_reason"] for delta in deltas] == ["max_tokens"]
        assert ('"type": "thinking"' in response.text) == (thinking is not None)


async def test_nonstream_stop_reason_override_reaches_message() -> None:
    """A queued ``stop_reason`` also ends a JSON (non-streaming) reply."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_llm_server.app), base_url="http://mock"
    ) as client:
        configured = await client.post(
            "/mock/configure",
            json={"responses": [{"text": "Here is the start —", "stop_reason": "max_tokens"}]},
        )
        configured.raise_for_status()
        response = await client.post(
            "/v1/messages",
            json={
                "model": "synthetic-model",
                "max_tokens": 8,
                "messages": [{"role": "user", "content": "Write the full report."}],
                "stream": False,
            },
        )
        response.raise_for_status()
        message = response.json()
        assert message["stop_reason"] == "max_tokens"
        assert message["content"] == [{"type": "text", "text": "Here is the start —"}]
