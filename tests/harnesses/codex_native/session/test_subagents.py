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
    read_bridge_state,
    write_bridge_state,
)
from tests.harnesses.codex_native.forwarder._support import (
    _posted_cumulative_tokens,
    _usage_event,
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
    agents_states: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Build a Codex ``collabAgentToolCall`` ``item/completed`` notification.

    :param parent_thread_id: Codex parent thread id.
    :param child_thread_id: Codex child thread id.
    :param item_id: Codex item id for the collab item.
    :param agents_states: Optional ``agentsStates`` snapshot keyed by child
        thread id, e.g. ``{"thread_child": {"status": "running"}}``.
    :returns: App-server event payload.
    """
    item: dict[str, Any] = {
        "type": "collabAgentToolCall",
        "id": item_id,
        "tool": "spawnAgent",
        "senderThreadId": parent_thread_id,
        "receiverThreadIds": [child_thread_id],
    }
    if agents_states is not None:
        item["agentsStates"] = agents_states
    return {
        "method": "item/completed",
        "params": {
            "threadId": parent_thread_id,
            "turnId": "turn_parent",
            "item": item,
        },
    }


def _child_turn_event(
    method: str,
    *,
    child_thread_id: str = "thread_child",
    turn_id: str = "turn_child",
) -> dict[str, Any]:
    """
    Build a child thread's own ``turn/started``, ``turn/completed`` or
    ``turn/failed`` notification.

    :param method: ``"turn/started"``, ``"turn/completed"`` or ``"turn/failed"``.
    :param child_thread_id: Codex child thread id.
    :param turn_id: Codex turn id on the child thread.
    :returns: App-server event payload.
    """
    turn: dict[str, Any] = {"id": turn_id}
    if method == "turn/completed":
        turn.update({"status": "completed", "items": []})
    elif method == "turn/failed":
        turn["status"] = "failed"
    else:
        turn["status"] = "inProgress"
    return {"method": method, "params": {"threadId": child_thread_id, "turn": turn}}


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
    turn_status: str | None = None,
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
    :param turn_status: Optional Codex status of the replayed turn, e.g.
        ``"completed"`` or ``"inProgress"``. Omitted when ``None``.
    :returns: JSON-RPC ``thread/resume`` response.
    """
    turn: dict[str, Any] = {
        "id": turn_id,
        "items": [{"type": "agentMessage", "id": item_id, "text": text}],
    }
    if turn_status is not None:
        turn["status"] = turn_status
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
                "turns": [turn],
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A native spawn activity creates the child before its events arrive."""
    workspace = tmp_path / "read-only-workspace"
    workspace.mkdir(mode=0o555)
    monkeypatch.chdir(workspace)
    parent_state = CodexNativeBridgeState(
        session_id="conv_parent",
        socket_path=str(tmp_path / "app-server.sock"),
        thread_id="thread_parent",
        codex_home=str(tmp_path / "codex-home"),
        active_turn_id="turn_parent",
    )
    write_bridge_state(tmp_path, parent_state)
    posted: list[tuple[str, dict[str, Any]]] = []
    codex_client = _PerThreadFakeCodexClient(
        thread_responses={"thread_child": _child_resume_response(text="backfilled child output")}
    )
    codex_client.events = [
        _subagent_activity_started_event(method=activity_method),
        {
            "method": "turn/started",
            "params": {"threadId": "thread_child", "turn": {"id": "turn_child"}},
        },
        _child_agent_message_event(item_id="child_live", text="live child output"),
        {
            "method": "turn/completed",
            "params": {"threadId": "thread_child", "turn": {"id": "turn_child"}},
        },
        {
            "method": "turn/started",
            "params": {"threadId": "thread_child", "turn": {"id": "turn_followup"}},
        },
        {
            "method": "turn/failed",
            "params": {"threadId": "thread_child", "turn": {"id": "turn_followup"}},
        },
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
    statuses = [
        body["data"]["status"]
        for path, body in posted
        if path == "/v1/sessions/conv_child/events"
        and body.get("type") == "external_session_status"
    ]
    assert statuses == ["running", "idle", "running", "failed"]
    assert read_bridge_state(tmp_path) == parent_state
    assert list(workspace.iterdir()) == []


def _child_turn_events(
    *, child_thread_id: str = "thread_child", turn_id: str = "turn_after", text: str
) -> list[dict[str, Any]]:
    """Build one child turn: its start, one reply, and its completion."""
    return [
        {
            "method": "turn/started",
            "params": {"threadId": child_thread_id, "turn": {"id": turn_id}},
        },
        _child_agent_message_event(
            child_thread_id=child_thread_id, turn_id=turn_id, item_id=f"{turn_id}_msg", text=text
        ),
        {
            "method": "turn/completed",
            "params": {"threadId": child_thread_id, "turn": {"id": turn_id}},
        },
    ]


def _run_forwarder(
    tmp_path: Path,
    codex_client: _FakeCodexAppServerClient,
    posted: list[tuple[str, dict[str, Any]]],
) -> None:
    """Run ``supervise_forwarder`` for ``thread_parent`` over the client's events."""

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


def _child_statuses(posted: list[tuple[str, dict[str, Any]]]) -> list[str]:
    """Return the session statuses posted to ``conv_child``, in order."""
    return [
        body["data"]["status"]
        for path, body in posted
        if path == "/v1/sessions/conv_child/events"
        and body.get("type") == "external_session_status"
    ]


def test_forwarder_reattaches_subagent_spawned_before_a_restart(tmp_path: Path) -> None:
    """
    A relaunched forwarder routes an earlier sub-agent's new work to its session.

    Nothing maps a sub-agent spawned before a runner relaunch, and the parent
    sending it new work emits no spawn item. Codex's spawn metadata links the
    thread back to the parent, so the child keeps its session and its own
    model, and the history an earlier forwarder mirrored is not replayed.
    """
    posted: list[tuple[str, dict[str, Any]]] = []
    child_read = _child_resume_response(text="mirrored before")
    child_read["result"]["thread"]["model"] = "gpt-child"
    codex_client = _PerThreadFakeCodexClient(thread_responses={"thread_child": child_read})
    codex_client.events = [
        *_child_turn_events(text="after restart"),
        *_child_turn_events(turn_id="turn_again", text="again"),
    ]

    _run_forwarder(tmp_path, codex_client, posted)

    registrations = _registration_posts(posted, "conv_parent")
    assert [registration["data"] for registration in registrations] == [
        {
            "thread_id": "thread_child",
            "agent_nickname": "Euclid",
            "agent_role": "explorer",
            "parent_thread_id": "thread_parent",
        }
    ]
    assert [
        post["data"]["item_data"]["content"][0]["text"]
        for post in _transcript_posts(posted, "conv_child")
    ] == ["after restart", "again"]
    assert _child_statuses(posted) == ["running", "idle", "running", "idle"]
    assert [
        body["data"]["model"]
        for path, body in posted
        if path == "/v1/sessions/conv_child/events" and body["type"] == "external_model_change"
    ] == ["gpt-child"]
    assert [
        request
        for request in codex_client.requests
        if request == ("thread/read", {"threadId": "thread_child", "includeTurns": False})
    ] == [("thread/read", {"threadId": "thread_child", "includeTurns": False})]


def test_forwarder_reattaches_a_nested_subagent_through_its_spawn_chain(
    tmp_path: Path,
) -> None:
    """A sub-agent of an unregistered sub-agent is linked to the parent through it."""
    posted: list[tuple[str, dict[str, Any]]] = []
    codex_client = _PerThreadFakeCodexClient(
        thread_responses={
            "thread_grandchild": _child_resume_response(
                child_thread_id="thread_grandchild", parent_thread_id="thread_child"
            ),
            "thread_child": _child_resume_response(),
        }
    )
    codex_client.events = _child_turn_events(child_thread_id="thread_grandchild", text="nested")

    _run_forwarder(tmp_path, codex_client, posted)

    registrations = _registration_posts(posted, "conv_parent")
    assert [registration["data"]["thread_id"] for registration in registrations] == [
        "thread_grandchild"
    ]
    assert registrations[0]["data"]["parent_thread_id"] == "thread_child"
    assert [
        post["data"]["item_data"]["content"][0]["text"]
        for post in _transcript_posts(posted, "conv_child")
    ] == ["nested"]


def test_forwarder_keeps_ignoring_threads_outside_its_spawn_tree(tmp_path: Path) -> None:
    """A thread another thread spawned stays ignored, and is looked up only once."""
    posted: list[tuple[str, dict[str, Any]]] = []
    codex_client = _PerThreadFakeCodexClient(
        thread_responses={
            "thread_other": _child_resume_response(
                child_thread_id="thread_other", parent_thread_id="thread_elsewhere"
            ),
            "thread_elsewhere": {"result": {"thread": {"id": "thread_elsewhere"}}},
        }
    )
    codex_client.events = [
        *_child_turn_events(child_thread_id="thread_other", text="not ours"),
        *_child_turn_events(
            child_thread_id="thread_other", turn_id="turn_again", text="still not"
        ),
    ]

    _run_forwarder(tmp_path, codex_client, posted)

    assert _registration_posts(posted, "conv_parent") == []
    assert _transcript_posts(posted, "conv_child") == []
    assert [
        request
        for request in codex_client.requests
        if request[1].get("threadId") in {"thread_other", "thread_elsewhere"}
    ] == [
        ("thread/read", {"threadId": "thread_other", "includeTurns": False}),
        ("thread/read", {"threadId": "thread_elsewhere", "includeTurns": False}),
    ]


def test_forwarder_still_backfills_a_spawn_whose_events_precede_its_spawn_item(
    tmp_path: Path,
) -> None:
    """
    A sub-agent announced on this connection is routed early but keeps its backfill.

    Its first events can arrive before the parent's spawn item names it. They
    are delivered live, and the spawn item still replays the history the live
    stream missed.
    """
    posted: list[tuple[str, dict[str, Any]]] = []
    codex_client = _PerThreadFakeCodexClient(
        thread_responses={"thread_child": _child_resume_response(text="backfilled")}
    )
    codex_client.events = [
        {
            "method": "thread/started",
            "params": {
                "thread": {
                    "id": "thread_child",
                    "source": {
                        "subAgent": {"thread_spawn": {"parent_thread_id": "thread_parent"}}
                    },
                }
            },
        },
        *_child_turn_events(text="live"),
        _collab_item_completed_event(),
    ]

    _run_forwarder(tmp_path, codex_client, posted)

    # The live lane and the inline backfill post concurrently.
    assert sorted(
        post["data"]["item_data"]["content"][0]["text"]
        for post in _transcript_posts(posted, "conv_child")
    ) == ["backfilled", "live"]
    assert ("thread/resume", {"threadId": "thread_child"}) in codex_client.requests


def test_forwarder_child_usage_excludes_inherited_totals_across_restart(
    tmp_path: Path,
) -> None:
    """A forked child posts only its own usage, including after a forwarder restart.

    Codex 0.149 seeds a forked child with the parent's totals and repeats them in
    the child's later frames. A restarted forwarder never sees the seed, so it
    must reuse the inherited totals recorded in the bridge directory.
    """
    posted: list[tuple[str, dict[str, Any]]] = []

    async def run(events: list[dict[str, Any]]) -> None:
        codex_client = _PerThreadFakeCodexClient(
            thread_responses={"thread_child": _child_resume_response()}
        )
        codex_client.events = events
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

    asyncio.run(
        run(
            [
                _usage_event("thread_parent", (2500, 1000, 110), (1500, 1000, 10)),
                _collab_item_started_event(),
                _usage_event(
                    "thread_child", (2500, 1000, 110), (1500, 1000, 10), "turn_thread_parent"
                ),
                _usage_event("thread_parent", (4500, 2000, 160), (2000, 1000, 50)),
                _usage_event("thread_child", (2800, 1000, 140), (300, 0, 30)),
            ]
        )
    )
    # The restarted forwarder re-registers the child, as an interrupted-turn
    # replay does, and the resumed child reports its full Codex totals again.
    asyncio.run(
        run(
            [
                _usage_event("thread_parent", (4500, 2000, 160), (2000, 1000, 50)),
                _collab_item_completed_event(),
                _usage_event("thread_child", (2800, 1000, 140), (300, 0, 30)),
                _usage_event("thread_child", (3500, 1300, 180), (700, 300, 40)),
            ]
        )
    )

    assert _posted_cumulative_tokens(posted, "conv_child") == [
        (0, 0, 0),
        (300, 0, 30),
        (300, 0, 30),
        (1000, 300, 70),
    ]
    assert _posted_cumulative_tokens(posted, "conv_parent") == [
        (2500, 1000, 110),
        (4500, 2000, 160),
        (4500, 2000, 160),
    ]


@pytest.mark.parametrize(
    ("started_model", "requested_model", "resumed_model", "resumed_thread_model", "child_model"),
    [
        pytest.param("gpt-child", None, None, None, "gpt-child", id="thread-started"),
        pytest.param(None, "gpt-child", None, None, "gpt-child", id="spawn-request"),
        pytest.param(None, None, "gpt-child", None, "gpt-child", id="resume"),
        pytest.param(None, None, None, "gpt-child", "gpt-child", id="resume-thread"),
        pytest.param(
            "gpt-child", "gpt-requested", None, None, "gpt-child", id="report-beats-request"
        ),
        pytest.param(
            None, "gpt-requested", "gpt-child", None, "gpt-child", id="snapshot-beats-request"
        ),
        pytest.param(
            "gpt-child", None, "gpt-snapshot", None, "gpt-child", id="report-beats-snapshot"
        ),
        pytest.param(None, None, None, None, None, id="inherits-parent"),
    ],
)
def test_forwarder_reports_and_prices_child_with_its_own_model(
    tmp_path: Path,
    started_model: str | None,
    requested_model: str | None,
    resumed_model: str | None,
    resumed_thread_model: str | None,
    child_model: str | None,
) -> None:
    """A sub-agent running another model reports and prices it on its own session.

    A child without a model of its own keeps the parent's for pricing and
    reports none, rather than claiming the parent's model as its own.
    """
    posted: list[tuple[str, dict[str, Any]]] = []
    child_resume = _child_resume_response()
    if resumed_model is not None:
        child_resume["result"]["model"] = resumed_model
    if resumed_thread_model is not None:
        child_resume["result"]["thread"]["model"] = resumed_thread_model
    codex_client = _PerThreadFakeCodexClient(
        thread_responses={
            "thread_parent": {
                "result": {"model": "gpt-parent", "thread": {"id": "thread_parent", "turns": []}}
            },
            "thread_child": child_resume,
        }
    )
    child_thread: dict[str, Any] = {
        "id": "thread_child",
        "source": {"subAgent": {"thread_spawn": {"parent_thread_id": "thread_parent"}}},
    }
    if started_model is not None:
        child_thread["model"] = started_model
    spawn = _collab_item_completed_event()
    if requested_model is not None:
        spawn["params"]["item"]["model"] = requested_model
    codex_client.events = [
        {"method": "thread/started", "params": {"thread": child_thread}},
        spawn,
        _usage_event("thread_child", (300, 0, 30), (300, 0, 30)),
    ]

    _run_forwarder(tmp_path, codex_client, posted)

    def reported_models(session_id: str) -> list[str]:
        return [
            body["data"]["model"]
            for path, body in posted
            if path == f"/v1/sessions/{session_id}/events"
            and body["type"] == "external_model_change"
        ]

    assert reported_models("conv_parent") == ["gpt-parent"]
    assert reported_models("conv_child") == ([child_model] if child_model else [])
    assert [
        body["data"].get("model")
        for path, body in posted
        if path == "/v1/sessions/conv_child/events" and body["type"] == "external_session_usage"
    ] == [child_model or "gpt-parent"]


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


# ── Parent collab snapshots must not revive a settled child ──────────────────


def _snapshot_test_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    codex_client: _FakeCodexAppServerClient | None = None,
) -> tuple[codex_native_forwarder._CodexForwarderState, Path]:
    """Child resume replay reads bridge state from cwd, so use a scratch directory."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    state = codex_native_forwarder._CodexForwarderState(
        parent_session_id="conv_parent",
        codex_client=codex_client,  # type: ignore[arg-type]
    )
    return state, bridge_dir


def _deliver_parent_events(
    state: codex_native_forwarder._CodexForwarderState,
    bridge_dir: Path,
    events: list[dict[str, Any]],
    posted: list[tuple[str, dict[str, Any]]],
) -> None:
    """
    Deliver events through ``_handle_event`` as the parent thread's forwarder.

    :param state: Forwarder state shared across the events.
    :param bridge_dir: Parent bridge directory.
    :param events: App-server notifications in delivery order.
    :param posted: List collecting every Omnigent post as ``(path, body)``.
    :returns: None.
    """

    async def run() -> None:
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(_make_omnigent_handler(posted)),
        ) as client:
            try:
                for event in events:
                    await codex_native_forwarder._handle_event(
                        client,
                        **_forwarder_context(client, bridge_dir, session_id="conv_parent"),
                        event=event,
                        expected_thread_id="thread_parent",
                        forwarder_state=state,
                    )
                    await state.settle_child_lanes()
            finally:
                await state.close_child_streams()

    asyncio.run(run())


def _running_snapshot_event() -> dict[str, Any]:
    """Build a parent spawn item whose snapshot still lists the child as running."""
    return _collab_item_completed_event(agents_states={"thread_child": {"status": "running"}})


def _completed_snapshot_event() -> dict[str, Any]:
    """Build a parent spawn item whose snapshot reports the child as completed."""
    return _collab_item_completed_event(agents_states={"thread_child": {"status": "completed"}})


def _child_error_event(
    *,
    child_thread_id: str = "thread_child",
    turn_id: str = "turn_child",
) -> dict[str, Any]:
    """Build a child thread's standalone terminal ``error`` notification."""
    return {
        "method": "error",
        "params": {
            "threadId": child_thread_id,
            "turnId": turn_id,
            "error": {"message": "child crashed"},
        },
    }


class _SuspendingResumeClient(_PerThreadFakeCodexClient):
    """Resume client that parks ``thread/resume`` until the test releases it."""

    def __init__(self, thread_responses: dict[str, dict[str, Any]]) -> None:
        super().__init__(thread_responses)
        self.resume_started = asyncio.Event()
        self.release_resume = asyncio.Event()

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "thread/resume":
            self.resume_started.set()
            await self.release_resume.wait()
        return await super().request(method, params)


def _child_resume_response_with_turns(statuses: list[str]) -> dict[str, Any]:
    """
    Build a child ``thread/resume`` response whose turns carry ``statuses`` in order.

    :param statuses: Non-empty list of Codex turn statuses, oldest first.
    :returns: JSON-RPC ``thread/resume`` response.
    """
    response = _child_resume_response(turn_status=statuses[0])
    turns = response["result"]["thread"]["turns"]
    for index, status in enumerate(statuses[1:], start=2):
        turns.append({"id": f"turn_child_{index}", "status": status, "items": []})
    return response


@pytest.mark.parametrize(
    ("terminal_event", "settled_status"),
    [
        (_child_turn_event("turn/completed"), "idle"),
        (_child_turn_event("turn/failed"), "failed"),
        (_child_error_event(), "failed"),
    ],
    ids=["turn-completed", "turn-failed", "error-notification"],
)
def test_forwarder_collab_snapshot_does_not_revive_settled_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    terminal_event: dict[str, Any],
    settled_status: str,
) -> None:
    """
    A child that finished or failed keeps that status when a later collab snapshot
    reports it running.
    """
    posted: list[tuple[str, dict[str, Any]]] = []
    state, bridge_dir = _snapshot_test_state(tmp_path, monkeypatch)

    _deliver_parent_events(
        state,
        bridge_dir,
        [
            _subagent_activity_started_event(),
            _child_turn_event("turn/started"),
            terminal_event,
            _running_snapshot_event(),
        ],
        posted,
    )

    assert _child_statuses(posted) == ["running", settled_status]


def test_forwarder_collab_snapshot_posts_status_for_child_without_own_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A freshly registered child with no lifecycle of its own still takes the snapshot status.
    """
    posted: list[tuple[str, dict[str, Any]]] = []
    state, bridge_dir = _snapshot_test_state(tmp_path, monkeypatch)

    _deliver_parent_events(
        state,
        bridge_dir,
        [_subagent_activity_started_event(), _running_snapshot_event()],
        posted,
    )

    assert _child_statuses(posted) == ["running"]


@pytest.mark.asyncio
@pytest.mark.parametrize("snapshot_stage", ["in-flight", "queued"])
@pytest.mark.parametrize(
    ("snapshot", "child_event", "snapshot_status", "child_status"),
    [
        (_running_snapshot_event(), _child_turn_event("turn/completed"), "running", "idle"),
        (_running_snapshot_event(), _child_error_event(), "running", "failed"),
        (_completed_snapshot_event(), _child_turn_event("turn/started"), "idle", "running"),
    ],
    ids=["completion", "failure", "new-turn"],
)
async def test_forwarder_in_flight_parent_snapshot_cannot_overwrite_the_child_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    snapshot: dict[str, Any],
    child_event: dict[str, Any],
    snapshot_status: str,
    child_status: str,
    snapshot_stage: str,
) -> None:
    posted: list[tuple[str, dict[str, Any]]] = []
    state, bridge_dir = _snapshot_test_state(tmp_path, monkeypatch)
    handler = _make_omnigent_handler(posted)
    snapshot_started = asyncio.Event()
    release_snapshot = asyncio.Event()
    child_started = asyncio.Event()

    async def transport(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if (
            request.url.path == "/v1/sessions/conv_child/events"
            and body.get("type") == "external_session_status"
            and body["data"]["status"] == snapshot_status
            and not snapshot_started.is_set()
            and snapshot_stage == "in-flight"
        ):
            snapshot_started.set()
            await release_snapshot.wait()
        return handler(request)

    snapshot_task: asyncio.Task[None] | None = None

    class ObservedLock(codex_native_forwarder._ReentrantAsyncLock):
        async def __aenter__(self) -> None:
            task = asyncio.current_task()
            if task is not None and task.get_name() == "codex-native-child-delivery":
                snapshot_started.set()
            await super().__aenter__()

    locks = {"conv_child": ObservedLock()} if snapshot_stage == "queued" else {}
    token = codex_native_forwarder._conversation_item_locks.set(locks)
    tasks: list[asyncio.Task[None]] = []
    try:
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000", transport=httpx.MockTransport(transport)
        ) as client:

            async def deliver(event: dict[str, Any]) -> None:
                if event is child_event:
                    child_started.set()
                await codex_native_forwarder._handle_event(
                    client,
                    **_forwarder_context(client, bridge_dir, session_id="conv_parent"),
                    event=event,
                    expected_thread_id="thread_parent",
                    forwarder_state=state,
                )

            try:
                async with asyncio.timeout(5):
                    await deliver(_subagent_activity_started_event())
                    if snapshot_stage == "queued":
                        async with codex_native_forwarder._conversation_item_delivery_scope(
                            "conv_child"
                        ):
                            snapshot_task = asyncio.create_task(deliver(snapshot))
                            tasks.append(snapshot_task)
                            await snapshot_started.wait()
                            await deliver(child_event)
                    else:
                        tasks.append(asyncio.create_task(deliver(snapshot)))
                        await snapshot_started.wait()
                        tasks.append(asyncio.create_task(deliver(child_event)))
                        await child_started.wait()
                        release_snapshot.set()
                    await asyncio.gather(*tasks)
                    await state.close_child_streams()
            finally:
                release_snapshot.set()
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await state.close_child_streams()
    finally:
        codex_native_forwarder._conversation_item_locks.reset(token)

    assert _child_statuses(posted) == (
        [child_status] if snapshot_stage == "queued" else [snapshot_status, child_status]
    )
    assert state.child_turn_status("thread_child") == child_status


def test_forwarder_collab_snapshot_marks_child_running_after_new_turn_starts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Once a settled child starts a new turn, a running snapshot is honoured again.
    """
    posted: list[tuple[str, dict[str, Any]]] = []
    state, bridge_dir = _snapshot_test_state(tmp_path, monkeypatch)

    _deliver_parent_events(
        state,
        bridge_dir,
        [
            _subagent_activity_started_event(),
            _child_turn_event("turn/started"),
            _child_turn_event("turn/completed"),
            _child_turn_event("turn/started", turn_id="turn_child_2"),
            _running_snapshot_event(),
        ],
        posted,
    )

    assert _child_statuses(posted) == ["running", "idle", "running", "running"]


def test_forwarder_collab_snapshot_does_not_settle_running_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A child mid-turn stays running when a stale snapshot reports it completed.
    """
    posted: list[tuple[str, dict[str, Any]]] = []
    state, bridge_dir = _snapshot_test_state(tmp_path, monkeypatch)

    _deliver_parent_events(
        state,
        bridge_dir,
        [
            _subagent_activity_started_event(),
            _child_turn_event("turn/started"),
            _completed_snapshot_event(),
        ],
        posted,
    )

    assert _child_statuses(posted) == ["running"]


def test_forwarder_replayed_spawn_snapshot_does_not_revive_backfilled_completed_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    After a reconnect, a replayed spawn item must not mark a finished child busy.

    A fresh forwarder has not seen the child's live turn lifecycle, but the
    child's backfilled ``thread/resume`` payload reports its last turn as
    completed. That terminal state wins over the spawn item's stale snapshot.
    """
    posted: list[tuple[str, dict[str, Any]]] = []
    codex_client = _PerThreadFakeCodexClient(
        thread_responses={"thread_child": _child_resume_response(turn_status="completed")}
    )
    state, bridge_dir = _snapshot_test_state(tmp_path, monkeypatch, codex_client=codex_client)

    _deliver_parent_events(state, bridge_dir, [_running_snapshot_event()], posted)

    assert len(_transcript_posts(posted, "conv_child")) == 1, "child backfill did not replay"
    assert _child_statuses(posted) == []


@pytest.mark.parametrize("turn_status", ["inProgress", "in_progress"])
def test_forwarder_replayed_spawn_snapshot_marks_backfilled_running_child_working(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    turn_status: str,
) -> None:
    """
    A replayed spawn snapshot still marks a child busy when its resumed turn is live,
    and a stale completed snapshot does not settle it.
    """
    posted: list[tuple[str, dict[str, Any]]] = []
    codex_client = _PerThreadFakeCodexClient(
        thread_responses={"thread_child": _child_resume_response(turn_status=turn_status)}
    )
    state, bridge_dir = _snapshot_test_state(tmp_path, monkeypatch, codex_client=codex_client)

    _deliver_parent_events(
        state, bridge_dir, [_running_snapshot_event(), _completed_snapshot_event()], posted
    )

    assert len(_transcript_posts(posted, "conv_child")) == 1, "child backfill did not replay"
    assert _child_statuses(posted) == ["running"]


@pytest.mark.parametrize(
    ("turn_statuses", "expected_posts"),
    [
        (["completed", "inProgress"], ["running"]),
        (["inProgress", "completed"], []),
    ],
    ids=["newest-running", "newest-completed"],
)
def test_forwarder_backfill_seeds_child_status_from_newest_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    turn_statuses: list[str],
    expected_posts: list[str],
) -> None:
    """
    Only the newest resumed turn decides whether a replayed running snapshot is honoured.
    """
    posted: list[tuple[str, dict[str, Any]]] = []
    codex_client = _PerThreadFakeCodexClient(
        thread_responses={"thread_child": _child_resume_response_with_turns(turn_statuses)}
    )
    state, bridge_dir = _snapshot_test_state(tmp_path, monkeypatch, codex_client=codex_client)

    _deliver_parent_events(state, bridge_dir, [_running_snapshot_event()], posted)

    assert _child_statuses(posted) == expected_posts


@pytest.mark.parametrize(
    ("resume_status", "live_event", "expected_statuses", "expected_state"),
    [
        ("completed", _child_turn_event("turn/started"), ["running", "running"], "running"),
        ("inProgress", _child_turn_event("turn/completed"), ["idle"], "idle"),
        ("inProgress", _child_turn_event("turn/failed"), ["failed"], "failed"),
    ],
    ids=["live-start", "live-completion", "live-failure"],
)
def test_forwarder_backfilled_status_does_not_overwrite_live_child_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    resume_status: str,
    live_event: dict[str, Any],
    expected_statuses: list[str],
    expected_state: str,
) -> None:
    """
    A live child lifecycle edge wins over a delayed resume status in either direction.

    A completion or failure during resume also defeats a later stale running snapshot.
    """
    posted: list[tuple[str, dict[str, Any]]] = []
    codex_client = _SuspendingResumeClient(
        {"thread_child": _child_resume_response(turn_status=resume_status)}
    )
    state, bridge_dir = _snapshot_test_state(tmp_path, monkeypatch, codex_client=codex_client)

    async def run() -> None:
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(_make_omnigent_handler(posted)),
        ) as client:

            def handle(event: dict[str, Any]) -> Any:
                return codex_native_forwarder._handle_event(
                    client,
                    **_forwarder_context(client, bridge_dir, session_id="conv_parent"),
                    event=event,
                    expected_thread_id="thread_parent",
                    forwarder_state=state,
                )

            spawn = asyncio.create_task(handle(_running_snapshot_event()))
            await asyncio.wait_for(codex_client.resume_started.wait(), timeout=5)
            await handle(live_event)
            codex_client.release_resume.set()
            await asyncio.wait_for(spawn, timeout=5)
            if expected_state != "running":
                await handle(_running_snapshot_event())

    asyncio.run(run())

    assert len(_transcript_posts(posted, "conv_child")) == 1, "child backfill did not replay"
    assert _child_statuses(posted) == expected_statuses
    assert state.child_turn_status("thread_child") == expected_state


def test_forwarder_resumed_model_does_not_overwrite_live_child_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A child's live model switch wins over the model in a delayed resume snapshot."""
    posted: list[tuple[str, dict[str, Any]]] = []
    child_resume = _child_resume_response()
    child_resume["result"]["thread"]["model"] = "gpt-snapshot"
    codex_client = _SuspendingResumeClient({"thread_child": child_resume})
    state, bridge_dir = _snapshot_test_state(tmp_path, monkeypatch, codex_client=codex_client)

    async def run() -> None:
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(_make_omnigent_handler(posted)),
        ) as client:

            def handle(event: dict[str, Any]) -> Any:
                return codex_native_forwarder._handle_event(
                    client,
                    **_forwarder_context(client, bridge_dir, session_id="conv_parent"),
                    event=event,
                    expected_thread_id="thread_parent",
                    forwarder_state=state,
                )

            try:
                spawn = asyncio.create_task(handle(_running_snapshot_event()))
                await asyncio.wait_for(codex_client.resume_started.wait(), timeout=5)
                await handle(
                    {
                        "method": "thread/settings/updated",
                        "params": {
                            "threadId": "thread_child",
                            "threadSettings": {"model": "gpt-live"},
                        },
                    }
                )
                codex_client.release_resume.set()
                await asyncio.wait_for(spawn, timeout=5)
                await handle(_usage_event("thread_child", (300, 0, 30), (300, 0, 30)))
                await state.settle_child_lanes()
            finally:
                await state.close_child_streams()

    asyncio.run(run())

    child_posts = [body for path, body in posted if path == "/v1/sessions/conv_child/events"]
    assert [
        body["data"]["model"] for body in child_posts if body["type"] == "external_model_change"
    ] == ["gpt-live"]
    assert [
        body["data"].get("model")
        for body in child_posts
        if body["type"] == "external_session_usage"
    ] == ["gpt-live"]
