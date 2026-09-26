"""Tests for the OpenCode HTTP + SSE client against a fake server."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from omnigent.harnesses.opencode_native.client import (
    OpenCodeClient,
    OpenCodeClientError,
    OpenCodeEvent,
    OpenCodeSession,
    _unwrap,
)

Handler = Callable[[httpx.Request], httpx.Response]


def _client(handler: Handler, **kwargs: object) -> OpenCodeClient:
    mock = httpx.AsyncClient(
        base_url="http://opencode.test",
        transport=httpx.MockTransport(handler),
    )
    return OpenCodeClient("http://opencode.test", client=mock, **kwargs)  # type: ignore[arg-type]


_SESSION = {
    "id": "ses_1",
    "projectID": "prj_1",
    "title": "omnigent:conv_1",
    "model": {"id": "big-pickle", "providerID": "opencode"},
    "location": {"directory": "/repo"},
    "cost": 0,
    "tokens": {"input": 0},
    "time": {"created": 1},
}


def test_unwrap_returns_data_or_bare_body() -> None:
    assert _unwrap({"data": [1], "cursor": {}}) == [1]
    assert _unwrap({"location": {"directory": "/r"}, "data": {"id": "m"}}) == {"id": "m"}
    assert _unwrap({"version": "2.0.18"}) == {"version": "2.0.18"}
    assert _unwrap(None) is None


async def test_info_reads_bare_server_info() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert (request.method, request.url.path) == ("GET", "/api/info")
        return httpx.Response(
            200, json={"version": "2.0.18", "pid": 7, "urls": [], "paths": {"tmp": "/t"}}
        )

    client = _client(handler)
    assert (await client.info())["version"] == "2.0.18"
    await client.aclose()


async def test_create_session_posts_v2_body() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"], seen["path"] = request.method, request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"data": _SESSION})

    client = _client(handler)
    session = await client.create_session(
        title="omnigent:conv_1",
        directory="/repo",
        permissions=[{"action": "*", "resource": "*", "effect": "ask"}],
        metadata={"omnigent_conversation": "conv_1"},
    )
    assert (seen["method"], seen["path"]) == ("POST", "/api/session")
    assert seen["body"] == {
        "title": "omnigent:conv_1",
        "location": {"directory": "/repo"},
        "permissions": [{"action": "*", "resource": "*", "effect": "ask"}],
        "metadata": {"omnigent_conversation": "conv_1"},
    }
    assert session == OpenCodeSession(
        id="ses_1",
        title="omnigent:conv_1",
        parent_id=None,
        directory="/repo",
        model={"id": "big-pickle", "providerID": "opencode"},
        raw=_SESSION,
    )
    await client.aclose()


async def test_create_session_passes_initial_model_only_when_given() -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"data": _SESSION})

    client = _client(handler)
    await client.create_session(title="t", directory="/repo")
    await client.create_session(
        title="t", directory="/repo", model={"id": "big-pickle", "providerID": "opencode"}
    )
    assert bodies[0] == {"title": "t", "location": {"directory": "/repo"}}
    assert bodies[1]["model"] == {"id": "big-pickle", "providerID": "opencode"}
    await client.aclose()


async def test_create_session_non_object_body_raises() -> None:
    client = _client(lambda _r: httpx.Response(200, json={"data": ["x"]}))
    with pytest.raises(OpenCodeClientError):
        await client.create_session(title="t", directory="/repo")
    await client.aclose()


async def test_get_session_unwraps_and_reads_location() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/session/ses_1"
        return httpx.Response(200, json={"data": {**_SESSION, "parentID": "ses_0"}})

    client = _client(handler)
    session = await client.get_session("ses_1")
    assert session is not None
    assert session.parent_id == "ses_0"
    assert session.directory == "/repo"
    await client.aclose()


async def test_get_session_404_returns_none() -> None:
    client = _client(
        lambda _r: httpx.Response(
            404,
            json={"_tag": "SessionNotFoundError", "sessionID": "ses_x", "message": "nope"},
        )
    )
    assert await client.get_session("ses_x") is None
    await client.aclose()


async def test_list_messages_follows_cursor_pages() -> None:
    pages = {
        None: {"data": [{"id": "msg_1", "type": "user"}], "cursor": {"next": "c2"}},
        "c2": {"data": [{"id": "msg_2", "type": "assistant"}], "cursor": {"next": None}},
    }
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/session/ses_1/message"
        seen.append(dict(request.url.params))
        return httpx.Response(200, json=pages[request.url.params.get("cursor")])

    client = _client(handler)
    messages = await client.list_messages("ses_1")
    assert [m["id"] for m in messages] == ["msg_1", "msg_2"]
    assert seen == [{"order": "asc"}, {"cursor": "c2"}]
    await client.aclose()


async def test_list_messages_after_id_returns_only_newer() -> None:
    body = {"data": [{"id": "msg_1"}, {"id": "msg_2"}, {"id": "msg_3"}], "cursor": {}}
    client = _client(lambda _r: httpx.Response(200, json=body))
    newer = await client.list_messages("ses_1", after_id="msg_1")
    assert [m["id"] for m in newer] == ["msg_2", "msg_3"]
    unknown = await client.list_messages("ses_1", after_id="msg_unknown")
    assert [m["id"] for m in unknown] == ["msg_1", "msg_2", "msg_3"]
    await client.aclose()


async def test_list_messages_stops_on_repeated_cursor() -> None:
    body = {"data": [{"id": "msg_1"}], "cursor": {"next": "same"}}
    client = _client(lambda _r: httpx.Response(200, json=body))
    assert [m["id"] for m in await client.list_messages("ses_1")] == ["msg_1", "msg_1"]
    await client.aclose()


async def test_list_root_sessions_queries_newest_roots() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"], seen["path"] = request.method, request.url.path
        seen["params"] = dict(request.url.params)
        return httpx.Response(
            200,
            json={
                "data": [_SESSION, {**_SESSION, "id": "ses_0"}, "junk"],
                "cursor": {"next": None},
            },
        )

    client = _client(handler)
    sessions = await client.list_root_sessions(limit=5)
    assert (seen["method"], seen["path"]) == ("GET", "/api/session")
    assert seen["params"] == {"parentID": "null", "order": "desc", "limit": "5"}
    assert [s.id for s in sessions] == ["ses_1", "ses_0"]
    assert sessions[0].directory == "/repo"
    await client.aclose()


async def test_get_context_unwraps_list() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/session/ses_1/context"
        return httpx.Response(200, json={"data": [{"id": "msg_9", "type": "compaction"}]})

    client = _client(handler)
    assert await client.get_context("ses_1") == [{"id": "msg_9", "type": "compaction"}]
    await client.aclose()


async def test_error_carries_status_code() -> None:
    client = _client(
        lambda _r: httpx.Response(500, json={"_tag": "UnknownError", "message": "boom"})
    )
    with pytest.raises(OpenCodeClientError) as exc_info:
        await client.info()
    assert exc_info.value.status_code == 500
    await client.aclose()


async def test_auth_and_directory_headers_applied() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization", "")
        captured["dir"] = request.headers.get("x-opencode-directory", "")
        return httpx.Response(200, json={"data": [], "cursor": {}})

    client = _client(handler, headers={"Authorization": "Basic abc"}, directory="/repo/ünï dir")
    await client.list_messages("ses_1")
    assert captured["auth"] == "Basic abc"
    assert captured["dir"] == "/repo/%C3%BCn%C3%AF%20dir"
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
