"""Deltas tests for Codex session."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.harnesses.codex_native import forwarder as codex_native_forwarder
from omnigent.harnesses.codex_native.bridge import (
    read_bridge_state,
)
from tests.harnesses.codex_native.session._support import (
    _agent_message_delta_event,
    _agent_message_event,
    _completed_event,
    _expected_delta_data,
    _expected_status_data,
    _FakeCodexAppServerClient,
    _forwarder_context,
    _plan_delta_event,
    _recording_forwarder_client,
    _write_forwarder_bridge,
)


def test_forwarder_posts_active_codex_agent_message_delta(tmp_path: Path) -> None:
    """
    Codex assistant deltas are forwarded as transient Omnigent text deltas.

    Breaking the ``item/agentMessage/delta`` branch would leave the
    web stream silent until Codex posts its completed ``agentMessage``
    item, so this asserts on the exact Omnigent event envelope.
    """
    _write_forwarder_bridge(tmp_path, active_turn_id="turn_123")
    posted: list[dict[str, Any]] = []

    async def run() -> None:
        """
        Replay one active assistant-delta event.

        :returns: None.
        """
        async with _recording_forwarder_client(posted) as client:
            coalescer = codex_native_forwarder._OutputTextDeltaCoalescer(
                client,
                "conv_123",
                flush_interval_seconds=60.0,
                flush_char_threshold=1000,
            )
            await codex_native_forwarder._handle_event(
                client,
                **_forwarder_context(client, tmp_path),
                event=_agent_message_delta_event("turn_123", "item_agent", "hel"),
                delta_coalescer=coalescer,
            )
            await coalescer.flush()
            await coalescer.close()

    asyncio.run(run())

    assert posted == [
        {
            "type": "external_output_text_delta",
            "data": _expected_delta_data("hel", "turn_123", "item_agent"),
        }
    ]


def test_forwarder_persists_interrupted_codex_partial_agent_message(tmp_path: Path) -> None:
    """
    Interrupted Codex turns persist the visible partial assistant text.

    Codex interruption terminates the turn with ``turn/completed`` status
    ``interrupted`` and may never emit a completed ``agentMessage`` item.
    Without this fallback, Omnigent Web shows the streamed text live but loses it
    from durable history as soon as the turn ends.
    """
    _write_forwarder_bridge(tmp_path, active_turn_id="turn_123")
    posted: list[dict[str, Any]] = []
    forwarder_state = codex_native_forwarder._CodexForwarderState()

    async def run() -> None:
        """
        Replay assistant deltas followed by an interrupted terminal boundary.

        :returns: None.
        """
        async with _recording_forwarder_client(posted) as client:
            coalescer = codex_native_forwarder._OutputTextDeltaCoalescer(
                client,
                "conv_123",
                flush_interval_seconds=60.0,
                flush_char_threshold=1000,
            )
            for event in [
                _agent_message_delta_event("turn_123", "item_agent", "partial "),
                _agent_message_delta_event("turn_123", "item_agent", "answer"),
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "thread_123",
                        "turn": {"id": "turn_123", "status": "interrupted"},
                    },
                },
            ]:
                await codex_native_forwarder._handle_event(
                    client,
                    **_forwarder_context(client, tmp_path),
                    event=event,
                    delta_coalescer=coalescer,
                    forwarder_state=forwarder_state,
                )
            await coalescer.close()

    asyncio.run(run())

    assert posted == [
        {
            "type": "external_output_text_delta",
            "data": _expected_delta_data("partial answer", "turn_123", "item_agent"),
        },
        {
            "type": "external_session_interrupted",
            "data": {"response_id": "codex_turn_123"},
        },
        {
            "type": "external_conversation_item",
            "data": {
                "item_type": "message",
                "item_data": {
                    "role": "assistant",
                    "agent": "codex-native-ui",
                    "interrupted": True,
                    "content": [{"type": "output_text", "text": "partial answer"}],
                },
                "response_id": "codex_turn_123",
                "source_id": "thread_123:turn_123:interrupted-partial",
            },
        },
        {
            "type": "external_session_status",
            "data": _expected_status_data("idle", "turn_123"),
        },
    ]
    assert forwarder_state.partial_text_by_turn == {}
    state = read_bridge_state(tmp_path)
    assert state is not None
    assert state.active_turn_id is None


def test_forwarder_posts_active_codex_plan_delta(tmp_path: Path) -> None:
    """
    Codex plan deltas are forwarded as transient Omnigent text deltas.

    Plan mode uses ``item/plan/delta`` while rendering the visible
    plan. If this branch is missing, Omnigent web stays blank even though the
    Codex TUI is already showing the plan.
    """
    _write_forwarder_bridge(tmp_path, active_turn_id="turn_123")
    posted: list[dict[str, Any]] = []

    async def run() -> None:
        """
        Replay one active plan-delta event.

        :returns: None.
        """
        async with _recording_forwarder_client(posted) as client:
            coalescer = codex_native_forwarder._OutputTextDeltaCoalescer(
                client,
                "conv_123",
                flush_interval_seconds=60.0,
                flush_char_threshold=1000,
            )
            await codex_native_forwarder._handle_event(
                client,
                **_forwarder_context(client, tmp_path),
                event=_plan_delta_event("turn_123", "item_plan", "1. Inspect"),
                delta_coalescer=coalescer,
            )
            await coalescer.flush()
            await coalescer.close()

    asyncio.run(run())

    assert posted == [
        {
            "type": "external_output_text_delta",
            "data": _expected_delta_data(
                "1. Inspect",
                "turn_123",
                "item_plan",
                item_type="plan",
            ),
        }
    ]


def test_forwarder_recovers_active_turn_from_codex_plan_delta(tmp_path: Path) -> None:
    """
    Plan deltas mark the session running when ``turn/started`` was missed.

    Fresh TUI turns can begin before the forwarder finishes subscribing
    via ``thread/resume``. The delta itself carries both ``threadId``
    and ``turnId``; when no active turn is recorded yet and the thread
    matches bridge state, the forwarder adopts that turn, publishes the
    missing ``running`` status edge, and streams the plan instead of
    dropping the first visible content.
    """
    _write_forwarder_bridge(tmp_path, active_turn_id=None)
    posted: list[dict[str, Any]] = []

    async def run() -> None:
        """
        Replay a plan delta before any observed turn-start edge.

        :returns: None.
        """
        async with _recording_forwarder_client(posted) as client:
            coalescer = codex_native_forwarder._OutputTextDeltaCoalescer(
                client,
                "conv_123",
                flush_interval_seconds=60.0,
                flush_char_threshold=1000,
            )
            await codex_native_forwarder._handle_event(
                client,
                **_forwarder_context(client, tmp_path),
                event=_plan_delta_event("turn_early", "item_plan", "Draft plan"),
                delta_coalescer=coalescer,
            )
            await coalescer.flush()
            await coalescer.close()

    asyncio.run(run())

    assert posted == [
        {
            "type": "external_session_status",
            "data": _expected_status_data("running", "turn_early"),
        },
        {
            "type": "external_output_text_delta",
            "data": _expected_delta_data(
                "Draft plan",
                "turn_early",
                "item_plan",
                item_type="plan",
            ),
        },
    ]
    state = read_bridge_state(tmp_path)
    assert state is not None
    assert state.active_turn_id == "turn_early"


def test_forwarder_recovers_active_turn_from_codex_agent_message_delta(tmp_path: Path) -> None:
    """
    Assistant deltas mark the session running when ``turn/started`` was missed.

    Some Codex turns begin before the forwarder has subscribed to the
    app-server event stream. If the first observed event is already an
    ``item/agentMessage/delta`` for the current thread, the forwarder must
    adopt that turn and publish ``external_session_status: running``; otherwise
    the web thread's ``Working...`` indicator stays idle until text happens to
    render.

    :param tmp_path: Temporary bridge directory.
    :returns: None.
    """
    _write_forwarder_bridge(tmp_path, active_turn_id=None)
    posted: list[dict[str, Any]] = []

    async def run() -> None:
        """
        Replay an assistant delta before any observed turn-start edge.

        :returns: None.
        """
        async with _recording_forwarder_client(posted) as client:
            coalescer = codex_native_forwarder._OutputTextDeltaCoalescer(
                client,
                "conv_123",
                flush_interval_seconds=60.0,
                flush_char_threshold=1000,
            )
            await codex_native_forwarder._handle_event(
                client,
                **_forwarder_context(client, tmp_path),
                event=_agent_message_delta_event("turn_early", "item_agent", "Hello"),
                delta_coalescer=coalescer,
            )
            await coalescer.flush()
            await coalescer.close()

    asyncio.run(run())

    assert posted == [
        {
            "type": "external_session_status",
            "data": _expected_status_data("running", "turn_early"),
        },
        {
            "type": "external_output_text_delta",
            "data": _expected_delta_data("Hello", "turn_early", "item_agent"),
        },
    ]
    state = read_bridge_state(tmp_path)
    assert state is not None
    assert state.active_turn_id == "turn_early"


def test_forwarder_recovers_user_before_recovered_agent_message_delta(tmp_path: Path) -> None:
    """
    Recovered assistant deltas do not stream above a missed user message.

    If the observer misses ``turn/started``, ``userMessage``, and
    ``item/started``, the first visible event may be an assistant delta.
    Adopting the turn is correct, but the forwarder must first recover and
    post the user item so the web transcript order remains user then
    assistant.

    :param tmp_path: Temporary bridge directory.
    :returns: None.
    """
    _write_forwarder_bridge(tmp_path, active_turn_id=None)
    resume_response = {
        "result": {
            "thread": {
                "id": "thread_123",
                "turns": [
                    {
                        "id": "turn_early",
                        "items": [
                            {
                                "type": "userMessage",
                                "id": "item_user",
                                "content": [{"type": "text", "text": "hello"}],
                            }
                        ],
                    }
                ],
            }
        }
    }
    fake_client = _FakeCodexAppServerClient(response=resume_response)
    forwarder_state = codex_native_forwarder._CodexForwarderState(
        parent_session_id="conv_123",
        codex_client=fake_client,  # type: ignore[arg-type]
    )
    posted: list[dict[str, Any]] = []

    async def run() -> None:
        """
        Replay an assistant delta before all earlier turn events.

        :returns: None.
        """
        async with _recording_forwarder_client(posted) as client:
            coalescer = codex_native_forwarder._OutputTextDeltaCoalescer(
                client,
                "conv_123",
                flush_interval_seconds=60.0,
                flush_char_threshold=1000,
            )
            await codex_native_forwarder._handle_event(
                client,
                **_forwarder_context(client, tmp_path),
                event=_agent_message_delta_event("turn_early", "item_agent", "Hello"),
                delta_coalescer=coalescer,
                expected_thread_id="thread_123",
                forwarder_state=forwarder_state,
            )
            await coalescer.flush()
            await coalescer.close()

    asyncio.run(run())

    assert [payload["type"] for payload in posted] == [
        "external_conversation_item",
        "external_session_status",
        "external_output_text_delta",
    ]
    assert posted[0]["data"]["item_data"]["role"] == "user"
    assert posted[0]["data"]["item_data"]["content"][0]["text"] == "hello"
    assert posted[1]["data"] == _expected_status_data("running", "turn_early")
    assert posted[2]["data"] == _expected_delta_data("Hello", "turn_early", "item_agent")
    assert [method for method, _ in fake_client.requests] == ["thread/resume"]
    assert forwarder_state.has_posted_user_message("turn_early")


def test_forwarder_drops_stale_and_malformed_codex_agent_message_deltas(
    tmp_path: Path,
) -> None:
    """
    Codex assistant deltas only stream for the active turn.

    This prevents a delayed delta from an older rapid-send turn from
    appearing in the current web bubble. Non-string deltas are dropped
    before they can reach AP's strict ``data.delta`` validation.
    """
    _write_forwarder_bridge(tmp_path, active_turn_id="turn_new")
    posted: list[dict[str, Any]] = []

    async def run() -> None:
        """
        Replay stale, malformed, and valid delta notifications.

        :returns: None.
        """
        async with _recording_forwarder_client(posted) as client:
            coalescer = codex_native_forwarder._OutputTextDeltaCoalescer(
                client,
                "conv_123",
                flush_interval_seconds=60.0,
                flush_char_threshold=1000,
            )
            for event in [
                _agent_message_delta_event("turn_old", "item_old", "stale"),
                _agent_message_delta_event("turn_new", "item_new", {"text": "bad"}),
                _agent_message_delta_event("turn_new", "item_new", "fresh"),
            ]:
                await codex_native_forwarder._handle_event(
                    client,
                    **_forwarder_context(client, tmp_path),
                    event=event,
                    delta_coalescer=coalescer,
                )
            await coalescer.flush()
            await coalescer.close()

    asyncio.run(run())

    assert posted == [
        {
            "type": "external_output_text_delta",
            "data": _expected_delta_data("fresh", "turn_new", "item_new"),
        }
    ]


def test_forwarder_coalesces_codex_agent_message_deltas(tmp_path: Path) -> None:
    """
    Native Codex streaming does not post one Omnigent event per tiny delta.

    Breaking the coalescer would recreate the slow-drain failure where
    Codex finishes locally while the Omnigent SSE stream is still serialized
    behind many per-token HTTP POSTs. Stale and malformed deltas must
    still be filtered before text enters the coalesced buffer.
    """
    _write_forwarder_bridge(tmp_path, active_turn_id="turn_new")
    posted: list[dict[str, Any]] = []

    async def run() -> None:
        """
        Replay multiple delta notifications through the coalescer.

        :returns: None.
        """
        async with _recording_forwarder_client(posted) as client:
            coalescer = codex_native_forwarder._OutputTextDeltaCoalescer(
                client,
                "conv_123",
                flush_interval_seconds=60.0,
                flush_char_threshold=1000,
            )
            for event in [
                _agent_message_delta_event("turn_old", "item_old", "stale"),
                _agent_message_delta_event("turn_new", "item_new", {"text": "bad"}),
                _agent_message_delta_event("turn_new", "item_new", "hel"),
                _agent_message_delta_event("turn_new", "item_new", "lo"),
            ]:
                await codex_native_forwarder._handle_event(
                    client,
                    **_forwarder_context(client, tmp_path),
                    event=event,
                    delta_coalescer=coalescer,
                )
            await coalescer.flush()
            await coalescer.close()

    asyncio.run(run())

    assert posted == [
        {
            "type": "external_output_text_delta",
            "data": _expected_delta_data("hello", "turn_new", "item_new"),
        }
    ]


@pytest.mark.parametrize(
    ("deltas", "flush_interval_seconds", "flush_char_threshold", "expected_delta"),
    [
        (["timed"], 0.001, 1000, "timed"),
        (["abc", "de"], 60.0, 5, "abcde"),
        (["line\n"], 60.0, 1000, "line\n"),
    ],
)
def test_output_text_delta_coalescer_auto_flushes(
    deltas: list[str],
    flush_interval_seconds: float,
    flush_char_threshold: int,
    expected_delta: str,
) -> None:
    """
    The coalescer flushes without an explicit flush barrier.

    Each parametrized case isolates one automatic trigger: timer
    expiry, character threshold, and newline. The test waits for the
    Omnigent post directly instead of calling ``flush()``, so removing any
    trigger leaves that case stuck until ``wait_for`` fails.

    :param deltas: Text fragments appended to the coalescer, e.g.
        ``["abc", "de"]``.
    :param flush_interval_seconds: Timer budget for the first buffered
        delta, e.g. ``0.001``.
    :param flush_char_threshold: Buffered character threshold that
        triggers a flush, e.g. ``5``.
    :param expected_delta: Coalesced Omnigent delta payload.
    :returns: None.
    """
    posted: list[dict[str, Any]] = []

    async def run() -> None:
        """
        Append deltas and wait for the automatic Omnigent post.

        :returns: None.
        """
        posted_event = asyncio.Event()

        def handler(request: httpx.Request) -> httpx.Response:
            """
            Capture Omnigent event posts from the coalescer.

            :param request: HTTP request sent by the coalescer.
            :returns: Accepted response.
            """
            posted.append(json.loads(request.content))
            posted_event.set()
            return httpx.Response(202, json={"queued": False})

        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            coalescer = codex_native_forwarder._OutputTextDeltaCoalescer(
                client,
                "conv_123",
                flush_interval_seconds=flush_interval_seconds,
                flush_char_threshold=flush_char_threshold,
            )
            for delta in deltas:
                await coalescer.append(delta)
            await asyncio.wait_for(posted_event.wait(), timeout=1.0)
            await coalescer.close()

    asyncio.run(run())

    assert posted == [
        {
            "type": "external_output_text_delta",
            "data": {"delta": expected_delta},
        }
    ]


def test_forwarder_flushes_coalesced_deltas_before_completed_agent_item(
    tmp_path: Path,
) -> None:
    """
    Completed Codex items cannot overtake buffered text deltas.

    The web stream should receive the live text tail before the durable
    completed ``agentMessage`` item for the same turn.
    """
    _write_forwarder_bridge(tmp_path, active_turn_id="turn_123")
    posted: list[dict[str, Any]] = []

    async def run() -> None:
        """
        Replay buffered deltas followed by the completed assistant item.

        :returns: None.
        """
        async with _recording_forwarder_client(posted) as client:
            coalescer = codex_native_forwarder._OutputTextDeltaCoalescer(
                client,
                "conv_123",
                flush_interval_seconds=60.0,
                flush_char_threshold=1000,
            )
            for event in [
                _agent_message_delta_event("turn_123", "item_agent", "partial "),
                _agent_message_delta_event("turn_123", "item_agent", "tail"),
                _agent_message_event("turn_123", "item_agent", "complete response"),
            ]:
                await codex_native_forwarder._handle_event(
                    client,
                    **_forwarder_context(client, tmp_path),
                    event=event,
                    delta_coalescer=coalescer,
                )
            await coalescer.close()

    asyncio.run(run())

    assert [payload["type"] for payload in posted] == [
        "external_output_text_delta",
        "external_conversation_item",
    ]
    assert posted[0]["data"] == _expected_delta_data("partial tail", "turn_123", "item_agent")
    assert posted[1]["data"]["item_data"]["content"][0]["text"] == "complete response"


def _turn_diff_event(turn_id: str, diff: str, *, thread_id: str = "thread_123") -> dict[str, Any]:
    """
    Build a Codex ``turn/diff/updated`` notification.

    :param turn_id: Codex turn id, e.g. ``"turn_123"``.
    :param diff: Aggregated unified diff for the turn so far.
    :param thread_id: Codex thread id, e.g. ``"thread_123"``.
    :returns: App-server event payload.
    """
    return {
        "method": "turn/diff/updated",
        "params": {"threadId": thread_id, "turnId": turn_id, "diff": diff},
    }


def test_forwarder_coalesces_and_flushes_turn_diff(tmp_path: Path) -> None:
    """
    ``turn/diff/updated`` is coalesced and flushed once at turn end.

    Codex streams the aggregated working-tree diff repeatedly as edits land.
    Posting each update would spam the transcript with a growing diff, so the
    forwarder stores only the newest diff and mirrors it once, at the
    terminal boundary, as a single ``turn_diff`` function-call pair — after
    the turn's other items and before the idle status edge.
    """
    _write_forwarder_bridge(tmp_path, active_turn_id="turn_123")
    posted: list[dict[str, Any]] = []
    forwarder_state = codex_native_forwarder._CodexForwarderState()
    latest_diff = "--- a/x.py\n+++ b/x.py\n@@\n-old\n+new\n"

    async def run() -> None:
        """
        Replay two diff updates followed by the terminal boundary.

        :returns: None.
        """
        async with _recording_forwarder_client(posted) as client:
            for event in [
                _turn_diff_event("turn_123", "--- a/x.py\n+++ b/x.py\n@@\n-old\n"),
                _turn_diff_event("turn_123", latest_diff),
                _completed_event("turn_123", thread_id="thread_123"),
            ]:
                await codex_native_forwarder._handle_event(
                    client,
                    **_forwarder_context(client, tmp_path),
                    event=event,
                    forwarder_state=forwarder_state,
                )

    asyncio.run(run())

    # The two diff updates post nothing; only the terminal boundary flushes a
    # single turn_diff pair (carrying the LATEST diff), then the idle edge.
    assert posted == [
        {
            "type": "external_conversation_item",
            "data": {
                "item_type": "function_call",
                "item_data": {
                    "agent": "codex-native-ui",
                    "name": "turn_diff",
                    "arguments": "{}",
                    "call_id": "codex_turn_diff_turn_123",
                },
                "response_id": "codex_turn_123",
                "source_id": "thread_123:turn_123:turn-diff:call",
            },
        },
        {
            "type": "external_conversation_item",
            "data": {
                "item_type": "function_call_output",
                "item_data": {
                    "call_id": "codex_turn_diff_turn_123",
                    "output": latest_diff,
                },
                "response_id": "codex_turn_123",
                "source_id": "thread_123:turn_123:turn-diff:output",
            },
        },
        {
            "type": "external_session_status",
            "data": _expected_status_data("idle", "turn_123"),
        },
    ]
    # The stored diff is consumed on flush — no leak into the next turn.
    assert forwarder_state.turn_diff_by_turn == {}


def test_forwarder_skips_turn_diff_when_none_captured(tmp_path: Path) -> None:
    """
    A turn with no ``turn/diff/updated`` posts no turn_diff card.

    Read-only turns never receive a diff notification, so the terminal
    boundary must emit only the idle status edge — no empty diff artifact.
    """
    _write_forwarder_bridge(tmp_path, active_turn_id="turn_123")
    posted: list[dict[str, Any]] = []
    forwarder_state = codex_native_forwarder._CodexForwarderState()

    async def run() -> None:
        """
        Replay a terminal boundary with no preceding diff update.

        :returns: None.
        """
        async with _recording_forwarder_client(posted) as client:
            await codex_native_forwarder._handle_event(
                client,
                **_forwarder_context(client, tmp_path),
                event=_completed_event("turn_123", thread_id="thread_123"),
                forwarder_state=forwarder_state,
            )

    asyncio.run(run())

    assert posted == [
        {
            "type": "external_session_status",
            "data": _expected_status_data("idle", "turn_123"),
        },
    ]
