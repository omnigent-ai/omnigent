"""E2E: ``StreamHooks`` sub-agent lifecycle hooks fire on a live sub-agent run.

``StreamHooks`` declares ``on_sub_agent_spawned`` / ``on_sub_agent_completed``
as part of the SDK's public hook surface (``omnigent_client._tool_handler``).
An observability adapter (e.g. an MLflow tracer) registers them to open a
per-sub-agent span when the orchestrator delegates work.

This test drives the real journey such an adapter takes, against a live
server + runner with the mock LLM (no API key needed):

1. register an orchestrator agent with a ``summarizer`` sub-agent tool,
2. bind ``StreamHooks`` with recorders for the sub-agent hooks (plus a
   generic recorder proving the hook adapter does see the stream),
3. send one user message whose scripted turn issues ``sys_session_send``,
4. wait until the sub-agent's output marker lands back in the parent
   session (the auto-wake continuation), proving a child session was
   spawned AND completed on the server,
5. assert the sub-agent lifecycle hooks fired.

If the hooks are declared but never dispatched, step 5 fails: the child
session provably ran to completion, other hooks fired from the same
stream, yet ``on_sub_agent_spawned`` and ``on_sub_agent_completed`` were
never called.

Usage::

    pytest tests/e2e/test_stream_hooks_subagent_lifecycle.py -v --timeout=300
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from typing import Any

import httpx
from omnigent_client._sessions import SessionsNamespace
from omnigent_client._sessions_chat import SessionsChat
from omnigent_client._tool_handler import (
    StreamHooks,
    SubAgentCompletedCtx,
    SubAgentSpawnedCtx,
)

from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    register_inline_agent,
    reset_mock_llm,
)

_MARKER = "SUBAGENT_LIFECYCLE_SUMMARY"


def test_sub_agent_lifecycle_hooks_fire_on_live_subagent_run(
    live_server: str,
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """A completed sub-agent run must dispatch the declared sub-agent hooks.

    **What breaks while the bug is live:** third-party tracers that register
    ``on_sub_agent_spawned`` / ``on_sub_agent_completed`` get silent no-ops —
    the SDK never invokes them from any stream path, so per-sub-agent spans,
    latency, and cost attribution are all lost.
    """
    uid = uuid.uuid4().hex[:6]
    parent_model = f"mock-subagent-hooks-parent-{uid}"
    child_model = f"mock-subagent-hooks-child-{uid}"

    reset_mock_llm(mock_llm_server_url)

    parent_name = register_inline_agent(
        http_client,
        name=f"subagent-hooks-parent-{uid}",
        harness="openai-agents",
        model=parent_model,
        profile="",
        prompt=(
            "You are a research assistant. You have a summarizer sub-agent. "
            "Call sys_session_send(agent='summarizer', title='photosynthesis', "
            "args='Summarize photosynthesis in 2 sentences') to spawn it. "
            "After its result arrives, quote it in your reply."
        ),
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
        extra_config={
            "tools": {
                "summarizer": {
                    "type": "agent",
                    "description": "Summarizes topics.",
                    "executor": {
                        "harness": "openai-agents",
                        "model": child_model,
                        "auth": {
                            "type": "api_key",
                            "api_key": "mock-key",
                            "base_url": f"{mock_llm_server_url}/v1",
                        },
                    },
                    "prompt": "You are a summarizer. Summarize the given topic.",
                },
            },
        },
    )

    # Parent turn script: dispatch the sub-agent, acknowledge, then quote the
    # collected result in the auto-wake continuation turn.
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": f"call_{uuid.uuid4().hex[:8]}",
                        "name": "sys_session_send",
                        "arguments": json.dumps(
                            {
                                "agent": "summarizer",
                                "title": "photosynthesis",
                                "args": "Summarize photosynthesis in 2 sentences",
                            }
                        ),
                    },
                ],
            },
            {"text": "Dispatched summarizer, waiting for result."},
            {"text": f"The summarizer returned: {_MARKER}"},
        ],
        key=parent_model,
    )
    # Child script: return the marker so its completion is observable in the
    # parent session once auto-collect delivers it.
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "text": (
                    f"{_MARKER} Photosynthesis converts sunlight, water, and "
                    "carbon dioxide into glucose and oxygen."
                ),
            },
        ],
        key=child_model,
    )

    session_id = create_runner_bound_session(
        http_client, agent_name=parent_name, runner_id=live_runner_id
    )

    spawned: list[SubAgentSpawnedCtx] = []
    completed: list[SubAgentCompletedCtx] = []
    other_hook_fires: list[str] = []
    raw_event_types: list[str] = []

    hooks = StreamHooks(
        # Generic recorders proving the stream did feed the hook adapter —
        # if these never fire either, the failure is environmental, not
        # the sub-agent-hook bug.
        on_tool_call_start=lambda ctx: other_hook_fires.append(f"tool_call_start:{ctx.name}"),
        on_response_start=lambda ctx: other_hook_fires.append("response_start"),
        on_response_end=lambda ctx: other_hook_fires.append("response_end"),
        # The sub-agent lifecycle hooks under test (declared public surface).
        on_sub_agent_spawned=spawned.append,
        on_sub_agent_completed=completed.append,
    )

    async def _drive() -> None:
        async with httpx.AsyncClient(
            timeout=300.0,
            headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
        ) as ac:
            ns = SessionsNamespace(ac, live_server)
            session = await ns.get(session_id)
            chat = SessionsChat(
                namespace=ns,
                files_uploader=None,
                files_getter=None,
                session=session,
                hooks=hooks,
            )

            first_event = asyncio.Event()

            async def _consume() -> None:
                # ``stream()`` is the SDK's hook-firing live tail: every
                # SSE event flows through ``_fire_stream_hooks``. It spans
                # both the dispatch turn and the auto-wake continuation,
                # so a wired ``on_sub_agent_completed`` would fire here.
                async for event in chat.stream():
                    raw_event_types.append(type(event).__name__)
                    first_event.set()

            consumer = asyncio.create_task(_consume())
            try:
                # Subscribe-before-post: the server has no replay buffer, so
                # wait for the subscription's first event (heartbeat) before
                # sending the message that triggers the spawn.
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(first_event.wait(), timeout=10.0)

                await ns.post_event(
                    session_id,
                    {
                        "type": "message",
                        "data": {
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": (
                                        "Use sys_session_send to spawn the summarizer. "
                                        "Ask it to summarize the concept of "
                                        "photosynthesis in exactly 2 sentences."
                                    ),
                                }
                            ],
                        },
                    },
                )

                # The sub-agent runs after the dispatch turn ends and its
                # result auto-wakes the parent; the marker landing in the
                # parent session proves spawn AND completion both happened.
                deadline = time.monotonic() + 240.0
                while time.monotonic() < deadline:
                    resp = await ac.get(f"{live_server}/v1/sessions/{session_id}")
                    resp.raise_for_status()
                    blob = json.dumps(resp.json().get("items", []))
                    if _MARKER in blob:
                        break
                    await asyncio.sleep(1.0)
                else:
                    raise AssertionError(
                        f"sub-agent marker {_MARKER!r} never appeared in session "
                        f"{session_id} — the spawn/auto-collect journey did not "
                        "complete, so the hook assertions below would be "
                        "meaningless (environment problem, not the bug)."
                    )
                # Grace period for any trailing continuation-turn events so
                # a wired implementation has ample time to dispatch.
                await asyncio.sleep(3.0)
            finally:
                consumer.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await consumer

    asyncio.run(_drive())

    # ── Controls: the journey really happened ────────────────────────────
    resp = http_client.get(f"/v1/sessions/{session_id}/child_sessions")
    resp.raise_for_status()
    children: list[dict[str, Any]] = resp.json().get("data", [])
    assert children, (
        "no child session was minted for the spawned sub-agent — the spawn "
        "journey itself failed, so this run cannot judge the hook contract."
    )
    assert raw_event_types, (
        "the SDK stream yielded no events at all — the SSE subscription "
        "failed, so this run cannot judge the hook contract."
    )
    assert other_hook_fires, (
        "no StreamHooks callback of any kind fired even though the stream "
        "yielded events — hook plumbing itself is broken, which is a "
        "different failure than the sub-agent-hook gap under test."
    )

    # ── The behavior under test: sub-agent lifecycle hooks fire ──────────────────────────────────
    assert spawned, (
        "StreamHooks.on_sub_agent_spawned never fired: a sub-agent session "
        f"was provably spawned (child_sessions={[c.get('id') for c in children]}) "
        f"and other hooks fired from the same stream ({other_hook_fires[:8]}...), "
        "yet the declared public sub-agent lifecycle hook was never invoked. "
        f"Raw stream events seen: {sorted(set(raw_event_types))}"
    )
    assert completed, (
        "StreamHooks.on_sub_agent_completed never fired even though the "
        "sub-agent completed and its result was auto-collected into the "
        f"parent session. Raw stream events seen: {sorted(set(raw_event_types))}"
    )
