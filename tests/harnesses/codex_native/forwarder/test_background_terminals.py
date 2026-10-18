"""Background-terminal count tests for the Codex forwarder."""

from __future__ import annotations

import logging
from pathlib import Path

import httpx
import pytest

from omnigent.harnesses.codex_native import forwarder as fwd
from omnigent.harnesses.codex_native.bridge import (
    CodexNativeBridgeState,
    write_bridge_state,
)
from tests.harnesses.codex_native.forwarder._support import _RecordingClient


def _command(item_id: str, command: str, *, status: str = "inProgress") -> dict:
    return {
        "type": "commandExecution",
        "id": item_id,
        "status": status,
        "processId": "4242",
        "source": "unifiedExecStartup",
        "command": command,
        "cwd": "/tmp",
    }


def _item_event(method: str, item: dict) -> dict:
    return {
        "method": method,
        "params": {"threadId": "thread_123", "turnId": "turn_123", "item": item},
    }


def _statuses(client: _RecordingClient) -> list[dict]:
    return [body["data"] for _, body in client.posts if body["type"] == "external_session_status"]


def _count_updates(client: _RecordingClient) -> list[dict]:
    return [body["data"] for _, body in client.posts if body["type"] == "subagent.status"]


async def _drive(
    tmp_path: Path,
    client: _RecordingClient,
    events: list[dict],
    state: fwd._CodexForwarderState | None = None,
) -> None:
    usage = fwd._SessionUsageCoalescer(client, "conv_x")  # type: ignore[arg-type]
    tracker = fwd._CodexElicitationTaskTracker()
    state = state or fwd._CodexForwarderState(parent_session_id="conv_x")
    for event in events:
        await fwd._handle_event(
            client,  # type: ignore[arg-type]
            session_id="conv_x",
            bridge_dir=tmp_path,
            event=event,
            usage_coalescer=usage,
            elicitation_tracker=tracker,
            expected_thread_id="thread_123",
            forwarder_state=state,
        )


def _bridge_state(tmp_path: Path) -> None:
    write_bridge_state(
        tmp_path,
        CodexNativeBridgeState(
            session_id="conv_x",
            socket_path=str(tmp_path / "app-server.sock"),
            thread_id="thread_123",
            codex_home=str(tmp_path / "codex-home"),
        ),
    )


_TURN_STARTED = {
    "method": "turn/started",
    "params": {"threadId": "thread_123", "turn": {"id": "turn_123", "status": "inProgress"}},
}
_TURN_COMPLETED = {
    "method": "turn/completed",
    "params": {
        "threadId": "thread_123",
        "turn": {"id": "turn_123", "status": "completed", "items": []},
    },
}


@pytest.mark.asyncio
async def test_terminal_outliving_its_turn_is_counted_until_it_exits(tmp_path: Path) -> None:
    """
    The TUI's "1 background terminal running" reaches Omnigent and clears on exit.

    The exit lands between turns, where a parent may already have dispatched the
    next message, so it updates the count without posting another turn-ending idle.
    """
    _bridge_state(tmp_path)
    client = _RecordingClient()
    background = _command("exec-bg", "/bin/bash -lc 'sleep 30'")
    foreground = _command("exec-fg", "/bin/bash -lc 'sleep 1'")

    await _drive(
        tmp_path,
        client,
        [
            _TURN_STARTED,
            _item_event("item/started", background),
            _item_event("item/started", foreground),
            _item_event("item/completed", {**foreground, "status": "completed"}),
            _TURN_COMPLETED,
            _item_event("item/completed", {**background, "status": "completed"}),
        ],
    )

    assert _statuses(client) == [
        {"status": "running", "response_id": "codex_turn_123"},
        {
            "status": "idle",
            "background_task_count": 1,
            "background_tasks": [
                {
                    "id": "exec-bg",
                    "type": "shell",
                    "status": "running",
                    "command": "/bin/bash -lc 'sleep 30'",
                }
            ],
            "response_id": "codex_turn_123",
        },
    ]
    assert _count_updates(client) == [{"idle": True, "background_task_count": 0}]


class _CountPostFailingClient(_RecordingClient):
    """Fails ``subagent.status`` posts while ``failing`` is set."""

    def __init__(self) -> None:
        super().__init__()
        self.failing = True

    async def post(self, url: str, *, json: dict, timeout: float | None = None) -> httpx.Response:
        response = await super().post(url, json=json, timeout=timeout)
        if self.failing and json["type"] == "subagent.status":
            return httpx.Response(500, request=response.request)
        return response


@pytest.mark.asyncio
async def test_failed_count_clear_is_restated_by_the_next_idle_edge(tmp_path: Path) -> None:
    """A clear the server never received must not leave its stale tally in place."""
    _bridge_state(tmp_path)
    client = _CountPostFailingClient()
    state = fwd._CodexForwarderState(parent_session_id="conv_x")
    background = _command("exec-bg", "/bin/bash -lc 'sleep 30'")

    await _drive(
        tmp_path,
        client,
        [
            _TURN_STARTED,
            _item_event("item/started", background),
            _TURN_COMPLETED,
            _item_event("item/completed", {**background, "status": "completed"}),
        ],
        state,
    )
    client.failing = False
    await _drive(tmp_path, client, [_TURN_STARTED, _TURN_COMPLETED], state)

    statuses = _statuses(client)
    assert [s["status"] for s in statuses] == ["running", "idle", "running", "idle"]
    assert statuses[1]["background_task_count"] == 1
    assert statuses[3]["background_task_count"] == 0


class _ZeroCountEdgeFailingClient(_RecordingClient):
    """Fails idle edges that clear the count while ``failing`` is set."""

    def __init__(self) -> None:
        super().__init__()
        self.failing = True

    async def post(self, url: str, *, json: dict, timeout: float | None = None) -> httpx.Response:
        response = await super().post(url, json=json, timeout=timeout)
        data = json["data"]
        if (
            self.failing
            and json["type"] == "external_session_status"
            and data.get("background_task_count") == 0
        ):
            return httpx.Response(500, request=response.request)
        return response


def _turn(turn_id: str, status: str) -> dict:
    method = "turn/started" if status == "inProgress" else "turn/completed"
    return {
        "method": method,
        "params": {
            "threadId": "thread_123",
            "turn": {"id": turn_id, "status": status, "items": []},
        },
    }


@pytest.mark.asyncio
async def test_failed_zero_count_idle_edge_is_restated_by_the_next_one(tmp_path: Path) -> None:
    """An idle edge that lost its clear must not leave later edges silent about the count."""
    _bridge_state(tmp_path)
    client = _ZeroCountEdgeFailingClient()
    state = fwd._CodexForwarderState(parent_session_id="conv_x")
    background = _command("exec-bg", "/bin/bash -lc 'sleep 30'")

    await _drive(
        tmp_path,
        client,
        [
            _turn("turn_1", "inProgress"),
            _item_event("item/started", background),
            _turn("turn_1", "completed"),
            _turn("turn_2", "inProgress"),
            _item_event("item/completed", {**background, "status": "completed"}),
            _turn("turn_2", "completed"),
        ],
        state,
    )
    client.failing = False
    await _drive(
        tmp_path, client, [_turn("turn_3", "inProgress"), _turn("turn_3", "completed")], state
    )

    idle = [s for s in _statuses(client) if s["status"] == "idle"]
    assert idle[0]["background_task_count"] == 1
    assert idle[-1]["background_task_count"] == 0
    assert idle[-1]["response_id"] == "codex_turn_3"


@pytest.mark.asyncio
async def test_resumed_forwarder_restates_a_count_left_by_its_predecessor(
    tmp_path: Path,
) -> None:
    """A replacement forwarder cannot see its predecessor's terminal, so it restates zero."""
    _bridge_state(tmp_path)
    client = _RecordingClient()
    background = _command("exec-bg", "/bin/bash -lc 'sleep 30'")
    await _drive(
        tmp_path,
        client,
        [
            _turn("turn_1", "inProgress"),
            _item_event("item/started", background),
            _turn("turn_1", "completed"),
        ],
    )

    resumed = fwd._CodexForwarderState(parent_session_id="conv_x")
    resumed.note_resume_response({"id": 1, "result": {}})
    await _drive(
        tmp_path,
        client,
        [
            _item_event("item/completed", {**background, "status": "completed"}),
            _turn("turn_2", "inProgress"),
            _turn("turn_2", "completed"),
        ],
        resumed,
    )

    idle = [s for s in _statuses(client) if s["status"] == "idle"]
    assert idle[0]["background_task_count"] == 1
    assert idle[-1]["background_task_count"] == 0
    assert idle[-1]["response_id"] == "codex_turn_2"


@pytest.mark.asyncio
async def test_turn_without_lingering_terminals_posts_no_count(tmp_path: Path) -> None:
    """Terminals that exit within their turn leave status edges unchanged."""
    _bridge_state(tmp_path)
    client = _RecordingClient()
    foreground = _command("exec-fg", "/bin/bash -lc 'echo hi'")

    await _drive(
        tmp_path,
        client,
        [
            _TURN_STARTED,
            _item_event("item/started", foreground),
            _item_event("item/completed", {**foreground, "status": "completed"}),
            _TURN_COMPLETED,
        ],
    )

    assert _statuses(client) == [
        {"status": "running", "response_id": "codex_turn_123"},
        {"status": "idle", "response_id": "codex_turn_123"},
    ]


@pytest.mark.asyncio
async def test_terminal_exiting_during_a_later_turn_does_not_idle_the_session(
    tmp_path: Path,
) -> None:
    """A mid-turn exit waits for that turn's idle edge instead of posting its own."""
    _bridge_state(tmp_path)
    client = _RecordingClient()
    background = _command("exec-bg", "/bin/bash -lc 'sleep 30'")
    second_started = {
        "method": "turn/started",
        "params": {"threadId": "thread_123", "turn": {"id": "turn_456", "status": "inProgress"}},
    }
    second_completed = {
        "method": "turn/completed",
        "params": {
            "threadId": "thread_123",
            "turn": {"id": "turn_456", "status": "completed", "items": []},
        },
    }

    await _drive(
        tmp_path,
        client,
        [
            _TURN_STARTED,
            _item_event("item/started", background),
            _TURN_COMPLETED,
            second_started,
            _item_event("item/completed", {**background, "status": "completed"}),
            second_completed,
        ],
    )

    statuses = _statuses(client)
    assert [s["status"] for s in statuses] == ["running", "idle", "running", "idle"]
    assert statuses[1]["background_task_count"] == 1
    assert statuses[3]["background_task_count"] == 0
    assert _count_updates(client) == []


@pytest.mark.asyncio
async def test_rotation_clears_the_old_chats_terminal_count() -> None:
    """After ``/new`` the old chat stops showing terminals the TUI no longer lists."""
    client = _RecordingClient()
    state = fwd._CodexForwarderState(parent_session_id="conv_old")
    state.note_terminal_started(_command("exec-bg", "sleep 30"))
    assert state.background_tasks_for_edge() is not None

    await fwd._clear_rotated_terminals(client, "conv_old", state)  # type: ignore[arg-type]

    assert client.posts == [
        (
            "/v1/sessions/conv_old/events",
            {
                "type": "subagent.status",
                "data": {"idle": True, "background_task_count": 0},
            },
        )
    ]
    assert state.background_tasks_for_edge() is None


@pytest.mark.asyncio
async def test_failed_rotation_clear_is_retried_against_the_old_chat(tmp_path: Path) -> None:
    """A clear lost during ``/new`` is retried on the old chat, not the new one."""
    _bridge_state(tmp_path)
    client = _CountPostFailingClient()
    state = fwd._CodexForwarderState(parent_session_id="conv_old")
    state.note_terminal_started(_command("exec-bg", "sleep 30"))
    assert state.background_tasks_for_edge() is not None

    await fwd._clear_rotated_terminals(client, "conv_old", state)  # type: ignore[arg-type]
    state.note_parent_rotation("conv_x")
    client.failing = False
    client.posts.clear()
    await _drive(tmp_path, client, [_TURN_STARTED, _TURN_COMPLETED], state)

    count_posts = [
        (url, body["data"]) for url, body in client.posts if body["type"] == "subagent.status"
    ]
    assert count_posts == [
        ("/v1/sessions/conv_old/events", {"idle": True, "background_task_count": 0})
    ]
    assert _statuses(client)[-1] == {"status": "idle", "response_id": "codex_turn_123"}


@pytest.mark.asyncio
async def test_consecutive_failed_rotation_clears_are_all_retried(tmp_path: Path) -> None:
    """A second failed ``/new`` clear does not drop the first chat's pending clear."""
    _bridge_state(tmp_path)
    client = _CountPostFailingClient()
    state = fwd._CodexForwarderState(parent_session_id="conv_a")
    for old_chat, new_chat in (("conv_a", "conv_b"), ("conv_b", "conv_x")):
        state.note_terminal_started(_command(f"exec-{old_chat}", "sleep 30"))
        assert state.background_tasks_for_edge() is not None
        await fwd._clear_rotated_terminals(client, old_chat, state)  # type: ignore[arg-type]
        state.note_parent_rotation(new_chat)

    client.failing = False
    client.posts.clear()
    await _drive(tmp_path, client, [_TURN_STARTED, _TURN_COMPLETED], state)

    count_posts = [
        (url, body["data"]) for url, body in client.posts if body["type"] == "subagent.status"
    ]
    assert count_posts == [
        ("/v1/sessions/conv_a/events", {"idle": True, "background_task_count": 0}),
        ("/v1/sessions/conv_b/events", {"idle": True, "background_task_count": 0}),
    ]


class _SubagentStatusRejectingClient(_RecordingClient):
    """Rejects ``subagent.status`` like servers that predate the event type."""

    async def post(self, url: str, *, json: dict, timeout: float | None = None) -> httpx.Response:
        response = await super().post(url, json=json, timeout=timeout)
        if json["type"] != "subagent.status":
            return response
        error = {
            "code": "invalid_input",
            "message": "Unknown event type: 'subagent.status'. Expected one of: ...",
        }
        return httpx.Response(400, json={"error": error}, request=response.request)


@pytest.mark.asyncio
async def test_server_without_count_only_updates_gets_counts_on_idle_edges(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An older server's rejection is learned once; idle edges keep its count current."""
    _bridge_state(tmp_path)
    client = _SubagentStatusRejectingClient()
    state = fwd._CodexForwarderState(parent_session_id="conv_x")
    exec_1, exec_2, exec_3 = (_command(f"exec-{n}", "sleep 30") for n in range(1, 4))

    async def lingering_turn(terminal: dict) -> None:
        events = [_TURN_STARTED, _item_event("item/started", terminal), _TURN_COMPLETED]
        await _drive(tmp_path, client, events, state)

    with caplog.at_level("INFO", logger=fwd.__name__):
        await lingering_turn(exec_1)
        # The first rejected clear arrives during ``/new``.
        await fwd._clear_rotated_terminals(client, "conv_x", state)  # type: ignore[arg-type]
        await lingering_turn(exec_2)
        await _drive(
            tmp_path,
            client,
            [
                _item_event("item/completed", {**exec_2, "status": "completed"}),
                _TURN_STARTED,
                _TURN_COMPLETED,
            ],
            state,
        )
        await lingering_turn(exec_3)
        await fwd._clear_rotated_terminals(client, "conv_x", state)  # type: ignore[arg-type]
        await _drive(tmp_path, client, [_TURN_STARTED, _TURN_COMPLETED], state)

    idle_counts = [
        s.get("background_task_count") for s in _statuses(client) if s["status"] == "idle"
    ]
    assert idle_counts == [1, 1, 0, 1, None]
    assert len(_count_updates(client)) == 1
    assert not state.pending_count_clear_session_ids
    assert not [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING and "subagent.status" in r.message
    ]


def test_running_terminal_command_is_bounded_for_display() -> None:
    """A huge heredoc command is clamped in the detail but still counted."""
    state = fwd._CodexForwarderState(parent_session_id="conv_x")
    state.note_terminal_started(_command("exec-bg", "x" * 5000))

    tasks = state.background_tasks_for_edge()

    assert tasks is not None and len(tasks) == 1
    assert tasks[0]["command"] == "x" * 512
