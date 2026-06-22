"""Unit tests for :class:`omnigent_client._sessions.SessionsNamespace`.

Mocks at the HTTP transport boundary via :class:`httpx.MockTransport`,
using real types (``Session``, ``SessionEventInput``, the typed
``ServerStreamEvent`` union) throughout — per the project testing
guide, MagicMock is reserved for cases where a real type cannot be
constructed, and the SDK boundary types are all easy to construct.

What each test claims to prove (and what failure indicates):

* ``test_create_*``: that ``create()`` issues the right multipart
  request shape and round-trips the typed :class:`Session`. Failure
  means the request body is wrong (server rejects the upload) or the
  response decoder drops a field (e.g. ``runner_id``).
* ``test_get_*``: same for ``get()``.
* ``test_set_reasoning_effort_*``: that mutable reasoning-effort
  metadata is PATCHed through the sessions API rather than staying
  as a client-only cache.
* ``test_post_event_*`` / ``test_interrupt_*``: that ``post_event()``
  posts to the correct URL with the correct body, and that
  ``interrupt()`` sends the wire literal that the server's
  ``_INTERRUPT_TYPE`` matches. Failure means the cancel path is
  silently broken.
* ``test_stream_*``: that the SSE parser yields typed
  :data:`ServerStreamEvent` instances and that malformed/unknown
  payloads are skipped without aborting iteration. Failure means a
  schema drift between server and SDK silently drops events.
* ``test_*_404``: that the namespace propagates :class:`OmnigentError`
  for non-2xx responses; failure means errors are silently swallowed.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from typing import Any

import httpx
import pytest
from omnigent_client._errors import OmnigentError
from omnigent_client._sessions import (
    Session,
    SessionsNamespace,
)

from omnigent.server.schemas import (
    CompletedEvent,
    OutputTextDeltaEvent,
    SessionInputConsumedEvent,
    SessionStatusEvent,
)

# ── Helpers ───────────────────────────────────────────────────────────


def _make_namespace(
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[SessionsNamespace, httpx.AsyncClient]:
    """
    Build a :class:`SessionsNamespace` wired to a mock HTTP transport.

    :param handler: Callable invoked for every request. Receives the
        :class:`httpx.Request` and returns an :class:`httpx.Response`.
    :returns: The namespace and the underlying client (caller closes
        the client in a teardown).
    """
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, base_url="http://srv")
    return SessionsNamespace(client, "http://srv"), client


def _format_sse_lines(events: Iterable[tuple[str, dict[str, Any] | str]]) -> bytes:
    """
    Render a sequence of (event_type, payload) into SSE wire bytes.

    Mirrors the server's ``_format_sse`` helper. The payload is JSON-
    encoded unless it's a string sentinel like ``"[DONE]"``, in which
    case it's emitted verbatim.

    :param events: Pairs of ``(event_type, payload)``. Payload is a
        dict for normal events or a string for the ``[DONE]``
        sentinel (in which case ``event_type`` is ignored — the
        server emits ``data: [DONE]`` without an ``event:`` line).
    :returns: SSE-framed bytes ready to feed into a mocked response.
    """
    parts: list[str] = []
    for event_type, payload in events:
        if isinstance(payload, str):
            # Terminal/[DONE] sentinel — no event: line.
            parts.append(f"data: {payload}\n\n")
        else:
            parts.append(f"event: {event_type}\ndata: {json.dumps(payload)}\n\n")
    return "".join(parts).encode("utf-8")


def _session_response_body(
    session_id: str = "conv_abc",
    agent_id: str = "ag_abc",
    status: str = "running",
    items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Build a minimal :class:`SessionResponse` JSON dict.

    :param session_id: Session id, e.g. ``"conv_abc"``.
    :param agent_id: Bound agent id, e.g. ``"ag_abc"``.
    :param status: Session status, e.g. ``"running"``.
    :param items: Committed items list, defaulting to empty.
    :returns: A dict matching the server's ``SessionResponse`` shape.
    """
    return {
        "id": session_id,
        "agent_id": agent_id,
        "status": status,
        "created_at": 1700000000,
        "items": items if items is not None else [],
    }


# ── create() ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_posts_bundle_and_returns_typed_session() -> None:
    captured: dict[str, Any] = {}
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.method == "POST":
            captured["url"] = str(request.url)
            captured["method"] = request.method
            captured["content_type"] = request.headers["content-type"]
            captured["body"] = request.content
            return httpx.Response(201, json={"session_id": "conv_abc"})
        if request.method == "GET":
            assert str(request.url) == "http://srv/v1/sessions/conv_abc"
            return httpx.Response(
                200,
                json=_session_response_body(status="idle") | {"runner_id": "runner_local_test"},
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    ns, client = _make_namespace(handler)
    try:
        session = await ns.create(
            b"bundle-bytes",
            filename="agent.tar.gz",
            title="debug title",
            labels={"env": "test"},
            reasoning_effort="high",
        )
    finally:
        await client.aclose()

    # Wire shape: POST /v1/sessions with metadata + bundle parts,
    # then GET /v1/sessions/{id} because create returns only
    # {"session_id": ...}.
    assert calls == ["POST /v1/sessions", "GET /v1/sessions/conv_abc"]
    assert captured["method"] == "POST"
    assert captured["url"] == "http://srv/v1/sessions"
    assert str(captured["content_type"]).startswith("multipart/form-data; boundary=")
    body = bytes(captured["body"])
    assert b'name="metadata"' in body
    assert (
        b'{"title": "debug title", "labels": {"env": "test"}, "reasoning_effort": "high"}' in body
    )
    assert b'name="bundle"; filename="agent.tar.gz"' in body
    assert b"bundle-bytes" in body

    # Response is a real Session (not a dict), proving from_dict ran.
    assert isinstance(session, Session)
    assert session.id == "conv_abc"
    assert session.agent_id == "ag_abc"
    assert session.status == "idle"
    assert session.runner_id == "runner_local_test"


@pytest.mark.asyncio
async def test_create_with_empty_metadata_sends_json_object() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            captured["body"] = request.content
            return httpx.Response(201, json={"session_id": "conv_abc"})
        return httpx.Response(200, json=_session_response_body(items=[]))

    ns, client = _make_namespace(handler)
    try:
        session = await ns.create(b"bundle-bytes")
    finally:
        await client.aclose()

    body = bytes(captured["body"])
    assert b'name="metadata"' in body
    assert b"{}" in body
    assert session.items == []


@pytest.mark.asyncio
async def test_create_404_raises_omnigent_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={"error": {"code": "not_found", "message": "no such agent"}},
        )

    ns, client = _make_namespace(handler)
    try:
        with pytest.raises(OmnigentError) as exc_info:
            await ns.create(b"bundle-bytes")
    finally:
        await client.aclose()

    # Server-supplied message must propagate verbatim — proves the
    # SDK isn't swallowing the body or substituting a generic string.
    assert "no such agent" in str(exc_info.value)


# ── get() ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_returns_typed_session() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://srv/v1/sessions/conv_abc"
        return httpx.Response(
            200,
            json=_session_response_body(
                items=[{"id": "msg_1", "type": "message"}],
            ),
        )

    ns, client = _make_namespace(handler)
    try:
        session = await ns.get("conv_abc")
    finally:
        await client.aclose()

    assert isinstance(session, Session)
    # items round-trip as raw dicts (heterogeneous, intentionally
    # un-modeled per the namespace docstring).
    assert session.items == [{"id": "msg_1", "type": "message"}]


@pytest.mark.asyncio
async def test_get_parses_agent_name() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_session_response_body() | {"agent_name": "claude-native-ui"},
        )

    ns, client = _make_namespace(handler)
    try:
        session = await ns.get("conv_abc")
    finally:
        await client.aclose()

    # Proves Session.from_dict parses `agent_name` through — the REPL
    # uses it to refresh the displayed agent after an in-place switch.
    # A silent drop here would leave it at the None default and the
    # toolbar stuck on the launch-time agent.
    assert session.agent_name == "claude-native-ui"


@pytest.mark.asyncio
async def test_get_agent_name_defaults_to_none_when_omitted() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # Old servers omit agent_name from the payload entirely.
        return httpx.Response(200, json=_session_response_body())

    ns, client = _make_namespace(handler)
    try:
        session = await ns.get("conv_abc")
    finally:
        await client.aclose()

    # None (not "" or a KeyError) is the contract for old servers —
    # the REPL's hydrate keeps its launch-time name on None.
    assert session.agent_name is None


# ── bind_runner() ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bind_runner_patches_runner_id_and_returns_snapshot() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json=_session_response_body(status="idle") | {"runner_id": "runner_local_test"},
        )

    ns, client = _make_namespace(handler)
    try:
        session = await ns.bind_runner(
            "conv_abc",
            runner_id="runner_local_test",
        )
    finally:
        await client.aclose()

    assert captured == {
        "url": "http://srv/v1/sessions/conv_abc",
        "method": "PATCH",
        "body": {"runner_id": "runner_local_test"},
    }
    assert session.runner_id == "runner_local_test"


@pytest.mark.asyncio
async def test_bind_runner_400_raises_omnigent_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"code": "invalid_input", "message": "runner is offline"}},
        )

    ns, client = _make_namespace(handler)
    try:
        with pytest.raises(OmnigentError) as exc_info:
            await ns.bind_runner("conv_abc", runner_id="runner_offline")
    finally:
        await client.aclose()

    assert "runner is offline" in str(exc_info.value)


# ── set_reasoning_effort() ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_set_reasoning_effort_patches_metadata_and_returns_snapshot() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json=_session_response_body(status="idle") | {"reasoning_effort": "high"},
        )

    ns, client = _make_namespace(handler)
    try:
        session = await ns.set_reasoning_effort(
            "conv_abc",
            reasoning_effort="high",
        )
    finally:
        await client.aclose()

    assert captured == {
        "url": "http://srv/v1/sessions/conv_abc",
        "method": "PATCH",
        "body": {"reasoning_effort": "high"},
    }
    assert session.reasoning_effort == "high"


@pytest.mark.asyncio
async def test_set_reasoning_effort_none_sends_clear_alias() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json=_session_response_body(status="idle"))

    ns, client = _make_namespace(handler)
    try:
        session = await ns.set_reasoning_effort(
            "conv_abc",
            reasoning_effort=None,
        )
    finally:
        await client.aclose()

    assert captured["body"] == {"reasoning_effort": "default"}
    assert session.reasoning_effort is None


# ── set_archived() ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_set_archived_patches_and_returns_snapshot() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json=_session_response_body(status="idle") | {"archived": True},
        )

    ns, client = _make_namespace(handler)
    try:
        session = await ns.set_archived("conv_abc", archived=True)
    finally:
        await client.aclose()

    # The wire call must be a PATCH carrying exactly {"archived": true};
    # a wrong path/method/body would fail to archive server-side.
    assert captured == {
        "url": "http://srv/v1/sessions/conv_abc",
        "method": "PATCH",
        "body": {"archived": True},
    }
    # Proves Session.from_dict parses the new `archived` field through —
    # if it didn't, this would be the False default and pass silently.
    assert session.archived is True


@pytest.mark.asyncio
async def test_set_archived_unarchive_sends_false() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json=_session_response_body(status="idle") | {"archived": False},
        )

    ns, client = _make_namespace(handler)
    try:
        session = await ns.set_archived("conv_abc", archived=False)
    finally:
        await client.aclose()

    # Unarchive sends the explicit false (not an omitted/clear alias) so
    # the server flips the flag back rather than leaving it unchanged.
    assert captured["body"] == {"archived": False}
    assert session.archived is False


# ── post_event / interrupt ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_event_posts_body_to_events_url() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(202, json={"queued": True, "item_id": "ci_123"})

    ns, client = _make_namespace(handler)
    event = {"type": "message", "data": {"role": "user", "content": []}}
    try:
        ack = await ns.post_event("conv_abc", event)
    finally:
        await client.aclose()

    assert captured["url"] == "http://srv/v1/sessions/conv_abc/events"
    assert captured["body"] == event
    assert ack == {"queued": True, "item_id": "ci_123"}


@pytest.mark.asyncio
async def test_resolve_elicitation_posts_result_to_resolve_url() -> None:
    """
    ``resolve_elicitation`` POSTs the bare MCP ``ElicitationResult``
    body to the elicitation's dedicated resolve URL — the
    elicitation id rides in the URL path, not the body (URL-based
    elicitation). Asserting the exact URL guards against the verdict
    regressing back to a generic ``approval`` event on ``/events``.
    """
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(202, json={"queued": False})

    ns, client = _make_namespace(handler)
    try:
        ack = await ns.resolve_elicitation("conv_abc", "elicit_xyz", {"action": "accept"})
    finally:
        await client.aclose()

    assert captured["url"] == ("http://srv/v1/sessions/conv_abc/elicitations/elicit_xyz/resolve")
    assert captured["body"] == {"action": "accept"}
    assert ack == {"queued": False}


@pytest.mark.asyncio
async def test_interrupt_posts_interrupt_event_literal() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(202, json={"queued": False})

    ns, client = _make_namespace(handler)
    try:
        await ns.interrupt("conv_abc")
    finally:
        await client.aclose()

    # The literal "interrupt" must match the server's _INTERRUPT_TYPE
    # constant. If this drifts the server treats it as an unknown
    # event type and returns 400 — the test catches the drift here.
    assert captured["body"] == {"type": "interrupt", "data": {}}


@pytest.mark.asyncio
async def test_post_event_404_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={"error": {"code": "not_found", "message": "no session"}},
        )

    ns, client = _make_namespace(handler)
    try:
        with pytest.raises(OmnigentError):
            await ns.post_event("conv_x", {"type": "message", "data": {}})
    finally:
        await client.aclose()


# ── stream() ──────────────────────────────────────────────────────────


def _completed_response_dict(
    response_id: str = "resp_1",
    status: str = "completed",
) -> dict[str, Any]:
    """
    Build a minimal :class:`ResponseObject` JSON dict for use in the
    typed terminal event payload.

    :param response_id: Response id, e.g. ``"resp_1"``.
    :param status: Status string, e.g. ``"completed"``.
    :returns: Dict matching the server's ``ResponseObject`` shape's
        required fields.
    """
    return {
        "id": response_id,
        "status": status,
        "model": "test-model",
        "created_at": 1700000000,
    }


@pytest.mark.asyncio
async def test_stream_yields_typed_events_in_order() -> None:
    payloads: list[tuple[str, dict[str, Any] | str]] = [
        (
            "session.status",
            {
                "type": "session.status",
                "conversation_id": "conv_abc",
                "status": "running",
            },
        ),
        (
            "response.output_text.delta",
            {"type": "response.output_text.delta", "delta": "Hello "},
        ),
        (
            "response.output_text.delta",
            {"type": "response.output_text.delta", "delta": "world"},
        ),
        (
            "response.completed",
            {
                "type": "response.completed",
                "response": _completed_response_dict(),
            },
        ),
        ("done", "[DONE]"),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://srv/v1/sessions/conv_abc/stream"
        return httpx.Response(
            200,
            content=_format_sse_lines(payloads),
            headers={"content-type": "text/event-stream"},
        )

    ns, client = _make_namespace(handler)
    try:
        events = [event async for event in ns.stream("conv_abc")]
    finally:
        await client.aclose()

    # 4 well-formed events; [DONE] terminates without yielding.
    # If 3, the terminal event was lost — likely the [DONE] handling
    # regressed and short-circuited the previous event.
    # If 5, [DONE] was yielded as an event — adapter would have raised
    # but the caller would still see a count mismatch.
    assert len(events) == 4

    # Real types — proves the TypeAdapter dispatched on the
    # discriminator field. If isinstance fails here, the typed-union
    # validation regressed and downstream consumers would break.
    assert isinstance(events[0], SessionStatusEvent)
    assert events[0].status == "running"
    assert isinstance(events[1], OutputTextDeltaEvent)
    assert events[1].delta == "Hello "
    assert isinstance(events[3], CompletedEvent)
    assert events[3].response.id == "resp_1"


@pytest.mark.asyncio
async def test_stream_skips_malformed_and_unknown_events() -> None:
    payloads: list[tuple[str, dict[str, Any] | str]] = [
        # Unknown discriminator — should be logged and skipped.
        (
            "made.up.event",
            {"type": "made.up.event", "data": "ignored"},
        ),
        # Well-formed event after the bad one — must still be yielded.
        (
            "response.output_text.delta",
            {"type": "response.output_text.delta", "delta": "ok"},
        ),
        ("done", "[DONE]"),
    ]
    # Inject a raw malformed line directly between the two real
    # events to also cover the JSON-decode failure path.
    raw = b"event: made.up.event\ndata: not-json{{{\n\n" + _format_sse_lines(payloads)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=raw,
            headers={"content-type": "text/event-stream"},
        )

    ns, client = _make_namespace(handler)
    try:
        events = [event async for event in ns.stream("conv_abc")]
    finally:
        await client.aclose()

    # Exactly one event survives — the well-formed
    # OutputTextDeltaEvent. If 0, the adapter is too strict and a
    # malformed event is killing the iteration. If 2+, an unknown
    # event leaked through, indicating the discriminator validation
    # was bypassed.
    assert len(events) == 1
    assert isinstance(events[0], OutputTextDeltaEvent)
    assert events[0].delta == "ok"


@pytest.mark.asyncio
async def test_stream_404_raises_before_first_yield() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={"error": {"code": "not_found", "message": "no session"}},
        )

    ns, client = _make_namespace(handler)
    try:
        with pytest.raises(OmnigentError):
            async for _ in ns.stream("conv_missing"):
                pytest.fail("Should have raised before yielding any event")
    finally:
        await client.aclose()


# ── full session lifecycle ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sdk_full_session_lifecycle_with_reconnect() -> None:
    """
    Drive the SDK through the 4-step lifecycle a real client follows.

    1. Create a new session.
    2. Subscribe to the live stream.
    3. Send a message; the stream emits
       :class:`SessionInputConsumedEvent` echoing the user input and
       an :class:`OutputTextDeltaEvent` + :class:`CompletedEvent`
       with the assistant's reply.
    4. Disconnect from the stream. GET the session snapshot to
       confirm the assistant text persisted. Subscribe again, send a
       follow-up, see the new response. After the second turn the
       snapshot must include BOTH assistant messages in order
       (history persistence invariant).

    Asserts the SDK / server contract surfaces the live stream and
    the durable snapshot as the same data viewed differently:
    everything observed live during turn 1 is still visible after
    turn 2 via GET, plus turn 2's response on top.

    Production breakage that causes this test to fail:

    * ``SessionsNamespace.create`` drops the returned session id, so
      subsequent calls target the wrong path. Caught by the
      MockTransport's URL assertions.
    * ``SessionsNamespace.stream`` short-circuits before terminal
      event or fails to dispatch on the discriminator (events arrive
      as raw dicts, not typed). Caught by the ``isinstance`` checks
      on observed events.
    * ``SessionsNamespace.get`` returns a session whose ``items``
      don't preserve turn-1 history after turn 2 completes. Caught
      by the final cross-stream / snapshot equality assertion.
    """
    # State the mock server holds across the test. The handler
    # mutates these to simulate server-side persistence.
    session_id = "conv_lifecycle"
    agent_id = "ag_lifecycle"
    # Items committed to history (in the server's view). turn 1 sets
    # the user message + assistant reply; turn 2 appends another pair.
    history: list[dict[str, Any]] = []
    # Pop-one queue of canned SSE payload sequences, one per stream
    # subscribe. After step 3 the SDK disconnects and resubscribes,
    # so we need two SSE scripts.
    sse_scripts: list[list[tuple[str, dict[str, Any] | str]]] = []
    # The sequence of POSTs the SDK issued, for assertion.
    posted_events: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Stand in for the full ``/v1/sessions`` surface.

        Dispatches on method + path:

        * POST /v1/sessions → create the session, seed history.
        * GET  /v1/sessions/{id} → return snapshot of ``history``.
        * GET  /v1/sessions/{id}/stream → consume one sse_scripts entry.
        * POST /v1/sessions/{id}/events → record the event, mutate
          history with the user message immediately so the GET in
          step 4 matches what the live stream's ``session.input.
          consumed`` echo reported.

        :param request: The incoming :class:`httpx.Request`.
        :returns: A typed :class:`httpx.Response`.
        """
        url = str(request.url)
        method = request.method
        if method == "POST" and url == "http://srv/v1/sessions":
            return httpx.Response(201, json={"session_id": session_id})
        if method == "GET" and url == f"http://srv/v1/sessions/{session_id}":
            return httpx.Response(
                200,
                json=_session_response_body(
                    session_id=session_id,
                    agent_id=agent_id,
                    status="idle",
                    items=list(history),
                ),
            )
        if method == "GET" and url == f"http://srv/v1/sessions/{session_id}/stream":
            assert sse_scripts, (
                "stream subscription opened but no scripted SSE "
                "sequence remains — SDK opened more streams than the "
                "test prepared. The test specifies exactly two "
                "subscribe calls (one per turn)."
            )
            return httpx.Response(
                200,
                content=_format_sse_lines(sse_scripts.pop(0)),
                headers={"content-type": "text/event-stream"},
            )
        if method == "POST" and url == f"http://srv/v1/sessions/{session_id}/events":
            body = json.loads(request.content.decode())
            posted_events.append(body)
            # Persist the user input synchronously so the subsequent
            # GET sees it — matches the real server, which writes the
            # queued item before responding 202.
            if body.get("type") == "message":
                history.append({"type": "message", "data": body["data"]})
            return httpx.Response(202, json={"queued": True})
        raise AssertionError(
            f"Unexpected request: {method} {url} (body={request.content!r})",
        )

    # ── Script the SSE bodies for both turns ──────────────────────
    #
    # Turn 1: session.input.consumed (the server echoes the user
    # input as it materializes into history), then a single delta,
    # then the terminal response.completed.
    turn1_consumed_payload: dict[str, Any] = {
        "type": "session.input.consumed",
        "data": {
            "item_id": "ci_t1",
            "type": "message",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": "Hello 1"}],
            },
        },
    }
    turn1_assistant_text = "Hi there from turn 1"
    sse_scripts.append(
        [
            (
                "session.input.consumed",
                turn1_consumed_payload,
            ),
            (
                "response.output_text.delta",
                {
                    "type": "response.output_text.delta",
                    "delta": turn1_assistant_text,
                },
            ),
            (
                "response.completed",
                {
                    "type": "response.completed",
                    "response": _completed_response_dict(response_id="resp_t1"),
                },
            ),
            ("done", "[DONE]"),
        ],
    )
    turn2_assistant_text = "Reply to turn 2"
    sse_scripts.append(
        [
            (
                "session.input.consumed",
                {
                    "type": "session.input.consumed",
                    "data": {
                        "item_id": "ci_t2",
                        "type": "message",
                        "data": {
                            "role": "user",
                            "content": [{"type": "input_text", "text": "Hello 2"}],
                        },
                    },
                },
            ),
            (
                "response.output_text.delta",
                {
                    "type": "response.output_text.delta",
                    "delta": turn2_assistant_text,
                },
            ),
            (
                "response.completed",
                {
                    "type": "response.completed",
                    "response": _completed_response_dict(response_id="resp_t2"),
                },
            ),
            ("done", "[DONE]"),
        ],
    )

    ns, client = _make_namespace(handler)
    try:
        # ── Step 1: Create ────────────────────────────────────────
        session = await ns.create(b"bundle-bytes")
        # The SDK retains the durable id used by every subsequent
        # call; if create() dropped it, the URL assertions in the
        # handler would fire on the next request.
        assert session.id == session_id

        # ── Steps 2 + 3: Subscribe, send, observe turn 1 ─────────
        #
        # Open the stream FIRST (the new pub-sub model drops events
        # published before any subscriber connects, so subscribe
        # must precede post_event for the test to be valid).
        stream_iter_1 = ns.stream(session_id).__aiter__()

        # Issue the user message after the subscriber is connected.
        # In production this is racy, but with MockTransport the
        # entire stream body is prebuffered so ordering of the
        # handler dispatch doesn't matter — what matters for this
        # test is asserting the SDK's behavior end-to-end.
        await ns.post_event(
            session_id,
            {
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Hello 1"}],
                },
            },
        )

        turn1_events = []
        async for event in stream_iter_1:
            turn1_events.append(event)

        # Append the assistant reply to the server's history at the
        # point the turn terminates. The real server persists it via
        # ``conv_store.append`` after the workflow's
        # response.completed fires — same observable result.
        history.append(
            {
                "type": "message",
                "data": {
                    "role": "assistant",
                    "content": [
                        {"type": "output_text", "text": turn1_assistant_text},
                    ],
                },
            },
        )

        # Turn 1 stream invariants. If any of these fail, the SDK
        # isn't surfacing the live shape the spec advertises.
        consumed_events_1 = [e for e in turn1_events if isinstance(e, SessionInputConsumedEvent)]
        assert len(consumed_events_1) == 1, (
            f"Turn 1 emitted {len(consumed_events_1)} "
            f"session.input.consumed events, expected 1. The event "
            f"is the SDK's signal that the user input was accepted "
            f"into history; missing it means callers can't tell "
            f"when their input is live."
        )
        assert consumed_events_1[0].data.data["content"][0]["text"] == "Hello 1", (
            "The consumed event must echo the exact user text the "
            "SDK posted. Mismatch indicates the SessionInputConsumed "
            "decoder dropped fields from the nested data payload."
        )
        # Concatenated delta text equals the assistant's full reply.
        turn1_text = "".join(e.delta for e in turn1_events if isinstance(e, OutputTextDeltaEvent))
        assert turn1_text == turn1_assistant_text, (
            f"Turn 1 delta-text concat = {turn1_text!r}; expected "
            f"{turn1_assistant_text!r}. A mismatch indicates either "
            f"a missed delta or duplicate yielding."
        )
        # Stream observed exactly one terminal event in turn 1.
        completed_1 = [e for e in turn1_events if isinstance(e, CompletedEvent)]
        assert len(completed_1) == 1
        assert completed_1[0].response.id == "resp_t1"

        # ── Step 4a: Disconnect (implicit — the async-for exited) ──
        #
        # The SDK's stream() generator's finally would have closed
        # the underlying httpx response; we've left the loop, so
        # there's no further reading.

        # ── Step 4b: GET snapshot mid-flight ──────────────────────
        snapshot_after_turn_1 = await ns.get(session_id)
        # History contains the user message + the assistant reply
        # from turn 1. The exact two-item set is what step 4's final
        # equality check will compare against post-turn-2.
        post_turn_1_items = list(snapshot_after_turn_1.items)
        assert len(post_turn_1_items) == 2, (
            f"Snapshot after turn 1 has {len(post_turn_1_items)} "
            f"items, expected 2 (user + assistant). If 0/1, the "
            f"SDK is dropping history fields from the snapshot "
            f"decode; if 3+, server-side dedup regressed."
        )
        # The assistant text recorded in history matches the live
        # stream's delta concat — proves the live + durable views
        # are the same data.
        assistant_item = post_turn_1_items[1]
        assert assistant_item["data"]["content"][0]["text"] == turn1_assistant_text, (
            "The assistant text returned by GET must match the live "
            "stream's delta concat. If they diverge, the SDK's view "
            "of history is inconsistent with what it just observed."
        )

        # ── Step 4c: Resubscribe + second turn ────────────────────
        stream_iter_2 = ns.stream(session_id).__aiter__()
        await ns.post_event(
            session_id,
            {
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Hello 2"}],
                },
            },
        )
        turn2_events = []
        async for event in stream_iter_2:
            turn2_events.append(event)

        history.append(
            {
                "type": "message",
                "data": {
                    "role": "assistant",
                    "content": [
                        {"type": "output_text", "text": turn2_assistant_text},
                    ],
                },
            },
        )

        turn2_text = "".join(e.delta for e in turn2_events if isinstance(e, OutputTextDeltaEvent))
        assert turn2_text == turn2_assistant_text, (
            f"Turn 2 delta-text concat = {turn2_text!r}; expected "
            f"{turn2_assistant_text!r}. A mismatch indicates the "
            f"second subscribe didn't get a fresh stream cleanly."
        )

        # ── Step 4d: Final GET — both turns persist in order ──────
        final_snapshot = await ns.get(session_id)
        final_items = list(final_snapshot.items)
        # 4 items: user1, assistant1, user2, assistant2.
        assert len(final_items) == 4, (
            f"Final snapshot has {len(final_items)} items, "
            f"expected 4. If <4, an item from one of the turns "
            f"vanished; if >4, the SDK is duplicating items "
            f"across the reconnect."
        )
        # Order check — the assistant message from turn 1 must
        # appear BEFORE turn 2's items. This is the history-
        # persistence invariant the spec promises: clients can
        # always reconstruct full session state via GET, even
        # across stream disconnects.
        assert final_items[1]["data"]["content"][0]["text"] == turn1_assistant_text
        assert final_items[3]["data"]["content"][0]["text"] == turn2_assistant_text

        # ── Cross-check: stream-1 view ⊆ final history ────────────
        # The user-facing invariant for step 4's final assertion:
        # everything the client saw live during turn 1 still appears
        # in the post-turn-2 snapshot. The assistant text from
        # turn 1 is the strongest evidence — its presence proves
        # durable replay is consistent with what was streamed live.
        live_turn_1_text = "".join(
            e.delta for e in turn1_events if isinstance(e, OutputTextDeltaEvent)
        )
        snapshot_texts = [
            item["data"]["content"][0]["text"]
            for item in final_items
            if item.get("type") == "message" and item["data"].get("role") == "assistant"
        ]
        assert live_turn_1_text in snapshot_texts, (
            f"Turn 1 assistant text {live_turn_1_text!r} (observed "
            f"live) is missing from final snapshot's assistant "
            f"messages {snapshot_texts!r}. History persistence "
            f"across reconnect is broken — the SDK or server is "
            f"losing turn-1 state when turn 2 runs."
        )

        # ── Final assertions on the post-record ───────────────────
        assert len(posted_events) == 2, (
            f"Expected exactly 2 user posts (one per turn); "
            f"got {len(posted_events)}. The SDK should not be "
            f"retrying or duplicating user messages."
        )
    finally:
        await client.aclose()


# ── Fork ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fork_posts_correct_url_and_parses_response() -> None:
    """``fork()`` POSTs to ``/v1/sessions/{id}/fork`` and returns the raw dict.

    Verifies the URL path, request body, and that the response dict
    is returned verbatim. A wrong URL means the server never receives
    the request; a wrong body means the server rejects it.
    """
    captured_request: httpx.Request | None = None

    def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_request
        captured_request = request
        return httpx.Response(
            status_code=201,
            json={
                "id": "conv_fork",
                "agent_id": "ag_cloned",
                "status": "idle",
                "created_at": 1234,
                "title": "Fork of original",
                "items": [],
            },
        )

    ns, client = _make_namespace(_handler)
    try:
        result = await ns.fork("conv_src", title="Fork of original")
    finally:
        await client.aclose()

    # Verify the request was sent to the correct URL.
    assert captured_request is not None
    assert captured_request.url.path == "/v1/sessions/conv_src/fork"
    assert captured_request.method == "POST"

    # Verify the body included the title.
    body = json.loads(captured_request.content)
    assert body == {"title": "Fork of original"}, f"Expected body with title, got {body}"

    # Verify the response is parsed correctly.
    assert result["id"] == "conv_fork"
    assert result["agent_id"] == "ag_cloned"
    assert result["status"] == "idle"
    assert result["title"] == "Fork of original"


@pytest.mark.asyncio
async def test_fork_omits_title_when_none() -> None:
    """``fork()`` sends an empty body when no title is provided.

    The server should derive a default title. If the SDK sends
    ``{"title": null}`` instead of ``{}``, the server might reject
    it or override the default-title logic incorrectly.
    """
    captured_request: httpx.Request | None = None

    def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_request
        captured_request = request
        return httpx.Response(
            status_code=201,
            json={
                "id": "conv_fork",
                "agent_id": "ag_cloned",
                "status": "idle",
                "created_at": 1234,
                "title": None,
                "items": [],
            },
        )

    ns, client = _make_namespace(_handler)
    try:
        await ns.fork("conv_src")
    finally:
        await client.aclose()

    assert captured_request is not None
    body = json.loads(captured_request.content)
    # No title key in the body when None is passed.
    assert "title" not in body, f"Expected empty body (no title key), got {body}"


@pytest.mark.asyncio
async def test_fork_404_raises() -> None:
    """``fork()`` raises ``OmnigentError`` when the source session is missing.

    Failure to raise means the SDK is swallowing server errors.
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=404,
            json={"error": {"message": "Session not found", "code": "not_found"}},
        )

    ns, client = _make_namespace(_handler)
    try:
        with pytest.raises(OmnigentError):
            await ns.fork("conv_nonexistent")
    finally:
        await client.aclose()
