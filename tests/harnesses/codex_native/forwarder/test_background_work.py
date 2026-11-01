"""Background work and goal status for the session sidebar in Codex forwarding.

An active thread goal keeps the session working between Codex's turns. A goal
that Codex stopped for a usage or token-budget limit surfaces as one failure.
Notifications for other threads do not change the parent session.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.harnesses.codex_native import forwarder as fwd
from omnigent.harnesses.codex_native.app_server import CodexAppServerResponseError
from omnigent.harnesses.codex_native.bridge import read_bridge_state
from tests.harnesses.codex_native.forwarder._support import (
    _background_work_post,
    _deliver_to_parent,
    _RecordingClient,
    _seed_active_turn,
    _status_post,
    _terminal_tasks,
    _turn_completed,
    _turn_started,
)

_EVENTS_URL = "/v1/sessions/conv_x/events"


def _goal_updated(
    status: str, *, turn_id: str | None = None, thread_id: str = "thread_123"
) -> dict[str, Any]:
    """
    Build a Codex ``thread/goal/updated`` envelope.

    :param status: Goal status, e.g. ``"active"``.
    :param turn_id: Turn that caused the update, e.g. ``"turn_1"``. ``None`` for
        an update outside a turn.
    :param thread_id: Thread the goal belongs to, e.g. ``"thread_123"``.
    :returns: Notification envelope dict.
    """
    params: dict[str, Any] = {
        "threadId": thread_id,
        "goal": {
            "threadId": thread_id,
            "objective": "ship the change",
            "status": status,
            "tokensUsed": 0,
            "timeUsedSeconds": 0,
            "createdAt": 1,
            "updatedAt": 2,
        },
    }
    if turn_id is not None:
        params["turnId"] = turn_id
    return {"method": "thread/goal/updated", "params": params}


def _goal_cleared(thread_id: str = "thread_123") -> dict[str, Any]:
    """
    Build a Codex ``thread/goal/cleared`` envelope.

    :param thread_id: Thread whose goal was cleared, e.g. ``"thread_123"``.
    :returns: Notification envelope dict.
    """
    return {"method": "thread/goal/cleared", "params": {"threadId": thread_id}}


def _failed_status_post(output: str, **data: Any) -> tuple[str, dict[str, Any]]:
    """
    Return the recorded post for one failed ``external_session_status`` edge.

    :param output: Failure text shown on the session, e.g. ``"usage limit"``.
    :param data: Extra expected data keys, e.g. ``response_id``.
    :returns: ``(url, json)`` as recorded by :class:`_RecordingClient`.
    """
    return (
        _EVENTS_URL,
        {
            "type": "external_session_status",
            "data": {"status": "failed", "output": output, **data},
        },
    )


class _AppServerStub:
    """
    App-server stub for the goal and background-terminal reads.

    Every request is recorded. ``thread/goal/get`` answers ``goal_response``
    or raises ``goal_error``. Each ``thread/backgroundTerminals/list`` answers
    the next entry of ``listings``, then an empty list; it raises ``list_error``
    when set, and never returns when ``hang`` is set.

    :param goal_response: Response envelope returned for ``thread/goal/get``.
    :param goal_error: Exception raised for ``thread/goal/get`` instead.
    :param listings: Response envelopes for successive terminal listings.
    :param list_error: Exception raised for terminal listings.
    :param list_error_count: How many listings raise ``list_error``; every one when ``None``.
    :param hang: Whether a terminal listing never returns.
    """

    def __init__(
        self,
        goal_response: dict[str, Any] | None = None,
        *,
        goal_error: Exception | None = None,
        listings: list[dict[str, Any]] | None = None,
        list_error: Exception | None = None,
        list_error_count: int | None = None,
        hang: bool = False,
    ) -> None:
        self.goal_response = goal_response or {"result": {"goal": None}}
        self.goal_error = goal_error
        self.listings = list(listings or [])
        self.list_error = list_error
        self.list_error_count = list_error_count
        self.hang = hang
        self.requests: list[tuple[str, dict[str, Any]]] = []

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """
        Record one request and answer it.

        :param method: App-server method, e.g. ``"thread/resume"``.
        :param params: Request params.
        :returns: Canned response envelope.
        """
        self.requests.append((method, params))
        if method == "thread/goal/get":
            if self.goal_error is not None:
                raise self.goal_error
            return self.goal_response
        if method == "thread/backgroundTerminals/list":
            if self.hang:
                await asyncio.Event().wait()
            if self.list_error is not None and self.list_error_count != 0:
                if self.list_error_count is not None:
                    self.list_error_count -= 1
                raise self.list_error
            return self.listings.pop(0) if self.listings else {"result": {"data": []}}
        return {"result": {"thread": {"id": "thread_123"}}}

    def is_subscribed_to_thread(self, _thread_id: str) -> bool:
        """
        Report that this connection has not received live items for the thread.

        :param _thread_id: Codex thread id.
        :returns: ``False``.
        """
        return False


def _terminal_listing(*commands: str) -> dict[str, Any]:
    """
    Build a ``thread/backgroundTerminals/list`` response with running terminals.

    :param commands: Command of each running terminal, e.g. ``"npm run dev"``.
    :returns: Response envelope dict.
    """
    return {
        "result": {
            "data": [
                {
                    "processId": f"proc_{index}",
                    "itemId": f"item_{index}",
                    "command": command,
                    "cwd": "/repo",
                }
                for index, command in enumerate(commands)
            ]
        }
    }


@pytest.mark.asyncio
async def test_active_goal_keeps_the_session_working_until_it_ends(tmp_path: Path) -> None:
    """
    An active goal marks the session working; progress updates do not repost.

    Codex starts each continuation turn itself, so the flag must stay set
    between turns until the goal leaves ``active``.
    """
    client = _RecordingClient()
    state = fwd._CodexForwarderState()

    await _deliver_to_parent(client, tmp_path, state, _goal_updated("active"))
    await _deliver_to_parent(client, tmp_path, state, _goal_updated("active", turn_id="turn_1"))
    await _deliver_to_parent(client, tmp_path, state, _goal_updated("complete"))

    assert client.posts == [_background_work_post(True), _background_work_post(False)]


@pytest.mark.asyncio
async def test_cleared_goal_stops_the_session_working(tmp_path: Path) -> None:
    """Clearing an active goal drops the flag, and nothing else is posted."""
    client = _RecordingClient()
    state = fwd._CodexForwarderState()

    await _deliver_to_parent(client, tmp_path, state, _goal_updated("active"))
    await _deliver_to_parent(client, tmp_path, state, _goal_cleared())

    assert client.posts == [_background_work_post(True), _background_work_post(False)]


@pytest.mark.asyncio
async def test_goal_of_another_thread_does_not_change_the_session(tmp_path: Path) -> None:
    """A goal on a child or other thread leaves the parent session untouched."""
    client = _RecordingClient()
    state = fwd._CodexForwarderState()

    await _deliver_to_parent(
        client, tmp_path, state, _goal_updated("active", thread_id="thread_child")
    )

    assert client.posts == []


class _PausedTrueClient(_RecordingClient):
    """
    Recording client that holds the first ``running: true`` flag post until released.

    Posts are recorded when they complete, in the order the server would accept them.
    """

    def __init__(self) -> None:
        super().__init__()
        self.true_post_started = asyncio.Event()
        self.release_true_post = asyncio.Event()

    async def post(self, url: str, *, json: dict, timeout: float | None = None) -> httpx.Response:
        """
        Pause the first true flag post, then record and accept every post.

        :param url: Request URL, e.g. ``"/v1/sessions/conv_x/events"``.
        :param json: JSON body.
        :param timeout: Ignored request timeout.
        :returns: HTTP 200.
        """
        if json.get("data", {}).get("running") is True and not self.true_post_started.is_set():
            self.true_post_started.set()
            await self.release_true_post.wait()
        return await super().post(url, json=json, timeout=timeout)


@pytest.mark.asyncio
async def test_goal_cleared_during_a_resync_post_ends_not_working(tmp_path: Path) -> None:
    """A clear that arrives while the resync's ``true`` is in flight is the last word."""
    client = _PausedTrueClient()
    state = fwd._CodexForwarderState()
    app_server = _AppServerStub({"result": {"goal": {"status": "active"}}})
    resync = asyncio.create_task(
        fwd._resync_background_work(
            app_server,  # type: ignore[arg-type]
            client,  # type: ignore[arg-type]
            session_id="conv_x",
            bridge_dir=tmp_path,
            thread_id="thread_123",
            forwarder_state=state,
        )
    )
    await client.true_post_started.wait()
    cleared = asyncio.create_task(_deliver_to_parent(client, tmp_path, state, _goal_cleared()))
    await asyncio.sleep(0)
    client.release_true_post.set()
    await asyncio.gather(resync, cleared)

    assert client.posts == [_background_work_post(True, []), _background_work_post(False)]
    assert state.posted_background_work_running is False


@pytest.mark.asyncio
async def test_goal_read_overtaken_by_a_notification_is_dropped(tmp_path: Path) -> None:
    """A goal read that a newer notification overtook does not overwrite it."""
    client = _RecordingClient()
    state = fwd._CodexForwarderState()

    class _ClearedDuringRead(_AppServerStub):
        async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            if method == "thread/goal/get":
                await _deliver_to_parent(client, tmp_path, state, _goal_cleared())
            return await super().request(method, params)

    app_server = _ClearedDuringRead({"result": {"goal": {"status": "active"}}})
    await fwd._resync_background_work(
        app_server,  # type: ignore[arg-type]
        client,  # type: ignore[arg-type]
        session_id="conv_x",
        bridge_dir=tmp_path,
        thread_id="thread_123",
        forwarder_state=state,
    )

    assert state.goal_status is None
    assert client.posts == [_background_work_post(False), _background_work_post(None, [])]


class _OldServerClient(_RecordingClient):
    """
    Recording client for an Omnigent server that predates ``external_background_work``.

    :param accepts_flag: Whether the server takes the event's flag but predates
        its terminal list, so it rejects a post that carries only the list.
    """

    def __init__(self, *, accepts_flag: bool = False) -> None:
        super().__init__()
        self.accepts_flag = accepts_flag

    async def post(self, url: str, *, json: dict, timeout: float | None = None) -> httpx.Response:
        """
        Record the post and reject what this server predates.

        :param url: Request URL, e.g. ``"/v1/sessions/conv_x/events"``.
        :param json: JSON body.
        :param timeout: Ignored request timeout.
        :returns: HTTP 400 for a rejected ``external_background_work`` post, 200 otherwise.
        """
        self.posts.append((url, json))
        request = httpx.Request("POST", url)
        running = json.get("data", {}).get("running")
        if json.get("type") != "external_background_work" or (
            self.accepts_flag and running is not None
        ):
            return httpx.Response(200, request=request)
        message = (
            f"external_background_work requires data.running to be a boolean; got {running!r}"
            if self.accepts_flag
            else "Unknown event type: 'external_background_work'. Allowed types: []"
        )
        return httpx.Response(
            400, json={"error": {"code": "invalid_input", "message": message}}, request=request
        )


@pytest.mark.asyncio
async def test_server_without_the_flag_is_not_asked_again(tmp_path: Path) -> None:
    """An older server rejects the flag once; later goal changes neither post nor add failures."""
    client = _OldServerClient()
    state = fwd._CodexForwarderState()

    await _deliver_to_parent(client, tmp_path, state, _goal_updated("active"))
    failures = fwd._forward_health.consecutive_failures
    await _deliver_to_parent(client, tmp_path, state, _goal_updated("complete"))
    await _deliver_to_parent(client, tmp_path, state, _goal_updated("active"))

    assert client.posts == [_background_work_post(True)]
    assert fwd._forward_health.consecutive_failures == failures


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["paused", "blocked", "complete"])
async def test_goal_that_is_not_active_reports_idle_without_a_failure(
    tmp_path: Path, status: str
) -> None:
    """
    A goal that is not active posts the cleared flag and no failure.

    The first sync on a connection always reaches the server, so the value
    is posted even when it matches the server's default.
    """
    client = _RecordingClient()
    state = fwd._CodexForwarderState()

    await _deliver_to_parent(client, tmp_path, state, _goal_updated(status))

    assert client.posts == [_background_work_post(False)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "output"),
    [
        ("usageLimited", "Codex stopped the goal: usage limit reached."),
        ("budgetLimited", "Codex stopped the goal: token budget reached."),
    ],
)
async def test_goal_stopped_by_a_limit_fails_the_session_once(
    tmp_path: Path, status: str, output: str
) -> None:
    """
    A goal stopped by a usage or token-budget limit fails the session once.

    A repeated update with the same status is not a new transition.
    """
    client = _RecordingClient()
    state = fwd._CodexForwarderState()

    await _deliver_to_parent(client, tmp_path, state, _goal_updated("active"))
    await _deliver_to_parent(client, tmp_path, state, _goal_updated(status, turn_id="turn_1"))
    await _deliver_to_parent(client, tmp_path, state, _goal_updated(status, turn_id="turn_1"))

    assert client.posts == [
        _background_work_post(True),
        _background_work_post(False),
        _failed_status_post(output),
    ]


@pytest.mark.asyncio
async def test_goal_limit_does_not_repeat_a_failure_the_turn_already_reported(
    tmp_path: Path,
) -> None:
    """
    A usage-limit error that already failed the turn is not failed again.

    Codex reports the error before it stops the goal, so the goal update must
    not add a second failure for the same turn.
    """
    _seed_active_turn(tmp_path, "turn_1")
    client = _RecordingClient()
    state = fwd._CodexForwarderState()

    await _deliver_to_parent(
        client,
        tmp_path,
        state,
        {
            "method": "error",
            "params": {
                "threadId": "thread_123",
                "turnId": "turn_1",
                "willRetry": False,
                "error": {
                    "message": "You've hit your usage limit.",
                    "codexErrorInfo": "usageLimitExceeded",
                },
            },
        },
    )
    await _deliver_to_parent(
        client, tmp_path, state, _goal_updated("usageLimited", turn_id="turn_1")
    )

    assert client.posts == [
        _failed_status_post(
            "You've hit your usage limit.",
            response_id="codex_turn_1",
        ),
        _background_work_post(False),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("turn_id", "start_seen"),
    [
        ("present", True),
        ("omitted", True),
        ("null", True),
        ("omitted", False),
        ("null", False),
    ],
)
async def test_turn_completion_after_a_goal_limit_keeps_the_failure(
    tmp_path: Path, turn_id: str, start_seen: bool
) -> None:
    """
    The turn's own completion does not replace the goal failure with idle.

    The budget stop lands while the turn is still open. Its ``turn/completed``
    must be absorbed like a turn that already reported a failure. A stop that
    names no turn belongs to the turn running when it lands, or, when the
    forwarder missed that turn's start, to the next turn boundary.
    """
    if start_seen:
        _seed_active_turn(tmp_path, "turn_1")
    client = _RecordingClient()
    state = fwd._CodexForwarderState()
    limit = _goal_updated("budgetLimited", turn_id="turn_1" if turn_id == "present" else None)
    if turn_id == "null":
        limit["params"]["turnId"] = None

    await _deliver_to_parent(client, tmp_path, state, limit)
    await _deliver_to_parent(
        client,
        tmp_path,
        state,
        {
            "method": "turn/completed",
            "params": {
                "threadId": "thread_123",
                "turn": {
                    "id": "turn_1",
                    "status": "completed",
                    "items": [{"type": "agentMessage", "id": "item_1", "text": "done"}],
                },
            },
        },
    )

    assert client.posts == [
        _background_work_post(False),
        _failed_status_post("Codex stopped the goal: token budget reached."),
    ]
    bridge_state = read_bridge_state(tmp_path)
    assert bridge_state is None or bridge_state.active_turn_id is None
    assert not state.goal_failure_awaits_turn


@pytest.mark.asyncio
async def test_reconnect_reads_the_thread_goal_and_reports_it(tmp_path: Path) -> None:
    """
    A (re)subscription reads the thread's goal, since Codex only notifies on change.

    Without this read, a forwarder started while a goal is active would leave the
    session idle until the next goal update.
    """
    app_server = _AppServerStub(
        {
            "result": {
                "goal": {
                    "threadId": "thread_123",
                    "objective": "ship the change",
                    "status": "active",
                    "tokensUsed": 0,
                    "timeUsedSeconds": 0,
                    "createdAt": 1,
                    "updatedAt": 2,
                }
            }
        }
    )
    client = _RecordingClient()
    state = fwd._CodexForwarderState()

    await fwd._subscribe_until_ready(
        app_server,  # type: ignore[arg-type]
        client,  # type: ignore[arg-type]
        session_id="conv_x",
        bridge_dir=tmp_path,
        thread_id="thread_123",
        usage_coalescer=fwd._SessionUsageCoalescer(client, "conv_x"),  # type: ignore[arg-type]
        elicitation_tracker=fwd._CodexElicitationTaskTracker(),
        forwarder_state=state,
    )

    assert ("thread/goal/get", {"threadId": "thread_123"}) in app_server.requests
    assert client.posts == [_background_work_post(True, [])]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rejection",
    [
        {"code": -32601, "message": "Method not found"},
        {"code": -32600, "message": "unknown variant `thread/goal/get`, expected one of ..."},
    ],
)
async def test_codex_without_goals_reconnects_as_goal_free(
    tmp_path: Path, rejection: dict[str, Any]
) -> None:
    """A Codex build that rejects ``thread/goal/get`` has no goal, so the flag is cleared."""
    app_server = _AppServerStub(goal_error=CodexAppServerResponseError(rejection))
    client = _RecordingClient()
    state = fwd._CodexForwarderState()

    await fwd._subscribe_until_ready(
        app_server,  # type: ignore[arg-type]
        client,  # type: ignore[arg-type]
        session_id="conv_x",
        bridge_dir=tmp_path,
        thread_id="thread_123",
        usage_coalescer=fwd._SessionUsageCoalescer(client, "conv_x"),  # type: ignore[arg-type]
        elicitation_tracker=fwd._CodexElicitationTaskTracker(),
        forwarder_state=state,
    )

    assert client.posts == [_background_work_post(False, [])]


@pytest.mark.asyncio
async def test_failed_goal_read_keeps_the_goal_the_forwarder_last_saw(tmp_path: Path) -> None:
    """
    A transport failure reading the goal keeps the last known goal.

    The active goal was posted before the forwarder reconnected, so a failed
    read must not drop the flag.
    """
    app_server = _AppServerStub(goal_error=ConnectionError("app-server socket closed"))
    client = _RecordingClient()
    state = fwd._CodexForwarderState(goal_status="active", posted_background_work_running=True)

    await fwd._subscribe_until_ready(
        app_server,  # type: ignore[arg-type]
        client,  # type: ignore[arg-type]
        session_id="conv_x",
        bridge_dir=tmp_path,
        thread_id="thread_123",
        usage_coalescer=fwd._SessionUsageCoalescer(client, "conv_x"),  # type: ignore[arg-type]
        elicitation_tracker=fwd._CodexElicitationTaskTracker(),
        forwarder_state=state,
    )

    assert client.posts == [_background_work_post(None, [])]


@pytest.mark.asyncio
async def test_failed_goal_read_on_a_fresh_connection_does_not_clear_the_flag(
    tmp_path: Path,
) -> None:
    """
    A reconnect that cannot read the goal posts no false for a goal it never saw.

    The server may still hold ``true`` for a goal that is active, and an empty
    terminal listing says nothing about the goal.
    """
    app_server = _AppServerStub(goal_error=ConnectionError("app-server socket closed"))
    client = _RecordingClient()
    state = fwd._CodexForwarderState()

    await fwd._subscribe_until_ready(
        app_server,  # type: ignore[arg-type]
        client,  # type: ignore[arg-type]
        session_id="conv_x",
        bridge_dir=tmp_path,
        thread_id="thread_123",
        usage_coalescer=fwd._SessionUsageCoalescer(client, "conv_x"),  # type: ignore[arg-type]
        elicitation_tracker=fwd._CodexElicitationTaskTracker(),
        forwarder_state=state,
    )

    assert client.posts == [_background_work_post(None, [])]
    # A goal notification makes the goal known again, so its end clears the flag.
    await _deliver_to_parent(client, tmp_path, state, _goal_updated("complete"))
    assert client.posts == [_background_work_post(None, []), _background_work_post(False)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("posted", "posted_tasks", "expected_data"),
    [
        (True, None, {"running": False}),
        (True, _terminal_tasks("npm run dev"), {"running": False, "background_task_count": 0}),
        (False, _terminal_tasks("npm run dev"), {"background_task_count": 0}),
        (False, [], None),
        (None, None, None),
    ],
)
async def test_clear_releases_the_flag_and_list_only_of_a_session_that_had_them(
    posted: bool | None,
    posted_tasks: list[dict[str, Any]] | None,
    expected_data: dict[str, Any] | None,
) -> None:
    """
    A ``/clear`` clears the flag and terminal list it set on the superseded session.

    The forwarder stops following that session, so nothing else would clear them.
    """
    client = _RecordingClient()
    state = fwd._CodexForwarderState(
        posted_background_work_running=posted, posted_background_tasks=posted_tasks
    )

    await fwd._clear_superseded_background_work(client, "conv_old", state)  # type: ignore[arg-type]

    assert client.posts == (
        []
        if expected_data is None
        else [
            (
                "/v1/sessions/conv_old/events",
                {"type": "external_background_work", "data": expected_data},
            )
        ]
    )


def test_new_parent_session_starts_with_no_goal_and_no_posted_flag() -> None:
    """Rotating to a new thread forgets the old thread's goal and the old baseline."""
    state = fwd._CodexForwarderState(
        goal_status="active",
        posted_background_work_running=True,
        background_terminals=_terminal_tasks("npm run dev"),
        posted_background_tasks=_terminal_tasks("npm run dev"),
        turn_failure_surfaced=True,
    )

    state.note_parent_rotation("conv_new")

    assert state.goal_status is None
    assert state.posted_background_work_running is None
    assert state.background_terminals is None
    assert state.posted_background_tasks is None
    assert state.turn_failure_surfaced is False
    assert state.background_work_running() is False


def _record_poll_delays(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """
    Make the forwarder's poll wait yield once instead of sleeping.

    :param monkeypatch: Pytest fixture used to replace the forwarder's sleep.
    :returns: Delays the forwarder asked to sleep for, in order.
    """
    delays: list[float] = []

    async def yield_once(seconds: float) -> None:
        """Record one requested delay and let other tasks run."""
        delays.append(seconds)
        await asyncio.sleep(0)

    monkeypatch.setattr(fwd, "_sleep", yield_once)
    return delays


def _park_poll(monkeypatch: pytest.MonkeyPatch) -> asyncio.Event:
    """
    Make the forwarder's poll wait until it is cancelled.

    :param monkeypatch: Pytest fixture used to replace the forwarder's sleep.
    :returns: Event set once the poll is waiting between listings.
    """
    waiting = asyncio.Event()

    async def park(_seconds: float) -> None:
        """Hold the poll until the forwarder cancels it."""
        waiting.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(fwd, "_sleep", park)
    return waiting


@pytest.mark.asyncio
async def test_running_background_terminal_keeps_the_session_working_after_its_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A terminal still running when a turn ends keeps the session working.

    The forwarder lists terminals right after the turn, then every few seconds,
    and clears the flag once none is left. The composer list goes with each
    change, including a terminal replaced by another, and an unchanged listing
    posts nothing.
    """
    _seed_active_turn(tmp_path, "turn_1")
    delays = _record_poll_delays(monkeypatch)
    app_server = _AppServerStub(
        listings=[
            _terminal_listing("npm run dev"),
            _terminal_listing("npm run dev"),
            _terminal_listing("make watch"),
        ]
    )
    client = _RecordingClient()
    state = fwd._CodexForwarderState()

    await _deliver_to_parent(
        client,
        tmp_path,
        state,
        _turn_completed("turn_1", "completed"),
        codex_client=app_server,
    )
    poll = state.background_terminal_poll
    assert poll is not None
    await poll

    assert client.posts == [
        _status_post({"status": "idle", "response_id": "codex_turn_1"}),
        _background_work_post(True, _terminal_tasks("npm run dev")),
        _background_work_post(None, _terminal_tasks("make watch")),
        _background_work_post(False, []),
    ]
    assert delays == [fwd._BACKGROUND_TERMINAL_POLL_SECONDS] * 3
    assert [method for method, _params in app_server.requests] == [
        "thread/backgroundTerminals/list"
    ] * 4


@pytest.mark.asyncio
async def test_server_without_the_list_still_gets_flag_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A server from before the composer list rejects a list sent alone once.

    Later list changes are not posted to it, and flag changes still are.
    """
    _seed_active_turn(tmp_path, "turn_1")
    _record_poll_delays(monkeypatch)
    app_server = _AppServerStub(
        listings=[
            _terminal_listing("npm run dev"),
            _terminal_listing("npm run dev", "make watch"),
            _terminal_listing("npm run dev", "make watch", "pytest -f"),
        ]
    )
    client = _OldServerClient(accepts_flag=True)
    state = fwd._CodexForwarderState()

    await _deliver_to_parent(
        client,
        tmp_path,
        state,
        _turn_completed("turn_1", "completed"),
        codex_client=app_server,
    )
    poll = state.background_terminal_poll
    assert poll is not None
    await poll

    assert client.posts == [
        _status_post({"status": "idle", "response_id": "codex_turn_1"}),
        _background_work_post(True, _terminal_tasks("npm run dev")),
        _background_work_post(None, _terminal_tasks("npm run dev", "make watch")),
        _background_work_post(False),
    ]


@pytest.mark.asyncio
async def test_terminal_listing_counts_every_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A listing longer than one page lists the terminals of every page."""
    _seed_active_turn(tmp_path, "turn_1")
    _record_poll_delays(monkeypatch)
    first_page = _terminal_listing("npm run dev")
    first_page["result"]["nextCursor"] = "page_2"
    second_page = {"result": {"data": [{"processId": "proc_1", "command": "make watch"}]}}
    app_server = _AppServerStub(listings=[first_page, second_page])
    client = _RecordingClient()
    state = fwd._CodexForwarderState()

    await _deliver_to_parent(
        client,
        tmp_path,
        state,
        _turn_completed("turn_1", "completed"),
        codex_client=app_server,
    )

    assert app_server.requests[:2] == [
        ("thread/backgroundTerminals/list", {"threadId": "thread_123"}),
        ("thread/backgroundTerminals/list", {"threadId": "thread_123", "cursor": "page_2"}),
    ]
    assert client.posts[-1] == _background_work_post(
        True, [*_terminal_tasks("npm run dev"), {"id": "proc_1", "command": "make watch"}]
    )
    state.cancel_background_terminal_poll()


@pytest.mark.asyncio
async def test_failed_turn_sends_the_terminal_list_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A failed turn clears the server's background-task tally, so the next
    listing sends a terminal list that did not change.
    """
    _seed_active_turn(tmp_path, "turn_1")
    _record_poll_delays(monkeypatch)
    app_server = _AppServerStub(listings=[_terminal_listing("npm run dev")])
    client = _RecordingClient()
    state = fwd._CodexForwarderState(
        background_terminals=_terminal_tasks("npm run dev"),
        posted_background_work_running=True,
        posted_background_tasks=_terminal_tasks("npm run dev"),
    )

    await _deliver_to_parent(
        client,
        tmp_path,
        state,
        _turn_completed("turn_1", "failed"),
        codex_client=app_server,
    )

    assert client.posts[0][1]["data"]["status"] == "failed"
    assert client.posts[1:] == [_background_work_post(None, _terminal_tasks("npm run dev"))]
    state.cancel_background_terminal_poll()


class _HeldListAnswerClient(_RecordingClient):
    """Records the first terminal-list post as stored, then holds its answer until released."""

    def __init__(self) -> None:
        super().__init__()
        self.list_stored = asyncio.Event()
        self.release_list_answer = asyncio.Event()

    async def post(self, url: str, *, json: dict, timeout: float | None = None) -> httpx.Response:
        """
        Record every post; hold the answer to the first one that carries a list.

        :param url: Request URL, e.g. ``"/v1/sessions/conv_x/events"``.
        :param json: JSON body.
        :param timeout: Ignored request timeout.
        :returns: HTTP 200.
        """
        response = await super().post(url, json=json, timeout=timeout)
        if "background_task_count" in json.get("data", {}) and not self.list_stored.is_set():
            self.list_stored.set()
            await self.release_list_answer.wait()
        return response


@pytest.mark.asyncio
async def test_system_error_during_a_listing_post_resends_the_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A failure that clears the server's tally while a list post awaits its answer
    still gets the unchanged list sent again by the next listing.
    """
    _seed_active_turn(tmp_path, "turn_1")
    _record_poll_delays(monkeypatch)
    app_server = _AppServerStub(
        listings=[_terminal_listing("npm run dev"), _terminal_listing("npm run dev")]
    )
    client = _HeldListAnswerClient()
    state = fwd._CodexForwarderState()
    forget_started = asyncio.Event()
    forget = state.forget_posted_terminals

    async def signal_then_forget() -> None:
        """Note that the failure's invalidation began, then run it."""
        forget_started.set()
        await forget()

    monkeypatch.setattr(state, "forget_posted_terminals", signal_then_forget)

    turn_end = asyncio.create_task(
        _deliver_to_parent(
            client,
            tmp_path,
            state,
            _turn_completed("turn_1", "completed"),
            codex_client=app_server,
        )
    )
    await asyncio.wait_for(client.list_stored.wait(), timeout=5.0)
    system_error = asyncio.create_task(
        _deliver_to_parent(
            client,
            tmp_path,
            state,
            {
                "method": "thread/status/changed",
                "params": {"threadId": "thread_123", "status": {"type": "systemError"}},
            },
        )
    )
    await asyncio.wait_for(forget_started.wait(), timeout=5.0)
    client.release_list_answer.set()
    await asyncio.wait_for(asyncio.gather(turn_end, system_error), timeout=5.0)
    poll = state.background_terminal_poll
    assert poll is not None
    await poll

    list_posts = [
        body["data"].get("background_task_count")
        for _url, body in client.posts
        if body["type"] == "external_background_work" and "background_task_count" in body["data"]
    ]
    statuses = [
        body["data"]["status"]
        for _url, body in client.posts
        if body["type"] == "external_session_status"
    ]
    assert statuses == ["idle", "failed"]
    # The held post, the same list again after the failure, then the terminal exiting.
    assert list_posts == [1, 1, 0]


@pytest.mark.asyncio
async def test_overlapping_listings_leave_one_poll_that_shutdown_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reconnect listing and a turn-end listing that finish out of order start one poll."""
    _park_poll(monkeypatch)
    releases = [asyncio.Event(), asyncio.Event()]
    listed = 0

    class _HeldListings(_AppServerStub):
        async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            nonlocal listed
            if method == "thread/backgroundTerminals/list":
                release = releases[listed]
                listed += 1
                await release.wait()
                return _terminal_listing("npm run dev")
            return await super().request(method, params)

    app_server = _HeldListings()
    client = _RecordingClient()
    state = fwd._CodexForwarderState()

    def track() -> asyncio.Task[None]:
        return asyncio.create_task(
            fwd._track_background_terminals(
                client,  # type: ignore[arg-type]
                app_server,  # type: ignore[arg-type]
                session_id="conv_x",
                bridge_dir=tmp_path,
                thread_id="thread_123",
                forwarder_state=state,
            )
        )

    reconnect, turn_end = track(), track()
    for _ in range(5):
        await asyncio.sleep(0)
    releases[1].set()
    await turn_end
    releases[0].set()
    await reconnect
    for _ in range(5):
        await asyncio.sleep(0)

    polls = [t for t in asyncio.all_tasks() if t.get_name() == "codex-native-background-terminals"]
    assert polls == [state.background_terminal_poll]
    state.cancel_background_terminal_poll()
    for _ in range(5):
        await asyncio.sleep(0)
    assert all(poll.done() for poll in polls)


@pytest.mark.asyncio
async def test_turn_start_stops_the_terminal_poll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A turn that starts while terminals are polled ends the poll; the turn reports itself."""
    _seed_active_turn(tmp_path, "turn_1")
    waiting = _park_poll(monkeypatch)
    app_server = _AppServerStub(listings=[_terminal_listing("npm run dev")])
    client = _RecordingClient()
    state = fwd._CodexForwarderState()
    await _deliver_to_parent(
        client,
        tmp_path,
        state,
        _turn_completed("turn_1", "completed"),
        codex_client=app_server,
    )
    poll = state.background_terminal_poll
    assert poll is not None
    await asyncio.wait_for(waiting.wait(), timeout=5.0)

    await _deliver_to_parent(
        client, tmp_path, state, _turn_started("turn_2"), codex_client=app_server
    )

    assert state.background_terminal_poll is None
    with pytest.raises(asyncio.CancelledError):
        await poll
    assert [method for method, _params in app_server.requests] == [
        "thread/backgroundTerminals/list"
    ]


@pytest.mark.asyncio
async def test_goal_and_terminal_share_one_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    The flag stays set while either an active goal or a terminal keeps the session working.

    The flag posts once per change of either; the terminal list posts on its own changes.
    """
    _seed_active_turn(tmp_path, "turn_1")
    _record_poll_delays(monkeypatch)
    app_server = _AppServerStub(listings=[_terminal_listing("npm run dev")])
    client = _RecordingClient()
    state = fwd._CodexForwarderState()

    await _deliver_to_parent(client, tmp_path, state, _goal_updated("active"))
    await _deliver_to_parent(
        client,
        tmp_path,
        state,
        _turn_completed("turn_1", "completed"),
        codex_client=app_server,
    )
    poll = state.background_terminal_poll
    assert poll is not None
    await poll
    await _deliver_to_parent(client, tmp_path, state, _goal_updated("complete"))

    assert client.posts == [
        _background_work_post(True),
        _status_post({"status": "idle", "response_id": "codex_turn_1"}),
        _background_work_post(None, _terminal_tasks("npm run dev")),
        _background_work_post(None, []),
        _background_work_post(False),
    ]


@pytest.mark.asyncio
async def test_codex_without_terminal_listing_is_not_asked_again(
    tmp_path: Path,
) -> None:
    """A Codex build that rejects the terminal listing is asked once, then never again."""
    app_server = _AppServerStub(
        list_error=CodexAppServerResponseError({"code": -32601, "message": "Method not found"})
    )
    client = _RecordingClient()
    state = fwd._CodexForwarderState()

    _seed_active_turn(tmp_path, "turn_1")
    await _deliver_to_parent(
        client,
        tmp_path,
        state,
        _turn_completed("turn_1", "completed"),
        codex_client=app_server,
    )
    _seed_active_turn(tmp_path, "turn_2")
    await _deliver_to_parent(
        client,
        tmp_path,
        state,
        _turn_completed("turn_2", "completed"),
        codex_client=app_server,
    )

    assert state.background_terminals_supported is False
    assert state.background_terminal_poll is None
    assert [method for method, _params in app_server.requests] == [
        "thread/backgroundTerminals/list"
    ]
    assert client.posts == [
        _status_post({"status": "idle", "response_id": "codex_turn_1"}),
        _background_work_post(False),
        _status_post({"status": "idle", "response_id": "codex_turn_2"}),
    ]


@pytest.mark.asyncio
async def test_failed_terminal_listing_keeps_the_last_known_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A listing that fails for a transport reason keeps the flag, and polling goes on."""
    _seed_active_turn(tmp_path, "turn_1")
    _record_poll_delays(monkeypatch)
    app_server = _AppServerStub(
        list_error=ConnectionError("app-server socket closed"), list_error_count=1
    )
    client = _RecordingClient()
    state = fwd._CodexForwarderState(
        background_terminals=_terminal_tasks("npm run dev"),
        posted_background_work_running=True,
        posted_background_tasks=_terminal_tasks("npm run dev"),
    )

    await _deliver_to_parent(
        client,
        tmp_path,
        state,
        _turn_completed("turn_1", "completed"),
        codex_client=app_server,
    )
    assert client.posts == [_status_post({"status": "idle", "response_id": "codex_turn_1"})]

    # The next listing succeeds and finds no terminal, which clears the flag.
    poll = state.background_terminal_poll
    assert poll is not None
    await poll
    assert client.posts[-1] == _background_work_post(False, [])


@pytest.mark.asyncio
async def test_failed_listing_with_no_known_terminal_does_not_poll(tmp_path: Path) -> None:
    """With no terminal known to be running, a failed listing schedules no poll."""
    _seed_active_turn(tmp_path, "turn_1")
    app_server = _AppServerStub(list_error=ConnectionError("app-server socket closed"))
    client = _RecordingClient()
    state = fwd._CodexForwarderState()

    await _deliver_to_parent(
        client,
        tmp_path,
        state,
        _turn_completed("turn_1", "completed"),
        codex_client=app_server,
    )

    assert state.background_terminal_poll is None


@pytest.mark.asyncio
async def test_hung_terminal_listing_does_not_hold_the_turn_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A listing that never answers is abandoned after a bound; the turn still reports."""
    _seed_active_turn(tmp_path, "turn_1")
    monkeypatch.setattr(fwd, "_BACKGROUND_TERMINAL_LIST_TIMEOUT_SECONDS", 0.01)
    app_server = _AppServerStub(hang=True)
    client = _RecordingClient()
    state = fwd._CodexForwarderState()

    await asyncio.wait_for(
        _deliver_to_parent(
            client,
            tmp_path,
            state,
            _turn_completed("turn_1", "completed"),
            codex_client=app_server,
        ),
        timeout=5.0,
    )

    assert client.posts == [
        _status_post({"status": "idle", "response_id": "codex_turn_1"}),
        _background_work_post(False),
    ]
    assert state.background_terminal_poll is None


@pytest.mark.asyncio
async def test_reconnect_during_a_turn_does_not_list_terminals(tmp_path: Path) -> None:
    """A reconnect while a turn runs leaves terminals to that turn's terminal event."""
    _seed_active_turn(tmp_path, "turn_1")
    app_server = _AppServerStub(listings=[_terminal_listing("npm run dev")])
    client = _RecordingClient()
    state = fwd._CodexForwarderState()

    await fwd._subscribe_until_ready(
        app_server,  # type: ignore[arg-type]
        client,  # type: ignore[arg-type]
        session_id="conv_x",
        bridge_dir=tmp_path,
        thread_id="thread_123",
        usage_coalescer=fwd._SessionUsageCoalescer(client, "conv_x"),  # type: ignore[arg-type]
        elicitation_tracker=fwd._CodexElicitationTaskTracker(),
        forwarder_state=state,
    )

    assert "thread/backgroundTerminals/list" not in [method for method, _ in app_server.requests]
    assert client.posts == [_background_work_post(False)]


@pytest.mark.asyncio
async def test_reconnect_after_a_settled_turn_reports_running_terminals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reconnect with no turn running lists terminals, reports them, and keeps polling."""
    delays = _record_poll_delays(monkeypatch)
    app_server = _AppServerStub(
        listings=[_terminal_listing("npm run dev"), _terminal_listing("npm run dev")]
    )
    client = _RecordingClient()
    state = fwd._CodexForwarderState()

    await fwd._subscribe_until_ready(
        app_server,  # type: ignore[arg-type]
        client,  # type: ignore[arg-type]
        session_id="conv_x",
        bridge_dir=tmp_path,
        thread_id="thread_123",
        usage_coalescer=fwd._SessionUsageCoalescer(client, "conv_x"),  # type: ignore[arg-type]
        elicitation_tracker=fwd._CodexElicitationTaskTracker(),
        forwarder_state=state,
    )
    poll = state.background_terminal_poll
    assert poll is not None
    await poll

    assert client.posts == [
        _background_work_post(True, _terminal_tasks("npm run dev")),
        _background_work_post(False, []),
    ]
    assert delays == [fwd._BACKGROUND_TERMINAL_POLL_SECONDS] * 2


@pytest.mark.asyncio
async def test_rotation_stops_the_terminal_poll_of_the_old_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A thread switch cancels the old thread's poll and forgets its terminals."""
    waiting = _park_poll(monkeypatch)
    state = fwd._CodexForwarderState(background_terminals=_terminal_tasks("npm run dev"))
    state.background_terminal_poll = asyncio.create_task(
        fwd._poll_background_terminals(
            _RecordingClient(),  # type: ignore[arg-type]
            _AppServerStub(),  # type: ignore[arg-type]
            session_id="conv_x",
            bridge_dir=Path("."),
            thread_id="thread_123",
            forwarder_state=state,
        )
    )
    await asyncio.wait_for(waiting.wait(), timeout=5.0)
    poll = state.background_terminal_poll

    state.note_parent_rotation("conv_new")

    assert state.background_terminal_poll is None
    assert state.has_background_terminals is False
    assert poll is not None
    with pytest.raises(asyncio.CancelledError):
        await poll
