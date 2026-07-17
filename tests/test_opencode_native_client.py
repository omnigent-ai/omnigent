"""Tests for the OpenCode HTTP + SSE client against a fake server."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from omnigent.opencode_native_client import (
    OpenCodeClient,
    OpenCodeClientError,
    OpenCodeEvent,
    OpenCodeSession,
)

Handler = Callable[[httpx.Request], httpx.Response]


def _client(handler: Handler, **kwargs: object) -> OpenCodeClient:
    mock = httpx.AsyncClient(
        base_url="http://opencode.test",
        transport=httpx.MockTransport(handler),
    )
    return OpenCodeClient("http://opencode.test", client=mock, **kwargs)  # type: ignore[arg-type]


async def test_create_session() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/session"
        return httpx.Response(200, json={"id": "ses_1", "title": "t"})

    client = _client(handler)
    session = await client.create_session({"title": "t"})
    assert isinstance(session, OpenCodeSession)
    assert session.id == "ses_1"
    assert session.title == "t"
    await client.aclose()


async def test_get_session_404_returns_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not found"})

    client = _client(handler)
    assert await client.get_session("ses_missing") is None
    await client.aclose()


async def test_get_session_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "ses_1", "parentID": "ses_0"})

    client = _client(handler)
    session = await client.get_session("ses_1")
    assert session is not None
    assert session.parent_id == "ses_0"
    await client.aclose()


async def test_list_messages() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"info": {"id": "msg_1"}, "parts": []}])

    client = _client(handler)
    messages = await client.list_messages("ses_1")
    assert messages == [{"info": {"id": "msg_1"}, "parts": []}]
    await client.aclose()


async def test_list_models() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/model"
        return httpx.Response(200, json={"models": [{"id": "opencode-go/glm-5.2"}]})

    client = _client(handler)
    assert await client.list_models() == [{"id": "opencode-go/glm-5.2"}]
    await client.aclose()


async def test_prompt_async_posts_parts() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/session/ses_1/prompt_async"
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={})

    client = _client(handler)
    await client.prompt_async("ses_1", {"parts": [{"type": "text", "text": "hi"}]})
    assert captured["body"] == {"parts": [{"type": "text", "text": "hi"}]}
    await client.aclose()


async def test_abort_returns_bool() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/session/ses_1/abort"
        return httpx.Response(200, json=True)

    client = _client(handler)
    assert await client.abort("ses_1") is True
    await client.aclose()


async def test_fork() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/session/ses_1/fork"
        return httpx.Response(200, json={"id": "ses_2", "parentID": "ses_1"})

    client = _client(handler)
    forked = await client.fork("ses_1", {"messageID": "msg_1"})
    assert forked.id == "ses_2"
    await client.aclose()


async def test_reply_permission() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/permission/per_1/reply"
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={})

    client = _client(handler)
    assert await client.reply_permission("per_1", {"reply": "once"}) is True
    assert captured["body"] == {"reply": "once"}
    await client.aclose()


async def test_list_permissions() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"id": "per_1", "action": "bash"}])

    client = _client(handler)
    perms = await client.list_permissions()
    assert perms == [{"id": "per_1", "action": "bash"}]
    await client.aclose()


async def test_error_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    client = _client(handler)
    with pytest.raises(OpenCodeClientError):
        await client.create_session()
    await client.aclose()


async def test_auth_and_directory_headers_applied() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization", "")
        captured["dir"] = request.headers.get("x-opencode-directory", "")
        return httpx.Response(200, json=[])

    client = _client(handler, headers={"Authorization": "Basic abc"}, directory="/repo")
    await client.list_messages("ses_1")
    assert captured["auth"] == "Basic abc"
    assert captured["dir"] == "/repo"
    await client.aclose()


async def test_events_parses_sse_stream() -> None:
    sse_body = (
        "event: message\n"
        'data: {"type": "session.next.text.delta", '
        '"properties": {"sessionID": "ses_1", "delta": "hel"}}\n'
        "\n"
        "id: evt_2\n"
        'data: {"type": "session.next.text.ended", '
        '"properties": {"sessionID": "ses_1", "text": "hello"}}\n'
        "\n"
        ": heartbeat comment\n"
        "\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/event"
        return httpx.Response(200, text=sse_body, headers={"content-type": "text/event-stream"})

    client = _client(handler)
    events: list[OpenCodeEvent] = []
    async for event in client.events():
        events.append(event)
    assert [e.type for e in events] == [
        "session.next.text.delta",
        "session.next.text.ended",
    ]
    assert events[0].properties["delta"] == "hel"
    assert events[1].id == "evt_2"
    await client.aclose()


async def test_events_skips_non_json_data() -> None:
    sse_body = 'data: not-json\n\ndata: {"type": "x", "properties": {}}\n\n'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sse_body)

    client = _client(handler)
    events = [e async for e in client.events()]
    assert [e.type for e in events] == ["x"]
    await client.aclose()


async def test_create_session_non_object_body_raises() -> None:
    client = _client(lambda _r: httpx.Response(200, json=["not", "an", "object"]))
    with pytest.raises(OpenCodeClientError):
        await client.create_session()
    await client.aclose()


async def test_get_session_server_error_raises() -> None:
    client = _client(lambda _r: httpx.Response(500, json={"error": "boom"}))
    with pytest.raises(OpenCodeClientError):
        await client.get_session("ses_1")
    await client.aclose()


async def test_get_session_non_object_returns_none() -> None:
    client = _client(lambda _r: httpx.Response(200, json=["x"]))
    assert await client.get_session("ses_1") is None
    await client.aclose()


async def test_list_messages_non_list_returns_empty() -> None:
    client = _client(lambda _r: httpx.Response(200, json={"not": "a list"}))
    assert await client.list_messages("ses_1") == []
    await client.aclose()


async def test_get_message_non_dict_returns_empty() -> None:
    client = _client(lambda _r: httpx.Response(200, json=[1, 2]))
    assert await client.get_message("ses_1", "msg_1") == {}
    await client.aclose()


async def test_prompt_non_dict_returns_empty() -> None:
    client = _client(lambda _r: httpx.Response(200, json=[1]))
    assert await client.prompt("ses_1", {"parts": []}) == {}
    await client.aclose()


async def test_fork_non_object_body_raises() -> None:
    client = _client(lambda _r: httpx.Response(200, json="nope"))
    with pytest.raises(OpenCodeClientError):
        await client.fork("ses_1")
    await client.aclose()


async def test_request_json_http_error_raises() -> None:
    client = _client(lambda _r: httpx.Response(503, json={"error": "down"}))
    with pytest.raises(OpenCodeClientError):
        await client.list_messages("ses_1")
    await client.aclose()


async def test_summarize_posts_v1_endpoint_with_model() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=True)

    client = _client(handler)
    assert await client.summarize("ses_1", provider_id="anthropic", model_id="claude-sonnet-4-5")
    assert seen["method"] == "POST"
    assert seen["path"] == "/session/ses_1/summarize"
    assert seen["body"] == {"providerID": "anthropic", "modelID": "claude-sonnet-4-5"}
    await client.aclose()


async def test_summarize_raises_on_error() -> None:
    client = _client(lambda _r: httpx.Response(503, json={"error": "compact not available"}))
    with pytest.raises(OpenCodeClientError):
        await client.summarize("ses_1", provider_id="opencode", model_id="big-pickle")
    await client.aclose()


async def test_seed_context_posts_noreply_message() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"info": {"id": "msg_1"}})

    client = _client(handler)
    assert await client.seed_context("ses_1", "prior context", provider_id="p", model_id="m")
    assert seen["path"] == "/session/ses_1/message"
    body = seen["body"]
    assert body["noReply"] is True
    assert body["parts"] == [{"type": "text", "text": "prior context"}]
    assert body["model"] == {"providerID": "p", "modelID": "m"}
    await client.aclose()


async def test_seed_context_omits_model_when_absent() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={})

    client = _client(handler)
    assert await client.seed_context("ses_1", "ctx")
    assert "model" not in seen["body"]
    await client.aclose()


async def test_reply_question_posts_global_endpoint() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=True)

    client = _client(handler)
    assert await client.reply_question("que_1", [["Tabs"]])
    assert seen["method"] == "POST"
    # GLOBAL /question path (NOT session-scoped) — live-verified.
    assert seen["path"] == "/question/que_1/reply"
    assert seen["body"] == {"answers": [["Tabs"]]}
    await client.aclose()


async def test_reply_question_raises_on_error() -> None:
    client = _client(lambda _r: httpx.Response(404, json={"error": "unknown question"}))
    with pytest.raises(OpenCodeClientError):
        await client.reply_question("que_x", [["A"]])
    await client.aclose()


async def test_reject_question_posts_global_endpoint() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        return httpx.Response(200, json=True)

    client = _client(handler)
    assert await client.reject_question("que_1")
    assert seen["method"] == "POST"
    assert seen["path"] == "/question/que_1/reject"
    await client.aclose()
