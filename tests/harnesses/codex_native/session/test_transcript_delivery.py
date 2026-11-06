"""Transcript delivery tests for Codex session."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

import omnigent.inner.codex_native_executor as native_executor
from omnigent.harnesses.codex_native import forwarder as codex_native_forwarder
from omnigent.harnesses.codex_native.bridge import (
    SentSteer,
    read_bridge_state,
    read_sent_steers,
    record_sent_steer,
)
from omnigent.native.input_diagnostics import input_delivery_scope
from tests.harnesses.codex_native.session._support import (
    _agent_message_event,
    _completed_event,
    _FakeCodexAppServerClient,
    _forwarder_context,
    _recording_forwarder_client,
    _started_event,
    _write_forwarder_bridge,
)


def _failed_event(turn_id: str | None, *, thread_id: str | None = None) -> dict[str, Any]:
    """
    Build a Codex ``turn/failed`` notification.

    :param turn_id: Codex turn id, e.g. ``"turn_123"``, or ``None``
        when testing legacy or malformed terminal events.
    :param thread_id: Optional Codex thread id, e.g. ``"thread_123"``.
    :returns: App-server event payload.
    """
    params: dict[str, Any] = {}
    if turn_id is not None:
        params["turnId"] = turn_id
    if thread_id is not None:
        params["threadId"] = thread_id
    return {"method": "turn/failed", "params": params}


@pytest.mark.parametrize(
    ("initial_active_turn_id", "events", "expected_active_turn_id", "expected_statuses"),
    [
        (None, [_started_event("turn_old")], "turn_old", ["running"]),
        (
            None,
            [_started_event("turn_old"), _completed_event("turn_old")],
            None,
            ["running", "idle"],
        ),
        (
            None,
            [_started_event("turn_old"), _failed_event("turn_old")],
            None,
            ["running", "failed"],
        ),
        (
            None,
            [_completed_event("turn_early", thread_id="thread_123")],
            None,
            ["idle"],
        ),
        (
            None,
            [_failed_event("turn_early", thread_id="thread_123")],
            None,
            ["failed"],
        ),
        (
            None,
            [_completed_event("turn_early", thread_id="thread_other")],
            None,
            [],
        ),
        (
            None,
            [_started_event("turn_old"), _started_event("turn_new"), _completed_event("turn_old")],
            "turn_new",
            ["running", "running"],
        ),
        (
            None,
            [_started_event("turn_old"), _started_event("turn_new"), _failed_event("turn_old")],
            "turn_new",
            ["running", "running"],
        ),
        (
            None,
            [_started_event("turn_old"), _started_event("turn_new"), _completed_event("turn_new")],
            None,
            ["running", "running", "idle"],
        ),
        # A no-id terminal event while turn_old is live is ambiguous: it is
        # ignored, so the active turn stays and no premature idle is posted
        # (the bug that hid the "working" spinner mid-turn).
        (
            None,
            [_started_event("turn_old"), _completed_event(None)],
            "turn_old",
            ["running"],
        ),
        ("turn_new", [_completed_event("turn_old")], "turn_new", []),
        ("turn_new", [_failed_event("turn_old")], "turn_new", []),
        ("turn_new", [_completed_event("turn_new")], None, ["idle"]),
    ],
)
def test_forwarder_tracks_active_turn_across_terminal_event_sequences(
    initial_active_turn_id: str | None,
    events: list[dict[str, Any]],
    expected_active_turn_id: str | None,
    expected_statuses: list[str],
    tmp_path: Path,
) -> None:
    """
    Forwarder turn lifecycle handling is ordered by terminal turn id.

    Rapid web sends can update the active Codex turn before an older
    terminal notification arrives. Matching terminal notifications must
    mark the session idle or failed, while stale terminal notifications
    must leave the newer active turn and session status untouched.

    :param initial_active_turn_id: Active turn id before replaying the
        sequence, e.g. ``"turn_new"``.
    :param events: Codex app-server notifications to replay.
    :param expected_active_turn_id: Expected bridge state after the
        sequence.
    :param expected_statuses: Expected external session status posts.
    :param tmp_path: Temporary bridge directory.
    :returns: None.
    """
    _write_forwarder_bridge(tmp_path, active_turn_id=initial_active_turn_id)
    posted: list[dict[str, Any]] = []

    async def run() -> None:
        """
        Replay the Codex event sequence through the real handler.

        :returns: None.
        """
        async with _recording_forwarder_client(posted) as client:
            for event in events:
                await codex_native_forwarder._handle_event(
                    client,
                    **_forwarder_context(client, tmp_path),
                    event=event,
                )

    asyncio.run(run())

    state = read_bridge_state(tmp_path)
    assert state is not None
    assert state.active_turn_id == expected_active_turn_id
    assert [
        payload["data"]["status"]
        for payload in posted
        if payload["type"] == "external_session_status"
    ] == expected_statuses


def test_terminal_turn_clears_control_state_before_blocked_delta_flush(tmp_path: Path) -> None:
    """A slow output flush cannot leave a completed Codex turn steerable."""
    _write_forwarder_bridge(tmp_path, active_turn_id="turn_123")
    posted: list[dict[str, Any]] = []

    async def run() -> None:
        """Block output delivery and inspect bridge control state mid-boundary."""
        flush_entered = asyncio.Event()
        release_flush = asyncio.Event()

        class _BlockingDeltaCoalescer:
            """Delta coalescer that parks the terminal handler in ``flush``."""

            async def flush(self) -> None:
                """Signal entry and wait until the assertion releases delivery."""
                flush_entered.set()
                await release_flush.wait()

        async with _recording_forwarder_client(posted) as client:
            task = asyncio.create_task(
                codex_native_forwarder._handle_event(
                    client,
                    **_forwarder_context(client, tmp_path),
                    event=_completed_event("turn_123"),
                    delta_coalescer=_BlockingDeltaCoalescer(),  # type: ignore[arg-type]
                )
            )
            await asyncio.wait_for(flush_entered.wait(), timeout=5.0)

            state = read_bridge_state(tmp_path)
            assert state is not None
            assert state.active_turn_id is None
            assert posted == [], "terminal status must still follow pending output delivery"

            release_flush.set()
            await asyncio.wait_for(task, timeout=5.0)

    asyncio.run(run())

    assert [
        payload["data"]["status"]
        for payload in posted
        if payload["type"] == "external_session_status"
    ] == ["idle"]


def test_forwarder_posts_agent_item_after_stale_terminal_event(tmp_path: Path) -> None:
    """
    Stale turn completion does not block newer Codex response mirroring.

    This is the rapid-send failure shape: the web inject path has
    already advanced to ``turn_new`` when a delayed terminal event for
    ``turn_old`` arrives. The stale terminal event must not mark the
    session idle or prevent the newer assistant item from syncing.
    """
    _write_forwarder_bridge(tmp_path, active_turn_id=None)
    posted: list[dict[str, Any]] = []

    async def run() -> None:
        """
        Replay the stale-terminal/new-item sequence.

        :returns: None.
        """
        async with _recording_forwarder_client(posted) as client:
            for event in [
                _started_event("turn_old"),
                _started_event("turn_new"),
                _completed_event("turn_old"),
                _agent_message_event("turn_new", "item_agent", "new response"),
            ]:
                await codex_native_forwarder._handle_event(
                    client,
                    **_forwarder_context(client, tmp_path),
                    event=event,
                )

    asyncio.run(run())

    state = read_bridge_state(tmp_path)
    assert state is not None
    assert state.active_turn_id == "turn_new"
    assert [
        payload["data"]["status"]
        for payload in posted
        if payload["type"] == "external_session_status"
    ] == ["running", "running"]
    assert [
        payload["data"]["item_data"]["content"][0]["text"]
        for payload in posted
        if payload["type"] == "external_conversation_item"
    ] == ["new response"]


def test_forwarder_posts_user_message_on_assistant_item_started(tmp_path: Path) -> None:
    """
    The user message is recovered when the assistant STARTS, not finishes.

    On a fresh thread the live ``userMessage`` event can be missed. If
    recovery waited for the assistant's ``item/completed``, the assistant
    text deltas would already have streamed into a bubble rendered ABOVE
    the still-pending user bubble. Recovering at the assistant's
    ``item/started`` — which fires before any delta — commits the user
    message first, so the web UI renders the question above the reply.
    """
    posted: list[dict[str, Any]] = []

    resume_response = {
        "result": {
            "thread": {
                "id": "thread_123",
                "turns": [
                    {
                        "id": "turn_123",
                        "items": [
                            {
                                "type": "userMessage",
                                "id": "item-1",
                                "content": [{"type": "text", "text": "hello codex"}],
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

    async def run() -> None:
        """
        Deliver the assistant's ``item/started`` with the user missed live.

        :returns: None.
        """
        async with _recording_forwarder_client(posted) as client:
            await codex_native_forwarder._handle_event(
                client,
                **_forwarder_context(client, tmp_path),
                event={
                    "method": "item/started",
                    "params": {
                        "threadId": "thread_123",
                        "turnId": "turn_123",
                        "item": {"type": "agentMessage", "id": "msg_live"},
                    },
                },
                expected_thread_id="thread_123",
                forwarder_state=forwarder_state,
            )

    asyncio.run(run())

    items = [p for p in posted if p["type"] == "external_conversation_item"]
    # The user message is posted at assistant-start (before any delta),
    # so it lands first and the assistant streams below it.
    assert [p["data"]["item_data"]["role"] for p in items] == ["user"]
    assert items[0]["data"]["item_data"]["content"][0]["text"] == "hello codex"
    # The recovery issued exactly one resume to fetch the user message, and
    # the turn is now marked so the item/completed backstop won't re-post.
    assert [method for method, _ in fake_client.requests] == ["thread/resume"]
    assert forwarder_state.has_posted_user_message("turn_123")


def test_forwarder_posts_codex_user_and_agent_messages(tmp_path: Path) -> None:
    """
    Codex app-server completed message items are translated into
    external conversation items for the Omnigent session stream.
    """
    posted: list[dict[str, Any]] = []

    async def run() -> None:
        async with _recording_forwarder_client(posted) as client:
            await codex_native_forwarder._handle_event(
                client,
                **_forwarder_context(client, tmp_path),
                event={
                    "method": "item/completed",
                    "params": {
                        "threadId": "thread_123",
                        "turnId": "turn_123",
                        "item": {
                            "type": "userMessage",
                            "id": "item_user",
                            "content": [{"type": "text", "text": "hello codex"}],
                        },
                        "completedAtMs": 1,
                    },
                },
            )
            await codex_native_forwarder._handle_event(
                client,
                **_forwarder_context(client, tmp_path),
                event={
                    "method": "item/completed",
                    "params": {
                        "threadId": "thread_123",
                        "turnId": "turn_123",
                        "item": {
                            "type": "agentMessage",
                            "id": "item_agent",
                            "text": "hello from codex",
                            "phase": None,
                            "memoryCitation": None,
                        },
                        "completedAtMs": 2,
                    },
                },
            )

    asyncio.run(run())

    assert posted == [
        {
            "type": "external_conversation_item",
            "data": {
                "item_type": "message",
                "item_data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "hello codex"}],
                },
                "response_id": "codex_turn_123",
                "source_id": "thread_123:turn_123:item_user",
            },
        },
        {
            "type": "external_conversation_item",
            "data": {
                "item_type": "message",
                "item_data": {
                    "role": "assistant",
                    "agent": "codex-native-ui",
                    "content": [{"type": "output_text", "text": "hello from codex"}],
                },
                "response_id": "codex_turn_123",
                "message_id": "codex:thread_123:turn_123:agentMessage:item_agent",
                "source_id": "thread_123:turn_123:item_agent",
            },
        },
    ]


def test_forwarder_recovers_missed_user_message_before_assistant(tmp_path: Path) -> None:
    """
    A missed live ``userMessage`` is recovered before the assistant reply.

    On a fresh thread the forwarder can subscribe after ``turn/start``, so
    the early ``userMessage`` event streams past before the subscription
    lands and only the ``agentMessage`` arrives live. Without recovery the
    assistant reply would be posted first and the resume backfill would
    add the user message after it, inverting the web bubbles. The
    forwarder must resume to recover the turn's user message and post it
    BEFORE the reply so Omnigent assigns it the earlier position.
    """
    posted: list[dict[str, Any]] = []

    # Resume returns the full turn (user then assistant) with Codex's
    # positional resume ids; the recovery reads the userMessage from it.
    resume_response = {
        "result": {
            "thread": {
                "id": "thread_123",
                "turns": [
                    {
                        "id": "turn_123",
                        "items": [
                            {
                                "type": "userMessage",
                                "id": "item-1",
                                "content": [{"type": "text", "text": "hello codex"}],
                            },
                            {"type": "agentMessage", "id": "item-2", "text": "hello from codex"},
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

    async def run() -> None:
        """
        Deliver only the assistant message live (user message missed).

        :returns: None.
        """
        async with _recording_forwarder_client(posted) as client:
            await codex_native_forwarder._handle_event(
                client,
                **_forwarder_context(client, tmp_path),
                event=_agent_message_event("turn_123", "msg_live", "hello from codex"),
                expected_thread_id="thread_123",
                forwarder_state=forwarder_state,
            )

    asyncio.run(run())

    items = [p for p in posted if p["type"] == "external_conversation_item"]
    roles = [p["data"]["item_data"]["role"] for p in items]
    # User recovered and posted first, then the assistant reply.
    assert roles == ["user", "assistant"]
    assert items[0]["data"]["item_data"]["content"][0]["text"] == "hello codex"
    assert items[1]["data"]["item_data"]["content"][0]["text"] == "hello from codex"
    # The recovery issued exactly one resume to fetch the user message.
    assert [method for method, _ in fake_client.requests] == ["thread/resume"]


def test_forwarder_skips_user_recovery_when_user_seen_live(tmp_path: Path) -> None:
    """
    No recovery resume fires when the user message arrived live.

    On the happy path the live stream delivers ``userMessage`` before
    ``agentMessage``, so the forwarder must NOT issue a spurious
    ``thread/resume`` when the reply arrives — the turn is already known
    to have a posted user message.
    """
    posted: list[dict[str, Any]] = []

    fake_client = _FakeCodexAppServerClient()
    forwarder_state = codex_native_forwarder._CodexForwarderState(
        parent_session_id="conv_123",
        codex_client=fake_client,  # type: ignore[arg-type]
    )

    async def run() -> None:
        """
        Deliver the user message then the assistant message live.

        :returns: None.
        """
        async with _recording_forwarder_client(posted) as client:
            await codex_native_forwarder._handle_event(
                client,
                **_forwarder_context(client, tmp_path),
                event={
                    "method": "item/completed",
                    "params": {
                        "threadId": "thread_123",
                        "turnId": "turn_123",
                        "item": {
                            "type": "userMessage",
                            "id": "msg_user_live",
                            "content": [{"type": "text", "text": "hello codex"}],
                        },
                    },
                },
                expected_thread_id="thread_123",
                forwarder_state=forwarder_state,
            )
            await codex_native_forwarder._handle_event(
                client,
                **_forwarder_context(client, tmp_path),
                event=_agent_message_event("turn_123", "msg_live", "hello from codex"),
                expected_thread_id="thread_123",
                forwarder_state=forwarder_state,
            )

    asyncio.run(run())

    roles = [
        p["data"]["item_data"]["role"] for p in posted if p["type"] == "external_conversation_item"
    ]
    assert roles == ["user", "assistant"]
    # The live user message satisfied the ordering guarantee; no resume.
    assert fake_client.requests == []


_STEER_CLIENT_ID = "c" * 32
_STEER_STABLE_ID = "b" * 32
_FOLLOW_UP_CLIENT_ID = "d" * 32
_RECOVERED_STEER = (
    f"user: also update the docs [delivery {_STEER_CLIENT_ID}] [input {_STEER_STABLE_ID}]"
)
_TRANSCRIPT_READ = "codex: thread/read"


def _turn_item_event(
    method: str, item: dict[str, Any], turn_id: str = "turn_123"
) -> dict[str, Any]:
    """Build a ``thread_123`` item notification, e.g. ``item/started``."""
    return {
        "method": method,
        "params": {"threadId": "thread_123", "turnId": turn_id, "item": item},
    }


def _native_user_message(item_id: str, text: str, client_id: str) -> dict[str, Any]:
    """Build a Codex ``userMessage`` item sent with ``clientUserMessageId``."""
    return {
        "type": "userMessage",
        "id": item_id,
        "clientId": client_id,
        "content": [{"type": "text", "text": text, "text_elements": []}],
    }


def _transcript(posted: list[dict[str, Any]]) -> list[str]:
    """Summarize posted messages, status edges and app-server reads, e.g. ``"user: hi"``."""
    summary = []
    for payload in posted:
        data = payload["data"]
        if payload["type"] == "external_conversation_item":
            item_data = data["item_data"]
            line = f"{item_data['role']}: {item_data['content'][0]['text']}"
            if "delivery_attempt_id" in data:
                line += f" [delivery {data['delivery_attempt_id']}]"
            if "input_stable_id" in data:
                line += f" [input {data['input_stable_id']}]"
            summary.append(line)
        elif payload["type"] == "external_session_status":
            summary.append(f"status: {data['status']}")
        elif payload["type"] == "codex_request":
            summary.append(f"codex: {data['method']}")
    return summary


async def _accept_web_steer(
    bridge_dir: Path,
    text: str,
    before_reply: Callable[[], Awaitable[None]] | None = None,
    client_id: str = _STEER_CLIENT_ID,
    stable_id: str = _STEER_STABLE_ID,
) -> None:
    """Steer the active turn through the executor, as a web send does."""
    state = read_bridge_state(bridge_dir)
    assert state is not None
    steer_client = _FakeCodexAppServerClient(response={"result": {"turnId": "turn_123"}})
    if before_reply is not None:
        send_steer = steer_client.request

        async def request(method: str, params: dict[str, Any]) -> dict[str, Any]:
            reply = await send_steer(method, params)
            await before_reply()
            return reply

        steer_client.request = request  # type: ignore[method-assign]
    with input_delivery_scope({"delivery_attempt_id": client_id, "input_stable_id": stable_id}):
        await native_executor._steer_codex_turn(
            steer_client,  # type: ignore[arg-type]
            bridge_dir=bridge_dir,
            state=state,
            input_items=[{"type": "text", "text": text}],
        )
    assert [params["clientUserMessageId"] for _, params in steer_client.requests] == [client_id]


def _run_steered_turn(
    bridge_dir: Path,
    *,
    live_steer_copy: bool,
    replies_after_steer: int,
    turn_end_read_error: Exception | None = None,
    next_turn_replies: int = 0,
    steer_in_transcript: bool = True,
    late_steer_reply: bool = False,
    live_follow_up_steer: bool = False,
    during_read: Callable[[], Awaitable[None]] | None = None,
    live_next_prompt: bool = True,
    late_live_steer_id: str | None = None,
    lagging_first_read: bool = False,
) -> list[str]:
    """
    Run one turn that a web send steers, returning posts and forwarder reads in order.

    :param bridge_dir: Native Codex bridge directory.
    :param live_steer_copy: Whether Codex's live ``userMessage`` for the steer arrives.
    :param replies_after_steer: Assistant messages Codex starts after the steer.
    :param turn_end_read_error: Error raised by the transcript read at the steered turn's end.
    :param next_turn_replies: Assistant messages in a following turn, if one runs.
    :param steer_in_transcript: Whether Codex's transcript holds the steer.
    :param late_steer_reply: Whether the turn ends before the steer's reply arrives.
    :param live_follow_up_steer: Whether a second web steer, seen live, follows the first.
    :param during_read: Awaited while each transcript read is in flight.
    :param live_next_prompt: Whether the following turn's live ``userMessage`` arrives.
    :param late_live_steer_id: Item id of a live steer copy arriving after the replies.
    :param lagging_first_read: Whether the first transcript read predates the steers.
    :returns: Transcript summary, e.g. ``["user: fix the bug", "status: idle"]``.
    """
    _write_forwarder_bridge(bridge_dir, active_turn_id="turn_123")
    first = _native_user_message("item_first", "fix the bug", "client-first")
    steer = _native_user_message("item_steer", "also update the docs", _STEER_CLIENT_ID)
    follow_up = _native_user_message("item_follow_up", "and the changelog", _FOLLOW_UP_CLIENT_ID)
    next_prompt = _native_user_message("item_next", "run the tests", "client-next")
    steered_items = [first, steer] if steer_in_transcript else [first]
    if live_follow_up_steer:
        steered_items.append(follow_up)
    turns = [
        {"id": "turn_123", "items": steered_items},
        {"id": "turn_456", "items": [next_prompt]},
    ]
    fake_client = _FakeCodexAppServerClient(
        response={"result": {"thread": {"id": "thread_123", "turns": turns}}}
    )
    forwarder_state = codex_native_forwarder._CodexForwarderState(
        parent_session_id="conv_123",
        codex_client=fake_client,  # type: ignore[arg-type]
    )
    posted: list[dict[str, Any]] = []
    read_thread = fake_client.request
    lagging_turns = [{"id": "turn_123", "items": [first]}]
    requests_answered = 0

    async def request(method: str, params: dict[str, Any]) -> dict[str, Any]:
        nonlocal requests_answered
        posted.append({"type": "codex_request", "data": {"method": method}})
        if during_read is not None:
            await during_read()
        requests_answered += 1
        if lagging_first_read and requests_answered == 1:
            return {"result": {"thread": {"id": "thread_123", "turns": lagging_turns}}}
        return await read_thread(method, params)

    fake_client.request = request  # type: ignore[method-assign]

    async def run() -> None:
        async with _recording_forwarder_client(posted) as client:

            async def deliver(event: dict[str, Any]) -> None:
                await codex_native_forwarder._handle_event(
                    client,
                    **_forwarder_context(client, bridge_dir),
                    event=event,
                    expected_thread_id="thread_123",
                    forwarder_state=forwarder_state,
                )

            await deliver(_turn_item_event("item/completed", first))
            turn_end = _completed_event("turn_123", thread_id="thread_123")
            if late_steer_reply:
                await _accept_web_steer(
                    bridge_dir, "also update the docs", before_reply=lambda: deliver(turn_end)
                )
                return
            await _accept_web_steer(bridge_dir, "also update the docs")
            if live_steer_copy:
                await deliver(_turn_item_event("item/completed", steer))
            if live_follow_up_steer:
                await _accept_web_steer(
                    bridge_dir,
                    "and the changelog",
                    client_id=_FOLLOW_UP_CLIENT_ID,
                    stable_id="e" * 32,
                )
                await deliver(_turn_item_event("item/completed", follow_up))
            for index in range(1, replies_after_steer + 1):
                agent_item = {"type": "agentMessage", "id": f"msg_{index}"}
                await deliver(_turn_item_event("item/started", agent_item))
                await deliver(_agent_message_event("turn_123", f"msg_{index}", f"reply {index}"))
            if late_live_steer_id is not None:
                late_copy = {**steer, "id": late_live_steer_id}
                await deliver(_turn_item_event("item/completed", late_copy))
            fake_client.error = turn_end_read_error
            await deliver(turn_end)
            fake_client.error = None
            if not next_turn_replies:
                return
            await deliver(_started_event("turn_456"))
            if live_next_prompt:
                await deliver(_turn_item_event("item/completed", next_prompt, "turn_456"))
            for index in range(1, next_turn_replies + 1):
                agent_item = {"type": "agentMessage", "id": f"next_{index}"}
                await deliver(_turn_item_event("item/started", agent_item, "turn_456"))
                await deliver(_agent_message_event("turn_456", f"next_{index}", f"next {index}"))
            await deliver(_completed_event("turn_456", thread_id="thread_123"))

    asyncio.run(run())
    return _transcript(posted)


@pytest.mark.parametrize(
    ("replies_after_steer", "expected_transcript"),
    [
        (
            3,
            [
                "user: fix the bug",
                "assistant: reply 1",
                _TRANSCRIPT_READ,
                _RECOVERED_STEER,
                "assistant: reply 2",
                "assistant: reply 3",
                "status: idle",
            ],
        ),
        (
            1,
            [
                "user: fix the bug",
                "assistant: reply 1",
                _TRANSCRIPT_READ,
                _RECOVERED_STEER,
                "status: idle",
            ],
        ),
    ],
    ids=["later_model_step", "turn_end"],
)
def test_forwarder_mirrors_accepted_steer_whose_live_message_was_missed(
    tmp_path: Path, replies_after_steer: int, expected_transcript: list[str]
) -> None:
    """
    An accepted steer is mirrored even when its live ``userMessage`` never arrives.

    The server settles a steered web input only once its native message is
    mirrored. Codex records a steer at its next model step, so the forwarder
    reads the transcript at the second reply after the steer, or at turn end.
    The mirror names the delivery it carried, so the server settles only that input.
    """
    transcript = _run_steered_turn(
        tmp_path, live_steer_copy=False, replies_after_steer=replies_after_steer
    )

    assert transcript == expected_transcript
    assert read_sent_steers(tmp_path, thread_id="thread_123") == []


def test_forwarder_skips_late_live_copy_of_recovered_steer(tmp_path: Path) -> None:
    """A live copy of a recovered steer arriving later under another item id is not reposted."""
    transcript = _run_steered_turn(
        tmp_path, live_steer_copy=False, replies_after_steer=2, late_live_steer_id="item_late"
    )

    assert transcript == [
        "user: fix the bug",
        "assistant: reply 1",
        _TRANSCRIPT_READ,
        _RECOVERED_STEER,
        "assistant: reply 2",
        "status: idle",
    ]


def test_forwarder_mirrors_steer_whose_reply_arrives_after_turn_end(tmp_path: Path) -> None:
    """A steer Codex consumed is mirrored at turn end even before its RPC reply arrives."""
    transcript = _run_steered_turn(
        tmp_path, live_steer_copy=False, replies_after_steer=0, late_steer_reply=True
    )

    assert transcript == ["user: fix the bug", _TRANSCRIPT_READ, _RECOVERED_STEER, "status: idle"]
    assert read_sent_steers(tmp_path, thread_id="thread_123") == []


@pytest.mark.parametrize(
    ("lagging_first_read", "expected_steer"),
    [(False, [_RECOVERED_STEER]), (True, [])],
    ids=["read_holds_later_steer", "read_lags_later_steer"],
)
def test_forwarder_mirrors_missed_steer_ahead_of_later_steer(
    tmp_path: Path, lagging_first_read: bool, expected_steer: list[str]
) -> None:
    """
    A missed steer is mirrored before a later steer seen live, in transcript order.

    The server reports inputs queued ahead of a mirrored message as undelivered.
    A read predating the later steer cannot order the missed one, so it is dropped.
    """
    transcript = _run_steered_turn(
        tmp_path,
        live_steer_copy=False,
        replies_after_steer=0,
        live_follow_up_steer=True,
        lagging_first_read=lagging_first_read,
    )

    assert transcript == [
        "user: fix the bug",
        _TRANSCRIPT_READ,
        *expected_steer,
        "user: and the changelog",
        "status: idle",
    ]
    assert read_sent_steers(tmp_path, thread_id="thread_123") == []


def test_forwarder_drops_missed_steer_when_read_before_later_steer_fails(tmp_path: Path) -> None:
    """
    A missed steer is dropped, not mirrored late, once a later steer is mirrored ahead of it.

    A server ignoring the delivery a late copy names would settle it against another input.
    """
    reads_started = 0

    async def fail_first_read() -> None:
        nonlocal reads_started
        reads_started += 1
        if reads_started == 1:
            raise ConnectionError("app-server closed")

    transcript = _run_steered_turn(
        tmp_path,
        live_steer_copy=False,
        replies_after_steer=2,
        live_follow_up_steer=True,
        during_read=fail_first_read,
    )

    assert transcript == [
        "user: fix the bug",
        _TRANSCRIPT_READ,
        "user: and the changelog",
        "assistant: reply 1",
        "assistant: reply 2",
        "status: idle",
    ]
    assert read_sent_steers(tmp_path, thread_id="thread_123") == []


def test_forwarder_keeps_resumed_message_ahead_of_steer_a_read_cannot_place(
    tmp_path: Path,
) -> None:
    """
    A resumed message the transcript read cannot find keeps its place ahead of a missed steer.

    Resume replay names items by position, while the read keeps native ids.
    """
    _write_forwarder_bridge(tmp_path, active_turn_id="turn_123")
    record_sent_steer(
        tmp_path, thread_id="thread_123", turn_id="turn_123", client_message_id=_STEER_CLIENT_ID
    )
    typed_prompt = {
        "type": "userMessage",
        "id": "item_typed",
        "content": [{"type": "text", "text": "fix the bug", "text_elements": []}],
    }
    steer = _native_user_message("item_steer", "also update the docs", _STEER_CLIENT_ID)
    fake_client = _FakeCodexAppServerClient(
        response={
            "result": {
                "thread": {
                    "id": "thread_123",
                    "turns": [{"id": "turn_123", "items": [typed_prompt, steer]}],
                }
            }
        }
    )
    forwarder_state = codex_native_forwarder._CodexForwarderState(
        parent_session_id="conv_123",
        codex_client=fake_client,  # type: ignore[arg-type]
    )
    resumed_items = [{**typed_prompt, "id": "item-1"}, {**steer, "id": "item-2"}]
    posted: list[dict[str, Any]] = []

    async def run() -> None:
        async with _recording_forwarder_client(posted) as client:
            await codex_native_forwarder._replay_resume_response(
                client,
                **_forwarder_context(client, tmp_path),
                response={
                    "result": {
                        "thread": {
                            "id": "thread_123",
                            "turns": [{"id": "turn_123", "items": resumed_items}],
                        }
                    }
                },
                forwarder_state=forwarder_state,
            )

    asyncio.run(run())

    assert [method for method, _ in fake_client.requests] == ["thread/read"]
    assert _transcript(posted) == ["user: fix the bug", "user: also update the docs"]
    assert read_sent_steers(tmp_path, thread_id="thread_123") == []


def test_forwarder_backs_off_reads_for_steer_missing_from_transcript(tmp_path: Path) -> None:
    """
    A steer Codex never wrote is read for at backed-off replies, then dropped at turn end.

    Reads run at the second, fourth and eighth replies after the steer and at turn end.
    """
    transcript = _run_steered_turn(
        tmp_path, live_steer_copy=False, replies_after_steer=8, steer_in_transcript=False
    )

    replies = [f"assistant: reply {index}" for index in range(1, 9)]
    assert transcript == [
        "user: fix the bug",
        replies[0],
        _TRANSCRIPT_READ,
        *replies[1:3],
        _TRANSCRIPT_READ,
        *replies[3:7],
        _TRANSCRIPT_READ,
        replies[7],
        _TRANSCRIPT_READ,
        "status: idle",
    ]
    assert read_sent_steers(tmp_path, thread_id="thread_123") == []


def test_forwarder_keeps_steer_recorded_under_new_turn_during_read(tmp_path: Path) -> None:
    """A steer the executor re-records under a later turn while its ended turn is read stays."""

    async def record_under_next_turn() -> None:
        record_sent_steer(
            tmp_path,
            thread_id="thread_123",
            turn_id="turn_456",
            client_message_id=_STEER_CLIENT_ID,
        )

    transcript = _run_steered_turn(
        tmp_path,
        live_steer_copy=False,
        replies_after_steer=0,
        steer_in_transcript=False,
        during_read=record_under_next_turn,
    )

    assert transcript == ["user: fix the bug", _TRANSCRIPT_READ, "status: idle"]
    assert read_sent_steers(tmp_path, thread_id="thread_123") == [
        SentSteer("turn_456", _STEER_CLIENT_ID)
    ]


def test_forwarder_keeps_posting_while_steer_read_goes_unanswered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A transcript read Codex never answers gives up without holding back live posts.

    The steer stays recorded, and the turn-end read mirrors it.
    """
    monkeypatch.setattr(codex_native_forwarder, "_STEER_RECOVERY_READ_TIMEOUT_SECONDS", 0.01)
    reads_started = 0

    async def leave_first_read_unanswered() -> None:
        nonlocal reads_started
        reads_started += 1
        if reads_started == 1:
            await asyncio.Event().wait()

    transcript = _run_steered_turn(
        tmp_path,
        live_steer_copy=False,
        replies_after_steer=3,
        during_read=leave_first_read_unanswered,
    )

    assert transcript == [
        "user: fix the bug",
        "assistant: reply 1",
        _TRANSCRIPT_READ,
        "assistant: reply 2",
        "assistant: reply 3",
        _TRANSCRIPT_READ,
        _RECOVERED_STEER,
        "status: idle",
    ]
    assert read_sent_steers(tmp_path, thread_id="thread_123") == []


@pytest.mark.parametrize(
    ("steer_in_transcript", "expected_steer"),
    [(True, [_RECOVERED_STEER]), (False, [])],
    ids=["recorded", "never_recorded"],
)
def test_forwarder_retries_steer_whose_turn_end_read_failed(
    tmp_path: Path, steer_in_transcript: bool, expected_steer: list[str]
) -> None:
    """
    A steer whose ended turn could not be read is retried before the next user message.

    The retry settles it, so later replies read nothing: a steer found is mirrored
    ahead of that message, and one absent from its ended turn was never consumed.
    """
    transcript = _run_steered_turn(
        tmp_path,
        live_steer_copy=False,
        replies_after_steer=0,
        turn_end_read_error=ConnectionError("app-server closed"),
        next_turn_replies=4,
        steer_in_transcript=steer_in_transcript,
    )

    assert transcript == [
        "user: fix the bug",
        _TRANSCRIPT_READ,
        "status: idle",
        "status: running",
        _TRANSCRIPT_READ,
        *expected_steer,
        "user: run the tests",
        "assistant: next 1",
        "assistant: next 2",
        "assistant: next 3",
        "assistant: next 4",
        "status: idle",
    ]
    assert read_sent_steers(tmp_path, thread_id="thread_123") == []


def test_forwarder_drops_unread_steer_once_next_turn_message_is_mirrored(tmp_path: Path) -> None:
    """An ended turn's unread steer is dropped once the next turn's prompt is mirrored first."""
    transcript = _run_steered_turn(
        tmp_path,
        live_steer_copy=False,
        replies_after_steer=0,
        turn_end_read_error=ConnectionError("app-server closed"),
        next_turn_replies=2,
        live_next_prompt=False,
    )

    assert transcript == [
        "user: fix the bug",
        _TRANSCRIPT_READ,
        "status: idle",
        "status: running",
        "codex: thread/resume",
        "user: run the tests",
        "assistant: next 1",
        "assistant: next 2",
        "status: idle",
    ]
    assert read_sent_steers(tmp_path, thread_id="thread_123") == []


def test_forwarder_does_not_read_transcript_for_steer_seen_live(tmp_path: Path) -> None:
    """A steer whose live ``userMessage`` arrived is posted once, without a transcript read."""
    transcript = _run_steered_turn(tmp_path, live_steer_copy=True, replies_after_steer=3)

    assert transcript == [
        "user: fix the bug",
        "user: also update the docs",
        "assistant: reply 1",
        "assistant: reply 2",
        "assistant: reply 3",
        "status: idle",
    ]
    assert read_sent_steers(tmp_path, thread_id="thread_123") == []


def test_forwarder_marks_codex_skill_user_message_as_meta(tmp_path: Path) -> None:
    """
    Codex ``<skill>`` user messages are hidden durable context.

    Codex persists skill bodies as user messages wrapped in
    ``<skill>...</skill>``. The forwarder must preserve that message
    for Omnigent resume/history replay while tagging it ``is_meta`` so UI
    clients can hide it.
    """
    posted: list[dict[str, Any]] = []

    async def run() -> None:
        """
        Replay one normal and one Codex skill user-message item.

        :returns: None.
        """
        async with _recording_forwarder_client(posted) as client:
            await codex_native_forwarder._handle_event(
                client,
                **_forwarder_context(client, tmp_path),
                event={
                    "method": "item/completed",
                    "params": {
                        "threadId": "thread_123",
                        "turnId": "turn_normal",
                        "item": {
                            "type": "userMessage",
                            "id": "item_user_normal",
                            "content": [{"type": "text", "text": "hello"}],
                        },
                    },
                },
            )
            await codex_native_forwarder._handle_event(
                client,
                **_forwarder_context(client, tmp_path),
                event={
                    "method": "item/completed",
                    "params": {
                        "threadId": "thread_123",
                        "turnId": "turn_skill",
                        "item": {
                            "type": "userMessage",
                            "id": "item_user_skill",
                            "content": [
                                {
                                    "type": "text",
                                    "text": (
                                        "<skill>\n<name>grill-me</name>\nAsk questions.\n</skill>"
                                    ),
                                }
                            ],
                        },
                    },
                },
            )

    asyncio.run(run())

    assert len(posted) == 2
    normal_data = posted[0]["data"]["item_data"]
    skill_data = posted[1]["data"]["item_data"]
    assert "is_meta" not in normal_data
    assert skill_data["is_meta"] is True
    assert skill_data["content"][0]["text"].startswith("<skill>")
