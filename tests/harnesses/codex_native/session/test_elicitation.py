"""Elicitation tests for Codex session."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx

from omnigent.harnesses.codex_native import forwarder as codex_native_forwarder
from omnigent.harnesses.codex_native.elicitation import codex_elicitation_id
from tests.harnesses.codex_native.session._support import (
    _agent_message_event,
    _elicitation_tracker,
    _expected_delta_data,
    _FakeCodexAppServerClient,
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
