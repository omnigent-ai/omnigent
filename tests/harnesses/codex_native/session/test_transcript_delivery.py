"""Transcript delivery tests for Codex session."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

import omnigent.inner.codex_native_executor as native_executor
from omnigent.harnesses.codex_native import forwarder as codex_native_forwarder
from omnigent.harnesses.codex_native.bridge import (
    read_accepted_steers,
    read_bridge_state,
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
    """Summarize posted messages and status edges, e.g. ``"user: hi"``."""
    summary = []
    for payload in posted:
        data = payload["data"]
        if payload["type"] == "external_conversation_item":
            item_data = data["item_data"]
            summary.append(f"{item_data['role']}: {item_data['content'][0]['text']}")
        elif payload["type"] == "external_session_status":
            summary.append(f"status: {data['status']}")
    return summary


async def _accept_web_steer(bridge_dir: Path, text: str) -> None:
    """Steer the active turn through the executor, as a web send does."""
    state = read_bridge_state(bridge_dir)
    assert state is not None
    steer_client = _FakeCodexAppServerClient(response={"result": {"turnId": "turn_123"}})
    with input_delivery_scope({"delivery_attempt_id": _STEER_CLIENT_ID}):
        await native_executor._steer_codex_turn(
            steer_client,  # type: ignore[arg-type]
            bridge_dir=bridge_dir,
            state=state,
            input_items=[{"type": "text", "text": text}],
        )
    assert [params["clientUserMessageId"] for _, params in steer_client.requests] == [
        _STEER_CLIENT_ID
    ]


def _run_steered_turn(
    bridge_dir: Path,
    *,
    live_steer_copy: bool,
    replies_after_steer: int,
    turn_end_read_error: Exception | None = None,
    next_turn_replies: int = 0,
    steer_in_transcript: bool = True,
) -> tuple[list[str], list[str]]:
    """
    Run one turn that a web send steers, returning posts and forwarder RPCs.

    :param bridge_dir: Native Codex bridge directory.
    :param live_steer_copy: Whether Codex's live ``userMessage`` for the steer arrives.
    :param replies_after_steer: Assistant messages Codex starts after the steer.
    :param turn_end_read_error: Error raised by the transcript read at the steered turn's end.
    :param next_turn_replies: Assistant messages in a following turn, if one runs.
    :param steer_in_transcript: Whether Codex's transcript holds the steer.
    :returns: Transcript summary and the forwarder's app-server methods.
    """
    _write_forwarder_bridge(bridge_dir, active_turn_id="turn_123")
    first = _native_user_message("item_first", "fix the bug", "client-first")
    steer = _native_user_message("item_steer", "also update the docs", _STEER_CLIENT_ID)
    next_prompt = _native_user_message("item_next", "run the tests", "client-next")
    turns = [
        {"id": "turn_123", "items": [first, steer] if steer_in_transcript else [first]},
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
            await _accept_web_steer(bridge_dir, "also update the docs")
            if live_steer_copy:
                await deliver(_turn_item_event("item/completed", steer))
            for index in range(1, replies_after_steer + 1):
                agent_item = {"type": "agentMessage", "id": f"msg_{index}"}
                await deliver(_turn_item_event("item/started", agent_item))
                await deliver(_agent_message_event("turn_123", f"msg_{index}", f"reply {index}"))
            fake_client.error = turn_end_read_error
            await deliver(_completed_event("turn_123", thread_id="thread_123"))
            fake_client.error = None
            if not next_turn_replies:
                return
            await deliver(_started_event("turn_456"))
            await deliver(_turn_item_event("item/completed", next_prompt, "turn_456"))
            for index in range(1, next_turn_replies + 1):
                agent_item = {"type": "agentMessage", "id": f"next_{index}"}
                await deliver(_turn_item_event("item/started", agent_item, "turn_456"))
                await deliver(_agent_message_event("turn_456", f"next_{index}", f"next {index}"))
            await deliver(_completed_event("turn_456", thread_id="thread_123"))

    asyncio.run(run())
    return _transcript(posted), [method for method, _ in fake_client.requests]


@pytest.mark.parametrize(
    ("replies_after_steer", "expected_transcript"),
    [
        (
            3,
            [
                "user: fix the bug",
                "assistant: reply 1",
                "user: also update the docs",
                "assistant: reply 2",
                "assistant: reply 3",
                "status: idle",
            ],
        ),
        (0, ["user: fix the bug", "user: also update the docs", "status: idle"]),
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
    """
    transcript, requests = _run_steered_turn(
        tmp_path, live_steer_copy=False, replies_after_steer=replies_after_steer
    )

    assert transcript == expected_transcript
    assert requests == ["thread/read"]
    assert read_accepted_steers(tmp_path, thread_id="thread_123") == []


@pytest.mark.parametrize(
    ("steer_in_transcript", "expected_steer"),
    [(True, ["user: also update the docs"]), (False, [])],
    ids=["recorded", "never_recorded"],
)
def test_forwarder_retries_steer_whose_turn_end_read_failed(
    tmp_path: Path, steer_in_transcript: bool, expected_steer: list[str]
) -> None:
    """
    A steer whose ended turn could not be read is retried on the next turn's read.

    The retry settles it, so later replies read nothing: a steer found is mirrored,
    and one absent from its ended turn was never consumed.
    """
    transcript, requests = _run_steered_turn(
        tmp_path,
        live_steer_copy=False,
        replies_after_steer=0,
        turn_end_read_error=ConnectionError("app-server closed"),
        next_turn_replies=4,
        steer_in_transcript=steer_in_transcript,
    )

    assert transcript == [
        "user: fix the bug",
        "status: idle",
        "status: running",
        "user: run the tests",
        "assistant: next 1",
        *expected_steer,
        "assistant: next 2",
        "assistant: next 3",
        "assistant: next 4",
        "status: idle",
    ]
    assert requests == ["thread/read", "thread/read"]
    assert read_accepted_steers(tmp_path, thread_id="thread_123") == []


def test_forwarder_does_not_read_transcript_for_steer_seen_live(tmp_path: Path) -> None:
    """A steer whose live ``userMessage`` arrived is posted once, without a transcript read."""
    transcript, requests = _run_steered_turn(tmp_path, live_steer_copy=True, replies_after_steer=3)

    assert transcript == [
        "user: fix the bug",
        "user: also update the docs",
        "assistant: reply 1",
        "assistant: reply 2",
        "assistant: reply 3",
        "status: idle",
    ]
    assert requests == []
    assert read_accepted_steers(tmp_path, thread_id="thread_123") == []


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
