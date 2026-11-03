"""Plan implementation tests for Codex session."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx

from omnigent.harnesses.codex_native import forwarder as codex_native_forwarder
from omnigent.harnesses.codex_native.bridge import (
    read_bridge_state,
)
from tests.harnesses.codex_native.session._support import (
    _completed_event,
    _expected_delta_data,
    _FakeCodexAppServerClient,
    _forwarder_context,
    _plan_delta_event,
    _write_forwarder_bridge,
)


def test_forwarder_synthesizes_plan_implementation_prompt_after_completed_plan_turn(
    tmp_path: Path,
) -> None:
    """
    Completed Plan-mode turns surface the final implementation prompt in Omnigent Web.

    Codex's terminal TUI owns the ``Implement this plan?`` picker
    locally, so the app-server does not emit a native
    ``item/tool/requestUserInput`` request. The forwarder must bridge
    that terminal-only prompt through the existing Codex elicitation
    hook after the completed plan item and terminal turn event arrive.
    """
    _write_forwarder_bridge(tmp_path, active_turn_id="turn_123")
    fake_client = _FakeCodexAppServerClient()
    forwarder_state = codex_native_forwarder._CodexForwarderState(model="mock-model")
    request_paths: list[str] = []
    request_bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Capture Omnigent posts and decline the synthesized prompt.

        :param request: HTTP request sent by the forwarder.
        :returns: Omnigent response appropriate to the endpoint.
        """
        request_paths.append(request.url.path)
        request_bodies.append(json.loads(request.content))
        if request.url.path.endswith("/events"):
            return httpx.Response(202, json={"queued": False})
        return httpx.Response(
            200,
            json={"answers": {"plan_implementation": {"answers": ["No, stay in Plan mode"]}}},
        )

    async def run() -> None:
        """
        Replay a streamed plan, completed plan item, and terminal completion.

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
            for event in [
                _plan_delta_event("turn_123", "item_plan", "1. Inspect existing flow"),
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": "thread_123",
                        "turnId": "turn_123",
                        "item": {
                            "type": "plan",
                            "id": "item_plan",
                            "text": "1. Inspect existing flow",
                        },
                    },
                },
                _completed_event("turn_123"),
            ]:
                await codex_native_forwarder._handle_event(
                    client,
                    **_forwarder_context(client, tmp_path),
                    event=event,
                    delta_coalescer=coalescer,
                    codex_client=fake_client,  # type: ignore[arg-type]
                    forwarder_state=forwarder_state,
                )
            await coalescer.close()

    asyncio.run(run())

    assert request_paths == [
        "/v1/sessions/conv_123/events",
        "/v1/sessions/conv_123/events",
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
    assert request_bodies[3] == {
        "id": "plan_implementation:turn_123",
        "method": "item/tool/requestUserInput",
        "params": {
            "threadId": "thread_123",
            "turnId": "turn_123",
            "itemId": "turn_123:plan_implementation",
            "questions": [
                {
                    "id": "plan_implementation",
                    "header": "Plan",
                    "question": "Implement this plan?",
                    "isOther": False,
                    "isSecret": False,
                    "options": [
                        {
                            "label": "Yes, implement this plan",
                            "description": "Switch to Default and start coding.",
                        },
                        {
                            "label": "Yes, clear context and implement",
                            "description": "Fresh thread with this plan.",
                        },
                        {
                            "label": "No, stay in Plan mode",
                            "description": "Continue planning with the model.",
                        },
                    ],
                }
            ],
        },
    }
    assert fake_client.requests == []


def test_forwarder_starts_default_turn_from_plan_implementation_prompt(
    tmp_path: Path,
) -> None:
    """
    Accepting the synthesized Plan prompt starts a Default-mode Codex turn.

    If the forwarder only displayed the web prompt without translating
    the answer back into Codex app-server actions, Omnigent Web would look
    interactive but selecting ``Yes, implement this plan`` would do
    nothing.
    """
    _write_forwarder_bridge(tmp_path, active_turn_id="turn_123")
    fake_client = _FakeCodexAppServerClient()
    forwarder_state = codex_native_forwarder._CodexForwarderState(
        model="mock-model",
        # A confirmed (config-read or live-notification) ABSENT read, the
        # way a real forwarder session would have it by the time a plan
        # prompt is answered — distinct from the never-yet-confirmed default,
        # which now makes _default_collaboration_mode refuse to build a
        # payload at all.
        developer_instructions_known=True,
    )

    async def fake_request(method: str, params: dict[str, Any]) -> dict[str, Any]:
        """
        Capture Codex app-server requests and return a started turn.

        :param method: JSON-RPC method.
        :param params: JSON-RPC params.
        :returns: JSON-RPC response envelope.
        """
        fake_client.requests.append((method, params))
        return {"result": {"turn": {"id": "turn_impl"}}}

    fake_client.request = fake_request  # type: ignore[method-assign]

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Accept the synthesized Plan implementation prompt.

        :param request: HTTP request sent by the forwarder.
        :returns: Omnigent hook or event response.
        """
        if request.url.path.endswith("/events"):
            return httpx.Response(202, json={"queued": False})
        return httpx.Response(
            200,
            json={"answers": {"plan_implementation": {"answers": ["Yes, implement this plan"]}}},
        )

    async def run() -> None:
        """
        Replay a completed plan turn and answer its web prompt.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            for event in [
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": "thread_123",
                        "turnId": "turn_123",
                        "item": {
                            "type": "plan",
                            "id": "item_plan",
                            "text": "1. Implement",
                        },
                    },
                },
                _completed_event("turn_123"),
            ]:
                await codex_native_forwarder._handle_event(
                    client,
                    **_forwarder_context(client, tmp_path),
                    event=event,
                    codex_client=fake_client,  # type: ignore[arg-type]
                    forwarder_state=forwarder_state,
                )

    asyncio.run(run())

    assert fake_client.requests == [
        (
            "turn/start",
            {
                "threadId": "thread_123",
                "input": [{"type": "text", "text": "Implement the plan."}],
                "collaborationMode": {
                    "mode": "default",
                    "settings": {
                        "model": "mock-model",
                        "reasoning_effort": None,
                        "developer_instructions": None,
                    },
                },
            },
        )
    ]
    state = read_bridge_state(tmp_path)
    assert state is not None
    assert state.active_turn_id == "turn_impl"


def test_forwarder_starts_fresh_thread_from_clear_context_plan_prompt(
    tmp_path: Path,
) -> None:
    """
    The clear-context Plan prompt choice creates a fresh Codex thread.

    This mirrors the terminal TUI action closely enough for Omnigent Web:
    the bridge switches to the new thread, sends the clear-context
    implementation prompt, and records the new active turn.
    """
    _write_forwarder_bridge(tmp_path, active_turn_id="turn_123")
    fake_client = _FakeCodexAppServerClient()
    forwarder_state = codex_native_forwarder._CodexForwarderState(
        model="mock-model",
        # See the sibling default-turn test above: a confirmed read is a
        # precondition for _default_collaboration_mode to build a payload.
        developer_instructions_known=True,
    )

    async def fake_request(method: str, params: dict[str, Any]) -> dict[str, Any]:
        """
        Capture Codex requests for fresh-thread plan implementation.

        :param method: JSON-RPC method.
        :param params: JSON-RPC params.
        :returns: JSON-RPC response envelope for the method.
        """
        fake_client.requests.append((method, params))
        if method == "thread/start":
            return {"result": {"thread": {"id": "thread_fresh"}}}
        return {"result": {"turn": {"id": "turn_fresh"}}}

    fake_client.request = fake_request  # type: ignore[method-assign]

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Select the clear-context implementation option.

        :param request: HTTP request sent by the forwarder.
        :returns: Omnigent hook or event response.
        """
        if request.url.path.endswith("/events"):
            return httpx.Response(202, json={"queued": False})
        return httpx.Response(
            200,
            json={
                "answers": {
                    "plan_implementation": {"answers": ["Yes, clear context and implement"]}
                }
            },
        )

    async def run() -> None:
        """
        Replay a completed plan turn and answer with clear-context.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            for event in [
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": "thread_123",
                        "turnId": "turn_123",
                        "item": {
                            "type": "plan",
                            "id": "item_plan",
                            "text": "- do the work",
                        },
                    },
                },
                _completed_event("turn_123"),
            ]:
                await codex_native_forwarder._handle_event(
                    client,
                    **_forwarder_context(client, tmp_path),
                    event=event,
                    codex_client=fake_client,  # type: ignore[arg-type]
                    forwarder_state=forwarder_state,
                )

    asyncio.run(run())

    assert fake_client.requests[0] == (
        "thread/start",
        {"model": "mock-model", "sessionStartSource": "clear"},
    )
    assert fake_client.requests[1][0] == "turn/start"
    assert fake_client.requests[1][1]["threadId"] == "thread_fresh"
    assert (
        fake_client.requests[1][1]["input"][0]["text"]
        == "A previous agent produced the plan below to accomplish the user's task. "
        "Implement the plan in a fresh context. Treat the plan as the source of "
        "user intent, re-read files as needed, and carry the work through "
        "implementation and verification.\n\n- do the work"
    )
    state = read_bridge_state(tmp_path)
    assert state is not None
    assert state.thread_id == "thread_fresh"
    assert state.active_turn_id == "turn_fresh"


def test_clear_context_plan_implementation_refuses_before_creating_thread_when_unconfirmed(
    tmp_path: Path,
) -> None:
    """
    The clear-context plan-implementation flow must validate the
    model/developer_instructions gate BEFORE creating (and switching to) a
    new Codex thread — not after.

    Regression: the old code only checked ``forwarder_state.model`` up
    front, then unconditionally created a fresh thread and recorded it as
    the bridge's active thread, and only THEN (inside
    ``_start_plan_implementation_turn``) checked
    ``developer_instructions_known`` and bailed. With never-confirmed
    developer_instructions (``developer_instructions_known=False``, e.g.
    every config.toml read so far has been UNREADABLE), that ordering let
    a bare ``thread/start`` through, switched the bridge to the new (now
    orphaned) empty thread, and silently never started the implementation
    turn — the user's "clear context and implement" choice would appear
    accepted but do nothing. Asserts NO ``thread/start`` (or any
    other) request reaches Codex, and the bridge state's thread_id is
    unchanged.
    """
    _write_forwarder_bridge(tmp_path, active_turn_id="turn_123")
    fake_client = _FakeCodexAppServerClient()
    # developer_instructions_known defaults to False — never confirmed.
    forwarder_state = codex_native_forwarder._CodexForwarderState(model="mock-model")

    async def fake_request(method: str, params: dict[str, Any]) -> dict[str, Any]:
        fake_client.requests.append((method, params))
        if method == "thread/start":
            return {"result": {"thread": {"id": "thread_fresh"}}}
        return {"result": {"turn": {"id": "turn_fresh"}}}

    fake_client.request = fake_request  # type: ignore[method-assign]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/events"):
            return httpx.Response(202, json={"queued": False})
        return httpx.Response(
            200,
            json={
                "answers": {
                    "plan_implementation": {"answers": ["Yes, clear context and implement"]}
                }
            },
        )

    async def run() -> None:
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            for event in [
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": "thread_123",
                        "turnId": "turn_123",
                        "item": {
                            "type": "plan",
                            "id": "item_plan",
                            "text": "- do the work",
                        },
                    },
                },
                _completed_event("turn_123"),
            ]:
                await codex_native_forwarder._handle_event(
                    client,
                    **_forwarder_context(client, tmp_path),
                    event=event,
                    codex_client=fake_client,  # type: ignore[arg-type]
                    forwarder_state=forwarder_state,
                )

    asyncio.run(run())

    assert fake_client.requests == [], (
        f"No Codex app-server request should have been issued when "
        f"developer_instructions_known is False; got {fake_client.requests!r}."
    )
    state = read_bridge_state(tmp_path)
    assert state is not None
    assert state.thread_id == "thread_123", (
        "Bridge state's active thread must be unchanged — no orphaned "
        "empty thread switch on a refused gate."
    )
