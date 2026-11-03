"""The fake Messages API supports native model validation and streamed turns."""

from __future__ import annotations

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


async def test_message_requests_record_each_requests_beta_flags() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_llm_server.app), base_url="http://mock"
    ) as client:
        (
            await client.post(
                "/mock/configure", json={"responses": [{"text": "a"}, {"text": "b"}]}
            )
        ).raise_for_status()
        long_context = {
            "model": "model-long",
            "max_tokens": 8,
            "messages": [{"role": "user", "content": "one"}],
        }
        plain = {
            "model": "model-plain",
            "max_tokens": 8,
            "messages": [{"role": "user", "content": "two"}],
        }
        headers = {"anthropic-beta": "context-1m-2025-08-07, interleaved-thinking-2025-05-14"}
        assert (
            await client.post("/v1/messages", json=long_context, headers=headers)
        ).status_code == 200
        assert (await client.post("/v1/messages", json=plain)).status_code == 200

        filtered = (
            await client.get("/mock/message_requests", params={"key": "model-long"})
        ).json()
        assert filtered["requests"] == [
            {
                "body": long_context,
                "betas": ["context-1m-2025-08-07", "interleaved-thinking-2025-05-14"],
            }
        ]
        everything = (await client.get("/mock/message_requests")).json()["requests"]
        assert [entry["betas"] for entry in everything] == [
            ["context-1m-2025-08-07", "interleaved-thinking-2025-05-14"],
            [],
        ]

        (await client.post("/mock/reset")).raise_for_status()
        assert (await client.get("/mock/message_requests")).json() == {"requests": []}
