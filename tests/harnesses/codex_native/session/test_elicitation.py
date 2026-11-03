"""Elicitation tests for Codex session."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.harnesses.codex_native import forwarder as codex_native_forwarder
from omnigent.harnesses.codex_native.app_server import CodexAppServerResponseError
from omnigent.harnesses.codex_native.bridge import read_bridge_state
from omnigent.harnesses.codex_native.elicitation import codex_elicitation_id
from omnigent.inner import codex_native_executor
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


_ASYNC_QUESTION_ELICITATION_ID = codex_elicitation_id(
    "conv_123",
    "item/tool/requestUserInput",
    "async_question:call-async-1",
)
# Reply text the Codex 0.158 TUI sends after the user picks "Alpha".
_TUI_ASYNC_QUESTION_REPLY = (
    "<send_user_message_question_reply>\n"
    '[{"answer":"Alpha","question":"Which option do you choose?",'
    '"questionItemId":"[\\"request_user_input_async\\",\\"call-async-1\\",0]"}]\n'
    "</send_user_message_question_reply>"
)


def _async_question_event() -> dict[str, Any]:
    """
    Build the ``item/completed`` notification for a Codex async question.

    :returns: App-server event payload in the Codex 0.158 shape, with one
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


def _answer_async_question(
    bridge_dir: Path,
    fake_client: _FakeCodexAppServerClient,
    hook_response: httpx.Response,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Drive one async question whose Omnigent hook returns ``hook_response``.

    :param bridge_dir: Native Codex bridge directory.
    :param fake_client: Fake Codex app-server client.
    :param hook_response: Omnigent hook response for the question card.
    :returns: Elicitation hook bodies and the other session events posted.
    """
    hook_bodies: list[dict[str, Any]] = []
    posted_events: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Answer the question card and capture other Omnigent posts.

        :param request: HTTP request sent by the forwarder.
        :returns: Omnigent hook or event response.
        """
        if request.url.path.endswith("/hooks/codex-elicitation-request"):
            hook_bodies.append(json.loads(request.content))
            return hook_response
        posted_events.append(json.loads(request.content))
        return httpx.Response(202, json={"queued": False})

    async def run() -> None:
        """
        Handle the question and wait for its answer delivery.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            context = _forwarder_context(client, bridge_dir)
            await codex_native_forwarder._handle_event(
                client,
                **context,
                event=_async_question_event(),
                codex_client=fake_client,  # type: ignore[arg-type]
            )
            await context["elicitation_tracker"].drain()

    asyncio.run(run())
    return hook_bodies, posted_events


def _compact_steer_refusal() -> CodexAppServerResponseError:
    """
    Build Codex's refusal to steer an active compaction turn.

    :returns: App-server response error for a compact turn.
    """
    return CodexAppServerResponseError(
        {
            "code": -32600,
            "message": "cannot steer a compact turn",
            "data": {"codexErrorInfo": {"activeTurnNotSteerable": {"turnKind": "compact"}}},
        }
    )


def _undelivered_answer_notices(posted_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Select the forwarder's undelivered async question answer notices.

    :param posted_events: Session events the forwarder posted.
    :returns: Notice events, in post order.
    """
    return [
        event
        for event in posted_events
        if str(event["data"].get("source_id", "")).startswith("async-question-undelivered-")
    ]


def test_forwarder_publishes_codex_async_question_and_steers_answer(
    tmp_path: Path,
) -> None:
    """
    A Default-mode async question gets an Omnigent card that outlives its turn.

    Codex reports ``request_user_input_async`` only as an ``agentMessage``
    with ``delivery: "async"`` after the tool has returned. The card must
    stay open past ``turn/completed``, and the answer must reach the model in
    the Codex TUI's reply format by steering the turn active at that time.
    """
    _write_forwarder_bridge(tmp_path, active_turn_id="turn_123")
    fake_client = _FakeCodexAppServerClient(response={"result": {"turnId": "turn_456"}})
    hook_bodies: list[dict[str, Any]] = []
    posted_events: list[dict[str, Any]] = []
    hook_started = asyncio.Event()
    answer_ready = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        """
        Hold the question card until the test answers it.

        :param request: HTTP request sent by the forwarder.
        :returns: Omnigent hook or event response.
        """
        body = json.loads(request.content)
        if request.url.path.endswith("/hooks/codex-elicitation-request"):
            hook_bodies.append(body)
            hook_started.set()
            await answer_ready.wait()
            return httpx.Response(
                200,
                json={
                    "answers": {
                        "0": {"answers": ["Alpha"]},
                        "1": {"answers": ["Ship it — today"]},
                    }
                },
            )
        posted_events.append(body)
        return httpx.Response(202, json={"queued": False})

    async def run() -> None:
        """
        Ask, finish the asking turn, start another, then answer the card.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            context = _forwarder_context(client, tmp_path)
            for event in (
                _async_question_event(),
                _completed_event("turn_123", thread_id="thread_123"),
            ):
                await codex_native_forwarder._handle_event(
                    client,
                    **context,
                    event=event,
                    codex_client=fake_client,  # type: ignore[arg-type]
                )
                await asyncio.wait_for(hook_started.wait(), timeout=1.0)
            _write_forwarder_bridge(tmp_path, active_turn_id="turn_456")
            answer_ready.set()
            await context["elicitation_tracker"].drain()

    asyncio.run(run())

    assert hook_bodies == [
        {
            "id": "async_question:call-async-1",
            "method": "item/tool/requestUserInput",
            "params": {
                "threadId": "thread_123",
                "turnId": "turn_123",
                "itemId": "call-async-1",
                "isBlocking": False,
                "questions": [
                    {
                        "id": "0",
                        "question": "Which option do you choose?",
                        "isOther": True,
                        "isSecret": False,
                        "options": [{"label": "Alpha"}, {"label": "Beta"}],
                    },
                    {
                        "id": "1",
                        "question": "Anything else?",
                        "isOther": True,
                        "isSecret": False,
                        "options": [],
                    },
                ],
            },
        }
    ]
    assert "external_elicitation_resolved" not in [event["type"] for event in posted_events]
    assert fake_client.requests == [
        (
            "turn/steer",
            {
                "threadId": "thread_123",
                "expectedTurnId": "turn_456",
                "input": [
                    {
                        "type": "text",
                        "text": "<send_user_message_question_reply>\n"
                        '[{"answer":"Alpha","question":"Which option do you choose?",'
                        '"questionItemId":"[\\"request_user_input_async\\",\\"call-async-1\\",0]"},'
                        '{"answer":"Ship it — today","question":"Anything else?",'
                        '"questionItemId":"[\\"request_user_input_async\\",\\"call-async-1\\",1]"}]\n'
                        "</send_user_message_question_reply>",
                    }
                ],
            },
        )
    ]


@pytest.mark.parametrize("active_turn_id", [None, "turn_123"], ids=["idle", "stale"])
def test_forwarder_starts_turn_for_codex_async_question_answer_when_idle(
    tmp_path: Path,
    active_turn_id: str | None,
) -> None:
    """
    An async question answered after its turn ended starts a new Codex turn.

    A bridge that still records the ended turn gets Codex's stale-steer
    rejection first; either way the reply starts a turn that later web
    messages can steer.
    """
    _write_forwarder_bridge(tmp_path, active_turn_id=active_turn_id)
    fake_client = _FakeCodexAppServerClient()

    async def fake_request(method: str, params: dict[str, Any]) -> dict[str, Any]:
        """
        Reject steering an ended turn and start a new one.

        :param method: JSON-RPC method.
        :param params: JSON-RPC params.
        :returns: JSON-RPC response envelope.
        :raises CodexAppServerResponseError: For ``turn/steer``.
        """
        fake_client.requests.append((method, params))
        if method == "turn/steer":
            raise CodexAppServerResponseError(
                {"code": -32600, "message": "no active turn to steer"}
            )
        return {"result": {"turn": {"id": "turn_reply"}}}

    fake_client.request = fake_request  # type: ignore[method-assign]

    _answer_async_question(
        tmp_path,
        fake_client,
        httpx.Response(200, json={"answers": {"0": {"answers": ["Alpha"]}}}),
    )

    reply_input = [{"type": "text", "text": _TUI_ASYNC_QUESTION_REPLY}]
    expected_requests: list[tuple[str, dict[str, Any]]] = [
        ("turn/start", {"threadId": "thread_123", "input": reply_input})
    ]
    if active_turn_id is not None:
        expected_requests.insert(
            0,
            (
                "turn/steer",
                {"threadId": "thread_123", "expectedTurnId": "turn_123", "input": reply_input},
            ),
        )
    assert fake_client.requests == expected_requests
    state = read_bridge_state(tmp_path)
    assert state is not None
    assert state.active_turn_id == "turn_reply"


@pytest.mark.parametrize(
    "hook_response",
    [httpx.Response(200, json={"answers": {}}), httpx.Response(200)],
    ids=["declined", "timed_out"],
)
def test_forwarder_sends_nothing_for_unanswered_codex_async_question(
    tmp_path: Path,
    hook_response: httpx.Response,
) -> None:
    """
    A declined or expired async question card sends no reply to Codex.

    :param hook_response: Omnigent hook response with no answers.
    """
    _write_forwarder_bridge(tmp_path, active_turn_id="turn_123")
    fake_client = _FakeCodexAppServerClient()

    hook_bodies, _posted_events = _answer_async_question(tmp_path, fake_client, hook_response)

    assert [body["id"] for body in hook_bodies] == ["async_question:call-async-1"]
    assert fake_client.requests == []


def test_forwarder_clears_codex_async_question_answered_in_native_tui(
    tmp_path: Path,
) -> None:
    """
    A Codex TUI answer closes the Omnigent card without sending a second reply.

    The TUI answers with a tagged ``userMessage``. The forwarder must
    resolve the card and abandon its hook wait; a repeated ``item/completed``
    for the question must not publish a second card.
    """
    _write_forwarder_bridge(tmp_path, active_turn_id="turn_123")
    fake_client = _FakeCodexAppServerClient()
    hook_calls: list[dict[str, Any]] = []
    posted_events: list[dict[str, Any]] = []
    hook_started = asyncio.Event()
    hook_cancelled = asyncio.Event()
    tui_reply_event = {
        "method": "item/completed",
        "params": {
            "threadId": "thread_123",
            "turnId": "turn_123",
            "item": {
                "type": "userMessage",
                "id": "item_reply",
                "content": [
                    {"type": "text", "text": _TUI_ASYNC_QUESTION_REPLY, "text_elements": []}
                ],
            },
        },
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        """
        Hold the question card open and capture other Omnigent posts.

        :param request: HTTP request sent by the forwarder.
        :returns: Omnigent event response for non-hook posts.
        """
        body = json.loads(request.content)
        if request.url.path.endswith("/hooks/codex-elicitation-request"):
            hook_calls.append(body)
            hook_started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                hook_cancelled.set()
                raise
        posted_events.append(body)
        return httpx.Response(202, json={"queued": False})

    async def run() -> None:
        """
        Ask twice for the same item, then answer in the native TUI.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            context = _forwarder_context(client, tmp_path)
            for event in (_async_question_event(), _async_question_event(), tui_reply_event):
                await codex_native_forwarder._handle_event(
                    client,
                    **context,
                    event=event,
                    codex_client=fake_client,  # type: ignore[arg-type]
                )
                await asyncio.wait_for(hook_started.wait(), timeout=1.0)
            await asyncio.wait_for(hook_cancelled.wait(), timeout=1.0)
            await context["elicitation_tracker"].drain()

    asyncio.run(run())

    assert len(hook_calls) == 1
    assert [
        event for event in posted_events if event["type"] == "external_elicitation_resolved"
    ] == [
        {
            "type": "external_elicitation_resolved",
            "data": {"elicitation_id": _ASYNC_QUESTION_ELICITATION_ID},
        }
    ]
    assert fake_client.requests == []


def test_forwarder_delivers_codex_async_question_answer_after_compaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    An answer Codex refuses during compaction is delivered once compaction ends.
    """
    monkeypatch.setattr(codex_native_executor, "_BRIDGE_STATE_SLOW_POLL_SECONDS", 0.01)
    _write_forwarder_bridge(tmp_path, active_turn_id="turn_compact")
    fake_client = _FakeCodexAppServerClient()
    posted_events: list[dict[str, Any]] = []
    steer_refused = asyncio.Event()

    async def fake_request(method: str, params: dict[str, Any]) -> dict[str, Any]:
        """
        Refuse steering the compaction turn and start a new one.

        :param method: JSON-RPC method.
        :param params: JSON-RPC params.
        :returns: JSON-RPC response envelope.
        :raises CodexAppServerResponseError: For ``turn/steer``.
        """
        fake_client.requests.append((method, params))
        if method == "turn/steer":
            steer_refused.set()
            raise _compact_steer_refusal()
        return {"result": {"turn": {"id": "turn_reply"}}}

    fake_client.request = fake_request  # type: ignore[method-assign]

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Answer the question card and capture other Omnigent posts.

        :param request: HTTP request sent by the forwarder.
        :returns: Omnigent hook or event response.
        """
        if request.url.path.endswith("/hooks/codex-elicitation-request"):
            return httpx.Response(200, json={"answers": {"0": {"answers": ["Alpha"]}}})
        posted_events.append(json.loads(request.content))
        return httpx.Response(202, json={"queued": False})

    async def run() -> None:
        """
        Answer during compaction, then complete the compaction turn.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            context = _forwarder_context(client, tmp_path)
            for event in (
                _async_question_event(),
                _completed_event("turn_compact", thread_id="thread_123"),
            ):
                await codex_native_forwarder._handle_event(
                    client,
                    **context,
                    event=event,
                    codex_client=fake_client,  # type: ignore[arg-type]
                )
                await asyncio.wait_for(steer_refused.wait(), timeout=1.0)
            await asyncio.wait_for(context["elicitation_tracker"].drain(), timeout=5.0)

    asyncio.run(run())

    reply_input = [{"type": "text", "text": _TUI_ASYNC_QUESTION_REPLY}]
    assert fake_client.requests == [
        (
            "turn/steer",
            {"threadId": "thread_123", "expectedTurnId": "turn_compact", "input": reply_input},
        ),
        ("turn/start", {"threadId": "thread_123", "input": reply_input}),
    ]
    state = read_bridge_state(tmp_path)
    assert state is not None
    assert state.active_turn_id == "turn_reply"
    assert _undelivered_answer_notices(posted_events) == []


@pytest.mark.parametrize(
    ("steer_error", "advance_bridge", "steered_turn_ids"),
    [
        (ConnectionResetError("app-server connection lost"), False, ["turn_123"]),
        (_compact_steer_refusal(), False, ["turn_123"]),
        (_compact_steer_refusal(), True, ["turn_123", "turn_124"]),
    ],
    ids=["disconnected", "compaction_timed_out", "refused_after_compaction"],
)
def test_forwarder_posts_notice_when_codex_async_question_answer_is_undelivered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    steer_error: Exception,
    advance_bridge: bool,
    steered_turn_ids: list[str],
) -> None:
    """
    An answer Codex may not have received tells the user to check and resend.

    Delivery is retried only once, after the refused compaction turn settles,
    and the forwarder never reconnects the client carrying its subscription.

    :param steer_error: Failure from every steer that carries the answer.
    :param advance_bridge: Whether a new turn starts after each failed steer.
    :param steered_turn_ids: Turns the forwarder is expected to steer.
    """
    monkeypatch.setattr(codex_native_executor, "_BRIDGE_STATE_SLOW_POLL_SECONDS", 0.01)
    monkeypatch.setattr(codex_native_executor, "_COMPACTION_WAIT_TIMEOUT_SECONDS", 0.05)
    _write_forwarder_bridge(tmp_path, active_turn_id="turn_123")
    fake_client = _FakeCodexAppServerClient()

    async def fake_request(method: str, params: dict[str, Any]) -> dict[str, Any]:
        """
        Fail the steer, then start the next turn when the bridge advances.

        :param method: JSON-RPC method.
        :param params: JSON-RPC params.
        :returns: Never; every steer fails.
        :raises Exception: ``steer_error``.
        """
        fake_client.requests.append((method, params))
        if advance_bridge and len(fake_client.requests) < 3:
            _write_forwarder_bridge(
                tmp_path, active_turn_id=f"turn_{123 + len(fake_client.requests)}"
            )
        raise steer_error

    fake_client.request = fake_request  # type: ignore[method-assign]

    _hook_bodies, posted_events = _answer_async_question(
        tmp_path,
        fake_client,
        httpx.Response(200, json={"answers": {"0": {"answers": ["Alpha"]}}}),
    )

    reply_input = [{"type": "text", "text": _TUI_ASYNC_QUESTION_REPLY}]
    assert fake_client.requests == [
        (
            "turn/steer",
            {"threadId": "thread_123", "expectedTurnId": turn_id, "input": reply_input},
        )
        for turn_id in steered_turn_ids
    ]
    assert not fake_client.connected
    assert not fake_client.closed
    marker = "async-question-undelivered-call-async-1"
    assert _undelivered_answer_notices(posted_events) == [
        {
            "type": "external_conversation_item",
            "data": {
                "item_type": "message",
                "item_data": {
                    "role": "assistant",
                    "agent": "codex-native-ui",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "Couldn't confirm that Codex received your answer to "
                            '"Which option do you choose?". Check the terminal and send your '
                            "answer again if Codex didn't get it.",
                        }
                    ],
                },
                "response_id": marker,
                "source_id": marker,
            },
        }
    ]
