"""Elicitation tests for Codex session."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.harnesses.codex_native import forwarder as codex_native_forwarder
from omnigent.harnesses.codex_native.elicitation import codex_elicitation_id
from tests.harnesses.codex_native.session._support import (
    _agent_message_event,
    _completed_event,
    _elicitation_tracker,
    _expected_delta_data,
    _FakeCodexAppServerClient,
    _forwarder_context,
    _plan_delta_event,
    _usage_coalescer,
    _write_forwarder_bridge,
)


def test_forwarder_sends_codex_mcp_elicitation_response_to_app_server(
    tmp_path: Path,
) -> None:
    """
    Codex MCP elicitation requests are forwarded to Omnigent and the Omnigent hook
    result is sent back to the app-server with the original JSON-RPC id.
    """
    fake_client = _FakeCodexAppServerClient()
    requests: list[httpx.Request] = []
    codex_event = {
        "id": 3,
        "method": "mcpServer/elicitation/request",
        "params": {
            "threadId": "thread_123",
            "turnId": "turn_123",
            "serverName": "booking",
            "mode": "form",
            "message": "Pick a date",
            "requestedSchema": {"type": "object", "properties": {}},
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Capture the Omnigent hook request and return an accepted MCP result.

        :param request: HTTP request sent by the forwarder.
        :returns: Omnigent hook response.
        """
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "action": "accept",
                "content": {"date": "tomorrow"},
                "_meta": None,
            },
        )

    async def run() -> None:
        """
        Drive one app-server request through the forwarder.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            elicitation_tracker = _elicitation_tracker()
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_123",
                bridge_dir=tmp_path,
                usage_coalescer=_usage_coalescer(client),
                elicitation_tracker=elicitation_tracker,
                event=codex_event,
                codex_client=fake_client,  # type: ignore[arg-type]
            )
            await elicitation_tracker.drain()

    asyncio.run(run())

    assert [request.url.path for request in requests] == [
        "/v1/sessions/conv_123/hooks/codex-elicitation-request"
    ]
    assert json.loads(requests[0].content) == codex_event
    assert fake_client.responses == [
        (
            3,
            {
                "action": "accept",
                "content": {"date": "tomorrow"},
                "_meta": None,
            },
        )
    ]


def test_forwarder_keeps_streaming_when_native_tui_answers_codex_elicitation(
    tmp_path: Path,
) -> None:
    """
    Native TUI approval must not park the Omnigent web mirror.

    The Omnigent hook remains pending when a separate native Codex client
    answers the prompt first. Codex app-server emits
    ``serverRequest/resolved`` with the original request id; the
    forwarder must mirror that exact resolution to Omnigent and still mirror
    later transcript events.
    """
    fake_client = _FakeCodexAppServerClient()
    hook_started = asyncio.Event()
    hook_cancelled = asyncio.Event()
    posted_events: list[dict[str, Any]] = []
    codex_event = {
        "id": 3,
        "method": "mcpServer/elicitation/request",
        "params": {
            "threadId": "thread_123",
            "turnId": "turn_123",
            "serverName": "booking",
            "mode": "form",
            "message": "Pick a date",
            "requestedSchema": {"type": "object", "properties": {}},
        },
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        """
        Hold the hook open and capture subsequent Omnigent event posts.

        :param request: HTTP request sent by the forwarder.
        :returns: Omnigent event response for non-hook posts.
        """
        if request.url.path.endswith("/hooks/codex-elicitation-request"):
            hook_started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                hook_cancelled.set()
                raise
        posted_events.append(json.loads(request.content))
        return httpx.Response(202, json={"queued": False})

    async def run() -> None:
        """
        Drive a pending elicitation followed by native-side progress.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            elicitation_tracker = _elicitation_tracker()
            usage_coalescer = _usage_coalescer(client)
            await asyncio.wait_for(
                codex_native_forwarder._handle_event(
                    client,
                    session_id="conv_123",
                    bridge_dir=tmp_path,
                    usage_coalescer=usage_coalescer,
                    elicitation_tracker=elicitation_tracker,
                    event=codex_event,
                    codex_client=fake_client,  # type: ignore[arg-type]
                ),
                timeout=0.2,
            )
            await asyncio.wait_for(hook_started.wait(), timeout=1.0)
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_123",
                bridge_dir=tmp_path,
                usage_coalescer=usage_coalescer,
                elicitation_tracker=elicitation_tracker,
                event={
                    "method": "serverRequest/resolved",
                    "params": {
                        "threadId": "thread_123",
                        "requestId": 3,
                    },
                },
                codex_client=fake_client,  # type: ignore[arg-type]
            )
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_123",
                bridge_dir=tmp_path,
                usage_coalescer=usage_coalescer,
                elicitation_tracker=elicitation_tracker,
                event=_agent_message_event("turn_123", "item_agent", "after approval"),
                codex_client=fake_client,  # type: ignore[arg-type]
            )
            await elicitation_tracker.close()
            await asyncio.wait_for(hook_cancelled.wait(), timeout=1.0)

    asyncio.run(run())

    assert fake_client.responses == []
    assert posted_events == [
        {
            "type": "external_elicitation_resolved",
            "data": {
                "elicitation_id": codex_elicitation_id(
                    "conv_123",
                    "mcpServer/elicitation/request",
                    3,
                ),
            },
        },
        {
            "type": "external_conversation_item",
            "data": {
                "item_type": "message",
                "item_data": {
                    "role": "assistant",
                    "agent": "codex-native-ui",
                    "content": [{"type": "output_text", "text": "after approval"}],
                },
                "response_id": "codex_turn_123",
                "message_id": "codex:thread_123:turn_123:agentMessage:item_agent",
                "source_id": "thread_123:turn_123:item_agent",
            },
        },
    ]


def test_forwarder_ignores_resolution_for_different_codex_request_id(
    tmp_path: Path,
) -> None:
    """
    Codex resolution must match the pending JSON-RPC request id.

    Same-thread activity is not enough: a different
    ``serverRequest/resolved.requestId`` may belong to another
    server-to-client request, so forwarding it would clear the wrong web
    approval card.
    """
    fake_client = _FakeCodexAppServerClient()
    hook_started = asyncio.Event()
    posted_events: list[dict[str, Any]] = []
    codex_event = {
        "id": 3,
        "method": "mcpServer/elicitation/request",
        "params": {
            "threadId": "thread_123",
            "turnId": "turn_123",
            "serverName": "booking",
            "mode": "form",
            "message": "Pick a date",
            "requestedSchema": {"type": "object", "properties": {}},
        },
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        """
        Hold the hook open and capture subsequent Omnigent event posts.

        :param request: HTTP request sent by the forwarder.
        :returns: Omnigent event response for non-hook posts.
        """
        if request.url.path.endswith("/hooks/codex-elicitation-request"):
            hook_started.set()
            await asyncio.Future()
        posted_events.append(json.loads(request.content))
        return httpx.Response(202, json={"queued": False})

    async def run() -> None:
        """
        Drive a pending elicitation followed by an unrelated resolution.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            elicitation_tracker = _elicitation_tracker()
            usage_coalescer = _usage_coalescer(client)
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_123",
                bridge_dir=tmp_path,
                usage_coalescer=usage_coalescer,
                elicitation_tracker=elicitation_tracker,
                event=codex_event,
                codex_client=fake_client,  # type: ignore[arg-type]
            )
            await asyncio.wait_for(hook_started.wait(), timeout=1.0)
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_123",
                bridge_dir=tmp_path,
                usage_coalescer=usage_coalescer,
                elicitation_tracker=elicitation_tracker,
                event={
                    "method": "serverRequest/resolved",
                    "params": {
                        "threadId": "thread_123",
                        "requestId": 999,
                    },
                },
                codex_client=fake_client,  # type: ignore[arg-type]
            )
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_123",
                bridge_dir=tmp_path,
                usage_coalescer=usage_coalescer,
                elicitation_tracker=elicitation_tracker,
                event=_agent_message_event("turn_123", "item_agent", "after approval"),
                codex_client=fake_client,  # type: ignore[arg-type]
            )
            await elicitation_tracker.close()

    asyncio.run(run())

    assert [event["type"] for event in posted_events] == ["external_conversation_item"]
    assert posted_events[0]["data"]["item_data"]["content"][0]["text"] == "after approval"


def test_forwarder_falls_back_to_terminal_turn_for_missed_resolution(
    tmp_path: Path,
) -> None:
    """
    A terminal Codex turn clears a matching pending web prompt.

    ``serverRequest/resolved`` is the exact signal for native-side
    approval, but if the forwarder misses that notification then an
    accepted ``turn/completed`` for the same turn is the next safe
    lifecycle boundary proving Codex is no longer waiting on the
    server-to-client request.
    """
    fake_client = _FakeCodexAppServerClient()
    hook_started = asyncio.Event()
    hook_cancelled = asyncio.Event()
    posted_events: list[dict[str, Any]] = []
    codex_event = {
        "id": 3,
        "method": "mcpServer/elicitation/request",
        "params": {
            "threadId": "thread_123",
            "turnId": "turn_123",
            "serverName": "booking",
            "mode": "form",
            "message": "Pick a date",
            "requestedSchema": {"type": "object", "properties": {}},
        },
    }
    _write_forwarder_bridge(tmp_path, active_turn_id="turn_123")

    async def handler(request: httpx.Request) -> httpx.Response:
        """
        Hold the hook open and capture subsequent Omnigent event posts.

        :param request: HTTP request sent by the forwarder.
        :returns: Omnigent event response for non-hook posts.
        """
        if request.url.path.endswith("/hooks/codex-elicitation-request"):
            hook_started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                hook_cancelled.set()
                raise
        posted_events.append(json.loads(request.content))
        return httpx.Response(202, json={"queued": False})

    async def run() -> None:
        """
        Drive a pending elicitation followed by terminal-turn cleanup.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            elicitation_tracker = _elicitation_tracker()
            usage_coalescer = _usage_coalescer(client)
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_123",
                bridge_dir=tmp_path,
                usage_coalescer=usage_coalescer,
                elicitation_tracker=elicitation_tracker,
                event=codex_event,
                codex_client=fake_client,  # type: ignore[arg-type]
            )
            await asyncio.wait_for(hook_started.wait(), timeout=1.0)
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_123",
                bridge_dir=tmp_path,
                usage_coalescer=usage_coalescer,
                elicitation_tracker=elicitation_tracker,
                event={
                    "method": "turn/completed",
                    "params": {
                        "threadId": "thread_123",
                        "turn": {
                            "id": "turn_123",
                            "status": "completed",
                            "items": [],
                        },
                    },
                },
                codex_client=fake_client,  # type: ignore[arg-type]
            )
            await elicitation_tracker.close()
            await asyncio.wait_for(hook_cancelled.wait(), timeout=1.0)

    asyncio.run(run())

    assert fake_client.responses == []
    assert [event["type"] for event in posted_events] == [
        "external_session_status",
        "external_elicitation_resolved",
    ]
    assert posted_events[1]["data"]["elicitation_id"] == codex_elicitation_id(
        "conv_123",
        "mcpServer/elicitation/request",
        3,
    )


def test_forwarder_does_not_clear_pending_elicitation_for_stale_terminal_turn(
    tmp_path: Path,
) -> None:
    """
    Stale terminal turn events cannot clear a newer pending prompt.

    The fallback must run only after the active-turn guard accepts the
    terminal event. Otherwise a delayed completion from an older Codex
    turn could dismiss an approval card that belongs to the current
    native turn.
    """
    fake_client = _FakeCodexAppServerClient()
    hook_started = asyncio.Event()
    posted_events: list[dict[str, Any]] = []
    codex_event = {
        "id": 3,
        "method": "mcpServer/elicitation/request",
        "params": {
            "threadId": "thread_123",
            "turnId": "turn_new",
            "serverName": "booking",
            "mode": "form",
            "message": "Pick a date",
            "requestedSchema": {"type": "object", "properties": {}},
        },
    }
    _write_forwarder_bridge(tmp_path, active_turn_id="turn_new")

    async def handler(request: httpx.Request) -> httpx.Response:
        """
        Hold the hook open and capture subsequent Omnigent event posts.

        :param request: HTTP request sent by the forwarder.
        :returns: Omnigent event response for non-hook posts.
        """
        if request.url.path.endswith("/hooks/codex-elicitation-request"):
            hook_started.set()
            await asyncio.Future()
        posted_events.append(json.loads(request.content))
        return httpx.Response(202, json={"queued": False})

    async def run() -> None:
        """
        Drive a pending elicitation followed by stale turn completion.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            elicitation_tracker = _elicitation_tracker()
            usage_coalescer = _usage_coalescer(client)
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_123",
                bridge_dir=tmp_path,
                usage_coalescer=usage_coalescer,
                elicitation_tracker=elicitation_tracker,
                event=codex_event,
                codex_client=fake_client,  # type: ignore[arg-type]
            )
            await asyncio.wait_for(hook_started.wait(), timeout=1.0)
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_123",
                bridge_dir=tmp_path,
                usage_coalescer=usage_coalescer,
                elicitation_tracker=elicitation_tracker,
                event={
                    "method": "turn/completed",
                    "params": {
                        "threadId": "thread_123",
                        "turn": {
                            "id": "turn_old",
                            "status": "completed",
                            "items": [],
                        },
                    },
                },
                codex_client=fake_client,  # type: ignore[arg-type]
            )
            await elicitation_tracker.close()

    asyncio.run(run())

    assert fake_client.responses == []
    assert posted_events == []


def test_forwarder_sends_codex_request_user_input_response_to_app_server(
    tmp_path: Path,
) -> None:
    """
    Codex requestUserInput frames use the same Omnigent hook path and relay
    its ``answers`` result back to app-server.
    """
    fake_client = _FakeCodexAppServerClient()
    codex_event = {
        "id": "req_9",
        "method": "item/tool/requestUserInput",
        "params": {
            "threadId": "thread_123",
            "turnId": "turn_123",
            "itemId": "item_123",
            "questions": [{"id": "framework", "question": "Which framework?"}],
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Return a requestUserInput result from the Omnigent hook.

        :param request: HTTP request sent by the forwarder.
        :returns: Omnigent hook response.
        """
        assert request.url.path == "/v1/sessions/conv_123/hooks/codex-elicitation-request"
        assert json.loads(request.content) == codex_event
        return httpx.Response(
            200,
            json={"answers": {"framework": {"answers": ["React"]}}},
        )

    async def run() -> None:
        """
        Drive one requestUserInput frame through the forwarder.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            elicitation_tracker = _elicitation_tracker()
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_123",
                bridge_dir=tmp_path,
                usage_coalescer=_usage_coalescer(client),
                elicitation_tracker=elicitation_tracker,
                event=codex_event,
                codex_client=fake_client,  # type: ignore[arg-type]
            )
            await elicitation_tracker.drain()

    asyncio.run(run())

    assert fake_client.responses == [("req_9", {"answers": {"framework": {"answers": ["React"]}}})]


def test_forwarder_flushes_plan_text_before_codex_request_user_input(
    tmp_path: Path,
) -> None:
    """
    Buffered plan deltas reach Omnigent before the final plan prompt.

    Codex can emit ``item/plan/delta`` and immediately send
    ``item/tool/requestUserInput`` for "Implement this plan?". The
    forwarder coalesces text deltas, so it must flush the buffer before
    posting the long-poll elicitation hook; otherwise web sees the
    prompt before the plan content, or the plan stays buffered while
    the hook waits for a user answer.
    """
    _write_forwarder_bridge(tmp_path, active_turn_id="turn_123")
    fake_client = _FakeCodexAppServerClient()
    request_bodies: list[dict[str, Any]] = []
    request_paths: list[str] = []
    codex_event = {
        "id": "plan_prompt",
        "method": "item/tool/requestUserInput",
        "params": {
            "threadId": "thread_123",
            "turnId": "turn_123",
            "itemId": "item_plan_prompt",
            "questions": [
                {
                    "id": "plan_decision",
                    "header": "Plan",
                    "question": "Implement this plan?",
                    "isOther": False,
                    "isSecret": False,
                    "options": [{"label": "Yes, implement this plan"}],
                }
            ],
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Capture Omnigent posts in arrival order.

        :param request: HTTP request sent by the forwarder.
        :returns: Omnigent response appropriate to the endpoint.
        """
        request_paths.append(request.url.path)
        request_bodies.append(json.loads(request.content))
        if request.url.path.endswith("/events"):
            return httpx.Response(202, json={"queued": False})
        return httpx.Response(
            200,
            json={"answers": {"plan_decision": {"answers": ["Yes, implement this plan"]}}},
        )

    async def run() -> None:
        """
        Replay a plan delta followed by the plan-mode final prompt.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            coalescer = codex_native_forwarder._OutputTextDeltaCoalescer(
                client,
                "conv_123",
                flush_interval_seconds=60.0,
                flush_char_threshold=1000,
            )
            elicitation_tracker = _elicitation_tracker()
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_123",
                bridge_dir=tmp_path,
                usage_coalescer=_usage_coalescer(client),
                elicitation_tracker=elicitation_tracker,
                event=_plan_delta_event("turn_123", "item_plan", "1. Inspect existing flow"),
                delta_coalescer=coalescer,
                codex_client=fake_client,  # type: ignore[arg-type]
            )
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_123",
                bridge_dir=tmp_path,
                usage_coalescer=_usage_coalescer(client),
                elicitation_tracker=elicitation_tracker,
                event=codex_event,
                delta_coalescer=coalescer,
                codex_client=fake_client,  # type: ignore[arg-type]
            )
            await elicitation_tracker.drain()
            await coalescer.close()

    asyncio.run(run())

    assert request_paths == [
        "/v1/sessions/conv_123/events",
        "/v1/sessions/conv_123/hooks/codex-elicitation-request",
    ]
    assert request_bodies[0] == {
        "type": "external_output_text_delta",
        "data": _expected_delta_data(
            "1. Inspect existing flow",
            "turn_123",
            "item_plan",
            item_type="plan",
        ),
    }
    assert request_bodies[1] == codex_event
    assert fake_client.responses == [
        (
            "plan_prompt",
            {"answers": {"plan_decision": {"answers": ["Yes, implement this plan"]}}},
        )
    ]


def test_forwarder_leaves_codex_elicitation_pending_on_empty_hook_body(
    tmp_path: Path,
) -> None:
    """
    Empty Omnigent hook responses represent timeout/disconnect fallback, not
    an approval. The forwarder must not synthesize an accept/decline
    result back to Codex.
    """
    fake_client = _FakeCodexAppServerClient()

    def handler(_request: httpx.Request) -> httpx.Response:
        """
        Return the Omnigent hook's fail-ask shape.

        :param _request: HTTP request sent by the forwarder.
        :returns: Empty successful response.
        """
        return httpx.Response(200)

    async def run() -> None:
        """
        Drive one timed-out elicitation request through the forwarder.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            elicitation_tracker = _elicitation_tracker()
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_123",
                bridge_dir=tmp_path,
                usage_coalescer=_usage_coalescer(client),
                elicitation_tracker=elicitation_tracker,
                event={
                    "id": 4,
                    "method": "mcpServer/elicitation/request",
                    "params": {
                        "mode": "form",
                        "message": "Pick a value",
                        "requestedSchema": {"type": "object", "properties": {}},
                    },
                },
                codex_client=fake_client,  # type: ignore[arg-type]
            )
            await elicitation_tracker.drain()

    asyncio.run(run())

    assert fake_client.responses == []


def _async_question_elicitation_id(index: int) -> str:
    """
    Return the Omnigent elicitation id of one async question card.

    :param index: Position of the question in the call, e.g. ``0``.
    :returns: Elicitation id for the session ``conv_123``.
    """
    return codex_elicitation_id(
        "conv_123",
        "item/tool/requestUserInput",
        f"async_question:call-async-1:{index}",
    )


def _async_question_reply(index: int, question: str, answer: str) -> str:
    """
    Build the reply text the Codex TUI picker sends for one question.

    :param index: Position of the answered question, e.g. ``0``.
    :param question: Question text, e.g. ``"Which option do you choose?"``.
    :param answer: Chosen or typed answer, e.g. ``"Alpha"``.
    :returns: Tagged user message text.
    """
    question_item_id = json.dumps(
        ["request_user_input_async", "call-async-1", index], separators=(",", ":")
    )
    entry = {"answer": answer, "question": question, "questionItemId": question_item_id}
    body = json.dumps([entry], ensure_ascii=False, separators=(",", ":"))
    return f"<send_user_message_question_reply>\n{body}\n</send_user_message_question_reply>"


# ``GET /v1/info`` body of a server that shows non-blocking async question cards.
_ASYNC_QUESTION_CARD_SERVER_INFO: dict[str, Any] = {"codex_nonblocking_decline": True}


def _serve_with_server_info(
    handler: Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]],
    server_info: dict[str, Any] = _ASYNC_QUESTION_CARD_SERVER_INFO,
) -> httpx.MockTransport:
    """
    Answer ``GET /v1/info`` with ``server_info`` and other requests with ``handler``.

    :param handler: Mock handler for the forwarder's other Omnigent requests.
    :param server_info: ``GET /v1/info`` response body.
    :returns: Mock transport for the forwarder's HTTP client.
    """

    async def serve(request: httpx.Request) -> httpx.Response:
        """
        Route one forwarder request.

        :param request: HTTP request sent by the forwarder.
        :returns: Server info or the ``handler`` response.
        """
        if request.url.path == "/v1/info":
            return httpx.Response(200, json=server_info)
        response = handler(request)
        return response if isinstance(response, httpx.Response) else await response

    return httpx.MockTransport(serve)


def _async_question_event() -> dict[str, Any]:
    """
    Build the ``item/completed`` notification for a Codex async question.

    :returns: App-server event payload in the Codex 0.167 shape, with one
        option question and one free-text question.
    """
    event = _agent_message_event(
        "turn_123",
        "call-async-1",
        "Which option do you choose?\n- Alpha\n- Beta",
    )
    event["params"]["item"].update(
        {
            "phase": "final_answer",
            "delivery": "async",
            "questions": [
                {"title": "Which option do you choose?", "options": ["Alpha", "Beta"]},
                {"title": "Anything else?"},
            ],
        }
    )
    return event


def _user_message_event(text: str) -> dict[str, Any]:
    """
    Build the ``item/completed`` notification for a Codex user message.

    :param text: Message text, e.g. a tagged reply or ``"Use Blue"``.
    :returns: App-server event payload for the ``userMessage``.
    """
    return {
        "method": "item/completed",
        "params": {
            "threadId": "thread_123",
            "turnId": "turn_123",
            "item": {
                "type": "userMessage",
                "id": "item_reply",
                "content": [{"type": "text", "text": text, "text_elements": []}],
            },
        },
    }


def _answer_async_question(
    bridge_dir: Path,
    hook_response: httpx.Response | None,
    *,
    message_status: int = 202,
    server_info: dict[str, Any] = _ASYNC_QUESTION_CARD_SERVER_INFO,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], _FakeCodexAppServerClient]:
    """
    Drive one async question call whose card hooks all return ``hook_response``.

    :param bridge_dir: Native Codex bridge directory.
    :param hook_response: Omnigent hook response, or ``None`` when no card
        may be shown.
    :param message_status: Status for the forwarder's ``message`` event posts.
    :param server_info: ``GET /v1/info`` response body.
    :returns: Elicitation hook bodies, the session events posted, and the
        fake Codex client.
    """
    fake_client = _FakeCodexAppServerClient()
    hook_bodies: list[dict[str, Any]] = []
    posted_events: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Answer the question cards and capture session event posts.

        :param request: HTTP request sent by the forwarder.
        :returns: Omnigent hook or event response.
        """
        body = json.loads(request.content)
        if request.url.path.endswith("/hooks/codex-elicitation-request"):
            hook_bodies.append(body)
            assert hook_response is not None
            return hook_response
        posted_events.append(body)
        status = message_status if body["type"] == "message" else 202
        return httpx.Response(status, json={"queued": False})

    async def run() -> None:
        """
        Handle the question and wait for its answers to be sent.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=_serve_with_server_info(handler, server_info),
        ) as client:
            context = _forwarder_context(client, bridge_dir)
            await codex_native_forwarder._handle_event(
                client,
                **context,
                event=_async_question_event(),
                codex_client=fake_client,  # type: ignore[arg-type]
            )
            await asyncio.wait_for(context["elicitation_tracker"].drain(), timeout=30.0)

    asyncio.run(run())
    hook_bodies.sort(key=lambda body: body["id"])
    return hook_bodies, posted_events, fake_client


def _user_messages(posted_events: list[dict[str, Any]]) -> list[str]:
    """
    Select the ordinary user messages the forwarder sent on the session.

    :param posted_events: Session events the forwarder posted.
    :returns: Text of each ``message`` event, sorted.
    """
    return sorted(
        block["text"]
        for event in posted_events
        if event["type"] == "message"
        for block in event["data"]["content"]
    )


def test_forwarder_sends_each_codex_async_question_answer_as_a_user_message(
    tmp_path: Path,
) -> None:
    """
    Each async question gets its own non-blocking card, answered on its own.

    Codex reports ``request_user_input_async`` only as an ``agentMessage``
    with ``delivery: "async"`` after the tool has returned. Like the Codex
    TUI picker, each answer goes out as its own tagged reply through the
    session's ordinary message path, so the forwarder sends nothing to Codex.
    """
    _write_forwarder_bridge(tmp_path, active_turn_id="turn_123")

    hook_bodies, posted_events, fake_client = _answer_async_question(
        tmp_path,
        httpx.Response(
            200,
            json={"answers": {"0": {"answers": ["Alpha"]}, "1": {"answers": ["Ship it — today"]}}},
        ),
    )

    assert hook_bodies == [
        {
            "id": f"async_question:call-async-1:{index}",
            "method": "item/tool/requestUserInput",
            "params": {
                "threadId": "thread_123",
                "turnId": "turn_123",
                "itemId": "call-async-1",
                "isBlocking": False,
                "questions": [
                    {
                        "id": str(index),
                        "question": question,
                        "isOther": True,
                        "isSecret": False,
                        "options": options,
                    }
                ],
            },
        }
        for index, question, options in [
            (0, "Which option do you choose?", [{"label": "Alpha"}, {"label": "Beta"}]),
            (1, "Anything else?", []),
        ]
    ]
    assert _user_messages(posted_events) == [
        _async_question_reply(0, "Which option do you choose?", "Alpha"),
        _async_question_reply(1, "Anything else?", "Ship it — today"),
    ]
    assert fake_client.requests == []


@pytest.mark.parametrize(
    "hook_response",
    [httpx.Response(200, json={"answers": {}}), httpx.Response(200)],
    ids=["skipped", "closed"],
)
def test_forwarder_sends_nothing_for_unanswered_codex_async_question(
    tmp_path: Path,
    hook_response: httpx.Response,
) -> None:
    """
    A skipped or closed async question card sends nothing, like the TUI's skip.

    :param hook_response: Omnigent hook response with no answers.
    """
    _write_forwarder_bridge(tmp_path, active_turn_id="turn_123")

    hook_bodies, posted_events, fake_client = _answer_async_question(tmp_path, hook_response)

    assert [body["id"] for body in hook_bodies] == [
        "async_question:call-async-1:0",
        "async_question:call-async-1:1",
    ]
    assert _user_messages(posted_events) == []
    assert fake_client.requests == []


def test_forwarder_keeps_codex_async_question_in_terminal_for_older_server(
    tmp_path: Path,
) -> None:
    """
    A server without ``codex_nonblocking_decline`` gets no async question card.

    Such a server interrupts Codex when the card is declined, so the question
    stays answerable only in the native terminal.
    """
    _write_forwarder_bridge(tmp_path, active_turn_id="turn_123")

    hook_bodies, posted_events, _fake_client = _answer_async_question(
        tmp_path, None, server_info={"session_claude_sdk_clear": True}
    )

    assert hook_bodies == []
    assert _user_messages(posted_events) == []


def test_forwarder_posts_notice_when_codex_async_question_answer_cannot_be_sent(
    tmp_path: Path,
) -> None:
    """
    A rejected answer post leaves a notice telling the user to answer in the terminal.
    """
    _write_forwarder_bridge(tmp_path, active_turn_id="turn_123")

    _hook_bodies, posted_events, _fake_client = _answer_async_question(
        tmp_path,
        httpx.Response(200, json={"answers": {"0": {"answers": ["Alpha"]}}}),
        message_status=503,
    )

    assert [
        event["data"]["source_id"]
        for event in posted_events
        if str(event["data"].get("source_id", "")).startswith("async-question-undelivered-")
    ] == ["async-question-undelivered-call-async-1-0"]


@pytest.mark.parametrize(
    ("clearing_events", "closed_indexes"),
    [
        (
            [
                _user_message_event(
                    _async_question_reply(0, "Which option do you choose?", "Alpha")
                )
            ],
            [0],
        ),
        (
            [
                _user_message_event(
                    _async_question_reply(
                        0, "Which option do you choose?", "</send_user_message_question_reply>"
                    )
                )
            ],
            [0],
        ),
        ([_user_message_event("What is 2 plus 2?")], [0, 1]),
        (
            [
                _user_message_event(
                    "Is this the format? "
                    + _async_question_reply(0, "Which option do you choose?", "Alpha")
                )
            ],
            [0, 1],
        ),
        (
            [
                _user_message_event(
                    "```\n"
                    + _async_question_reply(0, "Which option do you choose?", "Alpha")
                    + "\n```"
                )
            ],
            [0, 1],
        ),
        ([_completed_event("turn_123", thread_id="thread_123")], [0, 1]),
    ],
    ids=[
        "tagged_reply",
        "reply_contains_closing_tag",
        "any_other_message",
        "reply_quoted_in_prose",
        "reply_in_code_fence",
        "turn_end",
    ],
)
def test_forwarder_closes_codex_async_questions_the_tui_queue_clears(
    tmp_path: Path,
    clearing_events: list[dict[str, Any]],
    closed_indexes: list[int],
) -> None:
    """
    Cards close exactly when the Codex TUI clears its queued questions.

    A tagged reply (from the TUI picker or a card) clears the questions it
    names; any other user message, typed in the TUI or the web, clears every
    open question; the turn's end clears them too. A closed card sends no
    answer, and a repeated ``item/completed`` must not publish it again.

    :param clearing_events: Codex events delivered after the question.
    :param closed_indexes: Questions whose cards those events close.
    """
    _write_forwarder_bridge(tmp_path, active_turn_id="turn_123")
    fake_client = _FakeCodexAppServerClient()
    hook_ids: list[str] = []
    cancelled_ids: list[str] = []
    cancelled_before_teardown: list[str] = []
    # Resolutions and hook cancellations, in the order the server sees them.
    server_log: list[str] = []
    posted_events: list[dict[str, Any]] = []
    both_hooks_started = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        """
        Hold the question cards open and capture other Omnigent posts.

        :param request: HTTP request sent by the forwarder.
        :returns: Omnigent event response for non-hook posts.
        """
        body = json.loads(request.content)
        if request.url.path.endswith("/hooks/codex-elicitation-request"):
            hook_ids.append(body["id"])
            if len(hook_ids) == 2:
                both_hooks_started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled_ids.append(body["id"])
                server_log.append(f"cancelled {body['id']}")
                raise
        if body["type"] == "external_elicitation_resolved":
            # Yield first, as a real server read would, so an already-requested
            # hook cancellation lands before this resolution.
            for _ in range(3):
                await asyncio.sleep(0)
            server_log.append(f"resolved {body['data']['elicitation_id']}")
        posted_events.append(body)
        return httpx.Response(202, json={"queued": False})

    async def run() -> None:
        """
        Ask twice for the same call, then deliver the clearing events.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=_serve_with_server_info(handler),
        ) as client:
            context = _forwarder_context(client, tmp_path)
            for event in (_async_question_event(), _async_question_event()):
                await codex_native_forwarder._handle_event(
                    client,
                    **context,
                    event=event,
                    codex_client=fake_client,  # type: ignore[arg-type]
                )
            await asyncio.wait_for(both_hooks_started.wait(), timeout=1.0)
            for event in clearing_events:
                await codex_native_forwarder._handle_event(
                    client,
                    **context,
                    event=event,
                    codex_client=fake_client,  # type: ignore[arg-type]
                )
            for _ in range(5):
                await asyncio.sleep(0)
            cancelled_before_teardown.extend(sorted(cancelled_ids))
            await context["elicitation_tracker"].close()

    asyncio.run(run())

    assert sorted(hook_ids) == ["async_question:call-async-1:0", "async_question:call-async-1:1"]
    assert sorted(
        event["data"]["elicitation_id"]
        for event in posted_events
        if event["type"] == "external_elicitation_resolved"
    ) == sorted(_async_question_elicitation_id(index) for index in closed_indexes)
    assert cancelled_before_teardown == [
        f"async_question:call-async-1:{index}" for index in closed_indexes
    ]
    # A dropped hook wait alone keeps the card up for the server's re-park
    # grace, so each card is resolved before its wait is dropped.
    for index in closed_indexes:
        assert server_log.index(
            f"resolved {_async_question_elicitation_id(index)}"
        ) < server_log.index(f"cancelled async_question:call-async-1:{index}")
    assert _user_messages(posted_events) == []
    assert fake_client.requests == []
