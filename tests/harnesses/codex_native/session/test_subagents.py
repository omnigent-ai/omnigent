"""Subagents tests for Codex session."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.harnesses.codex_native import forwarder as codex_native_forwarder
from omnigent.harnesses.codex_native.bridge import (
    CodexNativeBridgeState,
    write_bridge_state,
)
from tests.harnesses.codex_native.session._support import (
    _elicitation_tracker,
    _FakeCodexAppServerClient,
    _forwarder_context,
    _usage_coalescer,
)

# ── Codex subagent tracking and dedup ────────────────────────────────────────


def _collab_item_completed_event(
    *,
    parent_thread_id: str = "thread_parent",
    child_thread_id: str = "thread_child",
    item_id: str = "collab_1",
) -> dict[str, Any]:
    """
    Build a Codex ``collabAgentToolCall`` ``item/completed`` notification.

    :param parent_thread_id: Codex parent thread id.
    :param child_thread_id: Codex child thread id.
    :param item_id: Codex item id for the collab item.
    :returns: App-server event payload.
    """
    return {
        "method": "item/completed",
        "params": {
            "threadId": parent_thread_id,
            "turnId": "turn_parent",
            "item": {
                "type": "collabAgentToolCall",
                "id": item_id,
                "tool": "spawnAgent",
                "senderThreadId": parent_thread_id,
                "receiverThreadIds": [child_thread_id],
            },
        },
    }


def _collab_item_started_event(
    *,
    parent_thread_id: str = "thread_parent",
    child_thread_id: str = "thread_child",
    item_id: str = "collab_1",
) -> dict[str, Any]:
    """
    Build a Codex ``collabAgentToolCall`` ``item/started`` notification.

    :param parent_thread_id: Codex parent thread id.
    :param child_thread_id: Codex child thread id.
    :param item_id: Codex item id for the collab item.
    :returns: App-server event payload.
    """
    return {
        "method": "item/started",
        "params": {
            "threadId": parent_thread_id,
            "turnId": "turn_parent",
            "item": {
                "type": "collabAgentToolCall",
                "id": item_id,
                "tool": "spawnAgent",
                "senderThreadId": parent_thread_id,
                "receiverThreadIds": [child_thread_id],
            },
        },
    }


def _subagent_activity_started_event(
    *,
    parent_thread_id: str = "thread_parent",
    child_thread_id: str = "thread_child",
    item_id: str = "activity_1",
    method: str = "item/completed",
) -> dict[str, Any]:
    """Build the native Codex child-spawn notification."""
    return {
        "method": method,
        "params": {
            "threadId": parent_thread_id,
            "turnId": "turn_parent",
            "item": {
                "type": "subAgentActivity",
                "id": item_id,
                "kind": "started",
                "agentThreadId": child_thread_id,
                "agentPath": "root/researcher",
            },
        },
    }


def _child_agent_message_event(
    *,
    child_thread_id: str = "thread_child",
    turn_id: str = "turn_child",
    item_id: str = "child_msg",
    text: str = "child output",
) -> dict[str, Any]:
    """
    Build a Codex ``agentMessage`` notification from a child thread.

    :param child_thread_id: Codex child thread id.
    :param turn_id: Codex turn id.
    :param item_id: Codex item id.
    :param text: Assistant text content.
    :returns: App-server event payload.
    """
    return {
        "method": "item/completed",
        "params": {
            "threadId": child_thread_id,
            "turnId": turn_id,
            "item": {
                "type": "agentMessage",
                "id": item_id,
                "text": text,
            },
        },
    }


def _child_resume_response(
    *,
    child_thread_id: str = "thread_child",
    parent_thread_id: str = "thread_parent",
    turn_id: str = "turn_child",
    item_id: str = "child_msg",
    text: str = "child output",
    agent_nickname: str = "Euclid",
    agent_role: str = "explorer",
) -> dict[str, Any]:
    """
    Build a Codex ``thread/resume`` response for a child thread.

    :param child_thread_id: Codex child thread id.
    :param parent_thread_id: Codex parent thread id in the spawn source.
    :param turn_id: Turn id for the replayed item.
    :param item_id: Item id for the replayed item.
    :param text: Text content of the replayed item.
    :param agent_nickname: Codex-assigned agent nickname.
    :param agent_role: Codex-assigned agent role.
    :returns: JSON-RPC ``thread/resume`` response.
    """
    return {
        "result": {
            "thread": {
                "id": child_thread_id,
                "agentNickname": agent_nickname,
                "agentRole": agent_role,
                "source": {
                    "subAgent": {
                        "thread_spawn": {
                            "parent_thread_id": parent_thread_id,
                        }
                    }
                },
                "turns": [
                    {
                        "id": turn_id,
                        "items": [{"type": "agentMessage", "id": item_id, "text": text}],
                    }
                ],
            }
        }
    }


class _PerThreadFakeCodexClient(_FakeCodexAppServerClient):
    """
    Test double that returns per-thread ``thread/resume`` responses.

    The base ``_FakeCodexAppServerClient.request`` returns the same canned
    response for every call. This subclass dispatches by the requested
    ``threadId`` so tests can give child threads a different resume payload
    from the parent.

    :param thread_responses: Mapping from Codex thread id to JSON-RPC
        response payload, e.g.
        ``{"thread_child": {"result": {...}}}``.
    :param default_response: Fallback response for thread ids that have no
        explicit entry.
    """

    def __init__(
        self,
        thread_responses: dict[str, dict[str, Any]],
        default_response: dict[str, Any] | None = None,
    ) -> None:
        """
        Initialise with per-thread responses.

        :param thread_responses: Per-thread response map.
        :param default_response: Fallback response when the requested
            thread id is not in ``thread_responses``.
        :returns: None.
        """
        super().__init__(response=default_response or {"result": {"thread": None}})
        self.thread_responses = thread_responses

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """
        Dispatch to a per-thread response or the default.

        :param method: JSON-RPC method, e.g. ``"thread/resume"``.
        :param params: JSON-RPC params.
        :returns: Per-thread or default JSON-RPC response.
        """
        self.requests.append((method, params))
        thread_id = params.get("threadId")
        if isinstance(thread_id, str) and thread_id in self.thread_responses:
            return self.thread_responses[thread_id]
        return self.response


def _transcript_posts(
    posted: list[tuple[str, dict[str, Any]]],
    session_id: str,
) -> list[dict[str, Any]]:
    """
    Filter Omnigent posts to the ``external_conversation_item`` events for one session.

    :param posted: All captured Omnigent posts as ``(path, body)`` tuples.
    :param session_id: Omnigent session id to filter for.
    :returns: List of ``external_conversation_item`` body dicts.
    """
    return [
        body
        for path, body in posted
        if path == f"/v1/sessions/{session_id}/events"
        and body["type"] == "external_conversation_item"
    ]


def _registration_posts(
    posted: list[tuple[str, dict[str, Any]]],
    parent_session_id: str,
) -> list[dict[str, Any]]:
    """
    Filter Omnigent posts to the ``external_codex_subagent_start`` events for a parent.

    :param posted: All captured Omnigent posts as ``(path, body)`` tuples.
    :param parent_session_id: Parent Omnigent session id to filter for.
    :returns: List of ``external_codex_subagent_start`` body dicts.
    """
    return [
        body
        for path, body in posted
        if path == f"/v1/sessions/{parent_session_id}/events"
        and body["type"] == "external_codex_subagent_start"
    ]


def _make_omnigent_handler(
    posted: list[tuple[str, dict[str, Any]]],
    child_session_id: str = "conv_child",
) -> Callable[[httpx.Request], httpx.Response]:
    """
    Build an Omnigent ``MockTransport`` handler that registers a child session on demand.

    :param posted: Mutable list collecting all captured Omnigent requests.
    :param child_session_id: Omnigent child session id to return for
        ``external_codex_subagent_start`` events.
    :returns: Request handler for ``httpx.MockTransport``.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Capture the request and return the appropriate mock response.

        :param request: Incoming HTTP request.
        :returns: Mock Omnigent response.
        """
        body = json.loads(request.content)
        posted.append((request.url.path, body))
        if body.get("type") == "external_codex_subagent_start":
            return httpx.Response(
                202, json={"queued": False, "child_session_id": child_session_id}
            )
        return httpx.Response(202, json={"queued": False})

    return handler


def test_forwarder_dedupes_replay_and_live_child_item(
    tmp_path: Path,
) -> None:
    """
    A child transcript item written during backfill replay is not rewritten
    by the matching live ``item/completed`` event.

    This is the primary regression test for the duplicate-write bug. The
    forwarder discovers a child thread via a ``collabAgentToolCall`` and
    replays its backlog. Seconds later the same item arrives live on the
    event stream. With a correct total dedup key (``threadId:turnId:item.id``
    derived identically in both paths) the second delivery should be
    silently dropped. The test fails when either path produces a key of
    ``None`` or a key derived from different field values.

    The critical assertion:

    - Exactly **one** ``external_conversation_item`` to ``conv_child``
      proves dedup fired on the live delivery.
    - If it were two, the total-key derivation differs between paths or
      ``claim_item_key`` was bypassed.
    """
    posted: list[tuple[str, dict[str, Any]]] = []
    codex_client = _PerThreadFakeCodexClient(
        thread_responses={"thread_child": _child_resume_response()}
    )
    events = [
        # Parent turn — causes child registration + backfill replay.
        _collab_item_completed_event(),
        # Live delivery of the SAME child item already written during replay.
        _child_agent_message_event(text="child output"),
    ]
    codex_client.events = events

    async def run() -> None:
        """
        Run supervise_forwarder against the event sequence.

        :returns: None.
        """
        await codex_native_forwarder.supervise_forwarder(
            base_url="http://127.0.0.1:8000",
            headers={},
            session_id="conv_parent",
            bridge_dir=tmp_path,
            app_server_url=str(tmp_path / "app-server.sock"),
            thread_id="thread_parent",
            client=codex_client,  # type: ignore[arg-type]
            ap_transport=httpx.MockTransport(_make_omnigent_handler(posted)),
        )

    asyncio.run(run())

    child_posts = _transcript_posts(posted, "conv_child")
    # Exactly one item posted: the replay delivery.
    # Two would mean the live delivery was not deduped against the replay.
    assert len(child_posts) == 1, (
        f"Expected exactly 1 transcript post to conv_child (replay only); "
        f"got {len(child_posts)}. Duplicate write survived dedup."
    )
    assert child_posts[0]["data"]["item_data"]["content"][0]["text"] == "child output"


@pytest.mark.parametrize("activity_method", ["item/started", "item/completed"])
def test_forwarder_registers_subagent_activity_before_child_events(
    tmp_path: Path,
    activity_method: str,
) -> None:
    """A native spawn activity creates the child before its events arrive."""
    posted: list[tuple[str, dict[str, Any]]] = []
    codex_client = _PerThreadFakeCodexClient(
        thread_responses={"thread_child": _child_resume_response(text="backfilled child output")}
    )
    codex_client.events = [
        _subagent_activity_started_event(method=activity_method),
        _child_agent_message_event(item_id="child_live", text="live child output"),
    ]

    async def run() -> None:
        await codex_native_forwarder.supervise_forwarder(
            base_url="http://127.0.0.1:8000",
            headers={},
            session_id="conv_parent",
            bridge_dir=tmp_path,
            app_server_url=str(tmp_path / "app-server.sock"),
            thread_id="thread_parent",
            client=codex_client,  # type: ignore[arg-type]
            ap_transport=httpx.MockTransport(_make_omnigent_handler(posted)),
        )

    asyncio.run(run())

    registrations = _registration_posts(posted, "conv_parent")
    assert registrations, "subAgentActivity(kind=started) must create an Omnigent child session"
    assert registrations[0]["data"]["thread_id"] == "thread_child"
    child_posts = _transcript_posts(posted, "conv_child")
    assert [post["data"]["item_data"]["content"][0]["text"] for post in child_posts] == [
        "backfilled child output",
        "live child output",
    ]


def test_forwarder_does_not_double_write_stable_id_item_delivered_twice(
    tmp_path: Path,
) -> None:
    """
    A child item with a stable ``id`` posted twice in the same turn is
    written to Omnigent only once.

    This is the primary stable-id dedup case: the same ``item/completed``
    event may arrive once from the backfill replay and once live. The
    dedup key ``threadId:turnId:item.id`` must be identical both times so
    the second delivery is dropped by ``claim_item_key``.

    Anonymous items (no ``id``) cannot be reliably deduped across replay
    and live because there is no stable identity to key on. Only items with
    Codex-assigned ``id`` fields are guaranteed to deduplicate correctly.
    """
    posted: list[tuple[str, dict[str, Any]]] = []
    state = codex_native_forwarder._CodexForwarderState(
        parent_session_id="conv_parent",
    )
    state.note_child_thread("thread_child", "conv_child")

    stable_event = {
        "method": "item/completed",
        "params": {
            "threadId": "thread_child",
            "turnId": "turn_child",
            # Stable item id — dedup must key on this.
            "item": {"type": "agentMessage", "id": "msg_abc123", "text": "stable output"},
        },
    }

    async def run() -> None:
        """
        Deliver the same stable-id child event twice with a shared state.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(_make_omnigent_handler(posted)),
        ) as client:
            uc = codex_native_forwarder._SessionUsageCoalescer(client, "conv_parent")
            for _ in range(2):
                await codex_native_forwarder._handle_event(
                    client,
                    session_id="conv_parent",
                    bridge_dir=tmp_path,
                    event=stable_event,
                    usage_coalescer=uc,
                    expected_thread_id="thread_parent",
                    forwarder_state=state,
                    elicitation_tracker=_elicitation_tracker(),
                )

    asyncio.run(run())

    child_posts = _transcript_posts(posted, "conv_child")
    # Exactly one post: the stable id is claimed on first delivery and
    # claim_item_key returns False on the second, preventing a duplicate.
    assert len(child_posts) == 1, (
        f"Expected 1 transcript post for stable-id item; "
        f"got {len(child_posts)}. Stable-id items are not deduped correctly."
    )
    assert child_posts[0]["data"]["item_data"]["content"][0]["text"] == "stable output"


def test_forwarder_child_thread_started_does_not_rotate_parent_session(
    tmp_path: Path,
) -> None:
    """
    A ``thread/started`` event from a Codex AgentControl child does not
    rotate the parent Omnigent session.

    Native ``/clear`` starts a new top-level thread and must rotate.
    Child threads also emit ``thread/started`` when they begin — those
    events carry ``source.subAgent.thread_spawn`` and must be ignored
    by the rotation check, otherwise the parent's Omnigent session would be
    replaced every time a child starts.

    The test fails if ``_maybe_rotate_session_on_thread_started`` returns
    ``True`` for a child ``thread/started`` event.
    """
    codex_write_bridge_state = write_bridge_state  # noqa: F841 — alias for clarity.
    write_bridge_state(
        tmp_path,
        CodexNativeBridgeState(
            session_id="conv_parent",
            socket_path=str(tmp_path / "sock"),
            thread_id="thread_parent",
            codex_home=str(tmp_path / "home"),
        ),
    )

    child_thread_started_event: dict[str, Any] = {
        "method": "thread/started",
        "params": {
            "thread": {
                "id": "thread_child",
                "source": {"subAgent": {"thread_spawn": {"parent_thread_id": "thread_parent"}}},
            }
        },
    }
    ap_posts: list[tuple[str, dict[str, Any]]] = []

    async def run() -> bool:
        """
        Drive the child thread-started event through the rotation check.

        :returns: Whether a session rotation occurred.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(_make_omnigent_handler(ap_posts)),
        ) as ap_client:
            target = codex_native_forwarder._ForwarderTarget(
                session_id="conv_parent",
                thread_id="thread_parent",
                delta_coalescer=codex_native_forwarder._OutputTextDeltaCoalescer(
                    ap_client, "conv_parent"
                ),
                usage_coalescer=codex_native_forwarder._SessionUsageCoalescer(
                    ap_client, "conv_parent"
                ),
                elicitation_tracker=_elicitation_tracker(),
            )
            return await codex_native_forwarder._maybe_rotate_session_on_thread_started(
                ap_client=ap_client,
                target=target,
                bridge_dir=tmp_path,
                app_server_url=str(tmp_path / "sock"),
                event=child_thread_started_event,
            )

    rotated = asyncio.run(run())
    assert rotated is False, (
        "Expected child thread/started to NOT rotate the parent session; "
        "it rotated. _thread_started_is_subagent guard is missing or broken."
    )
    # No Omnigent calls should have been made during rotation detection.
    assert ap_posts == []


def test_forwarder_routes_live_child_items_to_child_session(
    tmp_path: Path,
) -> None:
    """
    Live ``item/completed`` events for a known child thread are routed
    to the child Omnigent session, not the parent.

    Once a child thread is registered in ``forwarder_state.subagents_by_thread``,
    the routing layer must direct any event carrying the child's ``threadId``
    to the child session. The test fails if live child items land on the
    parent session or are dropped silently.
    """
    posted: list[tuple[str, dict[str, Any]]] = []
    state = codex_native_forwarder._CodexForwarderState(
        parent_session_id="conv_parent",
    )
    state.note_child_thread("thread_child", "conv_child")

    live_child_event = _child_agent_message_event(text="live child message")

    async def run() -> None:
        """
        Deliver one live child event through _handle_event with a registered child.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(_make_omnigent_handler(posted)),
        ) as client:
            await codex_native_forwarder._handle_event(
                client,
                **_forwarder_context(client, tmp_path, session_id="conv_parent"),
                event=live_child_event,
                expected_thread_id="thread_parent",
                forwarder_state=state,
            )

    asyncio.run(run())

    child_posts = _transcript_posts(posted, "conv_child")
    parent_posts = _transcript_posts(posted, "conv_parent")

    assert len(child_posts) == 1, (
        f"Expected 1 transcript post to conv_child; got {len(child_posts)}. "
        "Live child items are not being routed to the child session."
    )
    assert child_posts[0]["data"]["item_data"]["content"][0]["text"] == "live child message"
    assert parent_posts == [], (
        f"Got {len(parent_posts)} transcript post(s) to conv_parent; "
        "child items must not route to the parent session."
    )


def test_forwarder_collab_item_started_registers_child_before_completed(
    tmp_path: Path,
) -> None:
    """
    ``item/started`` for a collab-agent spawn registers the child session
    so live child events can be routed immediately.

    Codex emits both ``item/started`` and ``item/completed`` for a
    ``collabAgentToolCall``. Registration at ``item/started`` lets the
    forwarder route child events that arrive before ``item/completed``.
    The test checks that after ``item/started`` is processed, the child
    thread is already in ``forwarder_state.subagents_by_thread``.
    """
    posted: list[tuple[str, dict[str, Any]]] = []
    state = codex_native_forwarder._CodexForwarderState(
        parent_session_id="conv_parent",
    )

    started_event = _collab_item_started_event()

    async def run() -> None:
        """
        Drive the collab item/started through _handle_event.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(_make_omnigent_handler(posted)),
        ) as client:
            await codex_native_forwarder._handle_event(
                client,
                **_forwarder_context(client, tmp_path, session_id="conv_parent"),
                event=started_event,
                expected_thread_id="thread_parent",
                forwarder_state=state,
            )

    asyncio.run(run())

    regs = _registration_posts(posted, "conv_parent")
    assert len(regs) == 1, (
        f"Expected 1 child registration after item/started; got {len(regs)}. "
        "collab-agent children must be registered at item/started."
    )
    assert state.session_for_child_thread("thread_child") == "conv_child", (
        "Expected thread_child to be registered in forwarder_state after "
        "item/started, but it was not found."
    )


def test_completed_item_key_is_total_never_empty() -> None:
    """
    ``_completed_item_key`` returns a non-empty string for every item shape.

    The dedup key must be total: if it returned ``None`` or an empty string
    for any item, that item would bypass the dedup gate and could be written
    twice. This test covers three shapes:
    - item with a stable ``id`` (normal case)
    - item missing ``id`` (positional fallback via ``peek_anon_item_key``)
    - item missing ``id`` *and* ``turnId`` (worst-case envelope)
    """
    state = codex_native_forwarder._CodexForwarderState()

    # Case 1: stable item id — the normal production path.
    key1, is_anon1 = codex_native_forwarder._completed_item_key(
        {"threadId": "t", "turnId": "u"},
        {"id": "item-abc", "type": "agentMessage"},
        state,
    )
    assert isinstance(key1, str) and key1, (
        f"Key for stable item id must be a non-empty string; got {key1!r}"
    )
    assert not is_anon1, "Stable-id item must not be flagged anonymous"

    # Case 2: no item id — must produce a positional fallback key.
    key2, is_anon2 = codex_native_forwarder._completed_item_key(
        {"threadId": "t", "turnId": "u"},
        {"type": "agentMessage"},  # no id
        state,
    )
    assert isinstance(key2, str) and key2, (
        f"Key for anonymous item must be a non-empty string; got {key2!r}"
    )
    assert is_anon2, "Anonymous item must be flagged is_anon=True"

    # Case 3: no item id AND no turnId — must still produce a non-empty string.
    key3, _ = codex_native_forwarder._completed_item_key(
        {"threadId": "t"},  # no turnId
        {"type": "agentMessage"},  # no id
        state,
    )
    assert isinstance(key3, str) and key3, (
        f"Key without turnId must be a non-empty string; got {key3!r}"
    )


def test_completed_item_key_two_anon_items_same_turn_distinct() -> None:
    """
    Two anonymous items in the same (thread, turn) get distinct dedup keys
    when the counter is advanced between them.

    The positional counter is peeked (not advanced) during key derivation.
    Only after ``advance_anon_counter`` does the next peek return a new slot.
    This test simulates the correct claim-and-advance cycle for two sequential
    anonymous items in the same turn.
    """
    state = codex_native_forwarder._CodexForwarderState()
    params = {"threadId": "t", "turnId": "u"}
    item = {"type": "agentMessage"}  # no id

    # First item: peek key, claim, advance.
    key_a, _ = codex_native_forwarder._completed_item_key(params, item, state)
    assert state.claim_item_key(key_a)
    state.advance_anon_counter("t", "u")

    # Second item: peek key after counter advanced — must be different.
    key_b, _ = codex_native_forwarder._completed_item_key(params, item, state)

    assert key_a != key_b, (
        f"Two sequential anonymous items must get distinct keys after advancing "
        f"the counter; both got {key_a!r}. advance_anon_counter is not working."
    )


def test_completed_item_key_claimed_anon_slot_rejected_on_reclaim() -> None:
    """
    Once an anonymous item key is claimed, a second claim for the same key
    returns ``False``.

    The anonymous counter is only advanced after a successful claim, so
    peeking before advancing gives the same slot. This tests the dedup gate
    directly: after claim+advance the slot anon-0 is in ``synced_item_keys``
    and a re-claim must be rejected.

    Note: anonymous-item dedup across replay vs live is only reliable when
    both paths see the counter at the same value — which holds when they both
    call ``_handle_completed_item`` sequentially on the same connection (the
    counter advances after each successful claim). The primary dedup mechanism
    for production items is the stable ``item.id`` path, not this fallback.
    """
    state = codex_native_forwarder._CodexForwarderState()

    # Simulate the first delivery: peek the key, claim it, advance the counter.
    key_a, _ = codex_native_forwarder._completed_item_key(
        {"threadId": "t", "turnId": "u"}, {"type": "agentMessage"}, state
    )
    assert state.claim_item_key(key_a)
    state.advance_anon_counter("t", "u")

    # anon-0 is now in synced_item_keys — a direct re-claim must be rejected.
    assert not state.claim_item_key("t:u:anon-0"), (
        "anon-0 must be rejected after the first claim; the slot is already in synced_item_keys."
    )


def test_forwarder_resolves_child_thread_elicitation_on_child_session(
    tmp_path: Path,
) -> None:
    """
    A child-thread elicitation resolves on the child session, not the parent.

    When a collab child thread raises an elicitation, the approval card is
    published on the child Omnigent session (``route_session_id``). Codex's
    ``serverRequest/resolved`` must clear it on that same child session;
    resolving on the parent leaves the child's card stuck for any web user
    watching the child.
    """
    fake_client = _FakeCodexAppServerClient()
    hook_started = asyncio.Event()
    posted_paths: list[str] = []
    state = codex_native_forwarder._CodexForwarderState(parent_session_id="conv_parent")
    state.note_child_thread("thread_child", "conv_child")
    request_event = {
        "id": 7,
        "method": "mcpServer/elicitation/request",
        "params": {
            "threadId": "thread_child",
            "turnId": "turn_child",
            "serverName": "booking",
            "mode": "form",
            "message": "Pick a date",
            "requestedSchema": {"type": "object", "properties": {}},
        },
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        """
        Hold the child hook open; record the session path other events post to.

        :param request: HTTP request sent by the forwarder.
        :returns: Omnigent event response for non-hook posts.
        """
        if request.url.path.endswith("/hooks/codex-elicitation-request"):
            hook_started.set()
            await asyncio.Future()  # pending approval — never resolves natively
        posted_paths.append(request.url.path)
        return httpx.Response(202, json={"queued": False})

    async def run() -> None:
        """
        Drive a child-thread elicitation request, then its resolution.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            elicitation_tracker = _elicitation_tracker()
            usage_coalescer = _usage_coalescer(client)
            # Routes to the child thread → the approval card is published on
            # conv_child (the hook POST blocks, simulating a pending prompt).
            await asyncio.wait_for(
                codex_native_forwarder._handle_event(
                    client,
                    session_id="conv_parent",
                    bridge_dir=tmp_path,
                    usage_coalescer=usage_coalescer,
                    elicitation_tracker=elicitation_tracker,
                    event=request_event,
                    codex_client=fake_client,  # type: ignore[arg-type]
                    forwarder_state=state,
                ),
                timeout=0.2,
            )
            await asyncio.wait_for(hook_started.wait(), timeout=1.0)
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_parent",
                bridge_dir=tmp_path,
                usage_coalescer=usage_coalescer,
                elicitation_tracker=elicitation_tracker,
                event={
                    "method": "serverRequest/resolved",
                    "params": {"threadId": "thread_child", "requestId": 7},
                },
                codex_client=fake_client,  # type: ignore[arg-type]
                forwarder_state=state,
            )
            await elicitation_tracker.close()

    asyncio.run(run())

    # The resolution must post to the CHILD session's events. With the old
    # session_id=session_id (parent), it posts to conv_parent and the child's
    # approval card never clears for a web user watching the child.
    assert "/v1/sessions/conv_child/events" in posted_paths, (
        f"resolution should target the child session; got {posted_paths}"
    )
    assert "/v1/sessions/conv_parent/events" not in posted_paths, (
        f"resolution must not target the parent session; got {posted_paths}"
    )
