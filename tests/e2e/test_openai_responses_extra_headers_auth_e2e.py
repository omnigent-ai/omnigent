"""E2E: OpenAI Responses requests must carry connection-supplied auth headers.

A caller that authenticates an OpenAI-compatible endpoint solely through
``connection_params["extra_headers"]`` (the MAS Barnacle proxy route: a ``host``
header plus s2s headers instead of a bearer ``api_key``) needs those headers on
``/v1/responses`` as well as ``/v1/chat/completions``; otherwise the endpoint
rejects the Responses request with 401 and the caller's turn fails.

Drives the real ``omnigent.llms`` client, routing, adapter, and SSE parsing over
real HTTP against a loopback provider; needs no server, credentials, or network.
"""

from __future__ import annotations

import http.server
import json
import threading
from collections.abc import Iterator

import pytest

from omnigent.llms import Client
from omnigent.llms.adapters.openai import OpenAICompatibleAdapter
from omnigent.llms.types import Response, ResponseCompletedEvent

AUTH_HEADER = "Bearer mas-s2s-token"


class _FakeProvider(http.server.ThreadingHTTPServer):
    """Loopback OpenAI-compatible endpoint that accepts only ``AUTH_HEADER``.

    Any other request gets the provider's 401 body; the Authorization header
    seen per path is recorded in ``auth_seen``.
    """

    def __init__(self) -> None:
        self.auth_seen: dict[str, str | None] = {}
        super().__init__(("127.0.0.1", 0), _FakeProviderHandler)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/v1"


class _FakeProviderHandler(http.server.BaseHTTPRequestHandler):
    server: _FakeProvider

    def _send(self, status: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        auth = self.headers.get("Authorization")
        self.server.auth_seen[self.path] = auth
        if auth != AUTH_HEADER:
            body = json.dumps(
                {
                    "error": {
                        "message": "Missing bearer authentication in header. "
                        "Expected 'Authorization: Bearer <token>' in header",
                        "type": "invalid_request_error",
                        "param": None,
                        "code": None,
                    }
                }
            ).encode()
            self._send(401, body)
            return
        if self.path.endswith("/chat/completions"):
            self._send(200, _chat_completion_body())
            return
        if self.path.endswith("/responses"):
            if payload.get("stream"):
                self._send(200, _responses_sse_body(), "text/event-stream")
            else:
                self._send(200, json.dumps(_completed_response()).encode())
            return
        self._send(404, b"{}")

    def log_message(self, *args: object) -> None:  # keep pytest output clean
        pass


def _chat_completion_body() -> bytes:
    return json.dumps(
        {
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "model": "gpt-5",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "hello from chat"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
    ).encode()


def _completed_response() -> dict[str, object]:
    return {
        "id": "resp-1",
        "object": "response",
        "status": "completed",
        "model": "gpt-5",
        "output": [
            {
                "id": "msg-1",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "hello from responses"}],
            }
        ],
        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    }


def _responses_sse_body() -> bytes:
    """Minimal Responses-API SSE stream: one text delta, then completed."""
    completed = _completed_response()
    delta = {"type": "response.output_text.delta", "item_id": "msg-1", "delta": "hello"}
    return (
        f"event: response.output_text.delta\ndata: {json.dumps(delta)}\n\n"
        "event: response.completed\n"
        f"data: {json.dumps({'type': 'response.completed', 'response': completed})}\n\n"
    ).encode()


@pytest.fixture()
def provider() -> Iterator[_FakeProvider]:
    server = _FakeProvider()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _connection(provider: _FakeProvider) -> dict[str, object]:
    """The MAS-style connection: auth threaded via extra_headers, no api_key."""
    return {
        "base_url": provider.base_url,
        "extra_headers": {"Authorization": AUTH_HEADER},
    }


_INPUT = [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}]


async def _assert_chat_completions_authenticates(provider: _FakeProvider) -> None:
    """Control: the same connection authenticates on the chat path."""
    adapter = OpenAICompatibleAdapter()
    result = await adapter.chat_completions(
        [{"role": "user", "content": "hi"}],
        "gpt-5",
        None,
        False,
        {},
        connection_params=_connection(provider),  # type: ignore[arg-type]
    )
    assert isinstance(result, dict)
    assert result["choices"][0]["message"]["content"] == "hello from chat"
    assert provider.auth_seen["/v1/chat/completions"] == AUTH_HEADER


async def test_responses_stream_carries_extra_headers_auth(
    provider: _FakeProvider,
) -> None:
    """Streaming /v1/responses (the ticket's ``_stream_responses`` site)."""
    await _assert_chat_completions_authenticates(provider)

    client = Client()
    stream = await client.responses.create(
        model="openai/gpt-5",
        input=_INPUT,
        stream=True,
        connection_params=_connection(provider),  # type: ignore[arg-type]
    )
    assert not isinstance(stream, Response)
    # Before the fix: iteration raises httpx.HTTPStatusError (401
    # Unauthorized) because the request went out with no Authorization
    # header, and omnigent logs "OpenAI Responses API 401: ...".
    events = [event async for event in stream]

    assert provider.auth_seen["/v1/responses"] == AUTH_HEADER, (
        "the /v1/responses request must carry the connection's auth header; "
        f"the endpoint saw {provider.auth_seen['/v1/responses']!r}"
    )
    assert any(isinstance(event, ResponseCompletedEvent) for event in events)


async def test_responses_non_streaming_carries_extra_headers_auth(
    provider: _FakeProvider,
) -> None:
    """Non-streaming /v1/responses shares the same header-building defect."""
    await _assert_chat_completions_authenticates(provider)

    client = Client()
    # Before the fix: raises httpx.HTTPStatusError (401 Unauthorized).
    response = await client.responses.create(
        model="openai/gpt-5",
        input=_INPUT,
        stream=False,
        connection_params=_connection(provider),  # type: ignore[arg-type]
    )

    assert provider.auth_seen["/v1/responses"] == AUTH_HEADER, (
        "the /v1/responses request must carry the connection's auth header; "
        f"the endpoint saw {provider.auth_seen['/v1/responses']!r}"
    )
    assert isinstance(response, Response)
    assert response.output, "the parsed Response must carry the assistant output"
