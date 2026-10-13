"""Lifecycle coverage for exactly-once delivery of Claude-native self-resumed child turns.

The tests drive the real ``/events`` handler, the runner-local status publisher
the status-file poller uses, and the real ``sys_read_inbox`` drain.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from typing import Any

import pytest

from omnigent.runner import app as runner_app
from omnigent.runner import create_runner_app, subagent_work, tool_dispatch
from omnigent.spec.types import AgentSpec, ExecutorSpec
from tests.runner.conftest import _FakeProcessManager, _runner_client, _ScriptedHarnessClient
from tests.runner.helpers import NullServerClient

PARENT_SESSION_ID = "conv_parent_orchestrator"
CHILD_SESSION_ID = "conv_child_reviewer"
SECOND_CHILD_SESSION_ID = "conv_child_reviewer_two"
# The ``omnigent.wrapper`` label a claude-native sub-agent session carries.
CLAUDE_SUBAGENT_WRAPPER = "claude-code-native-ui-subagent"


_REGISTRY_MAPS = (
    (subagent_work, "_subagent_work_by_child"),
    (subagent_work, "_subagent_work_by_parent"),
    (subagent_work, "_session_inboxes_ref"),
    (runner_app, "_session_event_queues_ref"),
    (subagent_work, "_child_session_parents"),
    (subagent_work, "_drained_delivered_subagent_children"),
    (subagent_work, "_drained_subagent_dispatches"),
    (subagent_work, "_in_flight_send_locks"),
    (subagent_work, "_in_flight_send_lock_users"),
    (subagent_work, "_subagent_recovery_done"),
    (subagent_work, "_subagent_recovery_locks"),
)


@pytest.fixture
def _clean_subagent_registry() -> Iterator[None]:
    """Snapshot and restore the process-wide sub-agent / inbox maps.

    The sub-agent work registry, child records, inbox and event queues are
    module-level containers that otherwise leak across tests. Maps a tree does
    not define are skipped so the tests still run as a fail-to-pass check on it.
    """
    maps: list[dict[Any, Any] | set[Any]] = [
        getattr(module, name) for module, name in _REGISTRY_MAPS if hasattr(module, name)
    ]
    saved = [
        {k: (set(v) if isinstance(v, set) else v) for k, v in m.items()}
        if isinstance(m, dict)
        else set(m)
        for m in maps
    ]
    for m in maps:
        m.clear()
    try:
        yield
    finally:
        for m, snapshot in zip(maps, saved, strict=True):
            m.clear()
            m.update(snapshot)  # type: ignore[arg-type]


class _ChildSnapshotServerClient(NullServerClient):
    """Serve the child's sub-agent snapshot, ALLOW the inbox-drain policy, record labels.

    ``GET /v1/sessions/{child}`` returns the child ``SessionResponse`` (parent
    link + ``sub_agent_name``) the runner reads to rebuild a lost work entry.
    ``POST .../policies/evaluate`` returns ``ALLOW`` so the real ``sys_read_inbox``
    drain formats and cleans up the delivered item rather than re-queuing it.
    ``labels`` mirrors the child's server-side labels as the runner stamps
    dispatch ids and writes delivery receipts; ``patches`` lists every label
    write. A write yields once, as a real request would, so concurrent handlers
    interleave around it.
    """

    def __init__(self) -> None:
        self.labels: dict[str, str] = {}
        self.patches: list[dict[str, str]] = []

    async def patch(self, url: str, **kwargs: Any) -> Any:
        del url
        body = kwargs.get("json") or {}
        labels = dict(body.get("labels") or {})
        await asyncio.sleep(0)
        self.patches.append(labels)
        self.labels.update(labels)
        return self._Response()

    class _Resp:
        def __init__(self, payload: dict[str, Any]) -> None:
            self.status_code = 200
            self._payload = payload

        def json(self) -> dict[str, Any]:
            return self._payload

        def raise_for_status(self) -> None:
            return None

    async def get(self, url: str, **kwargs: Any) -> Any:
        del kwargs
        if url.rstrip("/").endswith(CHILD_SESSION_ID):
            return self._Resp(
                {
                    "id": CHILD_SESSION_ID,
                    "agent_id": "ag_reviewer",
                    "agent_name": "claude-native-ui",
                    "sub_agent_name": "reviewer",
                    "parent_session_id": PARENT_SESSION_ID,
                    "created_at": 0,
                    "workspace": None,
                }
            )
        return self._Response()

    async def post(self, url: str, **kwargs: Any) -> Any:
        del kwargs
        if url.rstrip("/").endswith("/policies/evaluate"):
            return self._Resp({"result": "POLICY_ACTION_ALLOW"})
        return self._Response()


class _RecordingServerClient(_ChildSnapshotServerClient):
    """Record the runner's event posts and serve restart recovery.

    ``child_status`` / ``child_output`` are what a restarted runner reads back
    through ``child_sessions`` and ``items``, alongside the recorded ``labels``.
    """

    def __init__(self) -> None:
        super().__init__()
        self.posts: list[tuple[str, Any]] = []
        self.child_status = "completed"
        self.child_output = ""

    async def get(self, url: str, **kwargs: Any) -> Any:
        path = url.rstrip("/")
        if path.endswith(f"/v1/sessions/{PARENT_SESSION_ID}/child_sessions"):
            child = {
                "id": CHILD_SESSION_ID,
                "tool": "reviewer",
                "session_name": "review",
                "current_task_status": self.child_status,
                "labels": dict(self.labels),
            }
            return self._Resp({"data": [child], "has_more": False})
        if path.endswith(f"/v1/sessions/{CHILD_SESSION_ID}/items"):
            message = {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": self.child_output}],
            }
            return self._Resp({"data": [message], "has_more": False})
        return await super().get(url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> Any:
        self.posts.append((url, kwargs.get("json")))
        return await super().post(url, **kwargs)


_HARNESS_BY_AGENT = {
    "ag_sdk_parent": "claude-sdk",
    "ag_native_parent": "claude-native",
    "ag_codex_parent": "codex-native",
    "ag_antigravity_parent": "antigravity-native",
}


async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
    """Resolve a spec whose harness is encoded in the agent id (default claude-native)."""
    del session_id
    return AgentSpec(
        spec_version=1,
        name=agent_id,
        executor=ExecutorSpec(
            type="omnigent", config={"harness": _HARNESS_BY_AGENT.get(agent_id, "claude-native")}
        ),
    )


def _pin_child_harness(
    app: Any, child_id: str = CHILD_SESSION_ID, harness: str = "claude-native"
) -> None:
    """Record the worker's harness the way a routed dispatch pins it on the runner."""
    app.state.session_harness_overrides[child_id] = harness


async def _await_settle(app: Any, child_id: str) -> None:
    """Wait for *child_id*'s pending settle task to finish (or be cancelled)."""
    task = app.state.rearm_settle_tasks.get(child_id)
    if task is None:
        await asyncio.sleep(0)
        return
    _done, pending = await asyncio.wait({task}, timeout=5)
    assert not pending, "the re-arm settle did not finish within 5 s"
    if not task.cancelled():
        task.result()


def _dispatch_worker(
    server: NullServerClient, *, wrapper_label: str | None = None
) -> tuple[Any, asyncio.Queue[dict[str, Any]], str]:
    """Build the runner app with the orchestrator's inbox and one dispatched worker.

    Mirrors what ``sys_session_send`` leaves behind: the child->parent record
    and the dispatch entry, both carrying the child's wrapper label when known.

    :returns: ``(app, parent_inbox, work_id)``.
    """
    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server,  # type: ignore[arg-type]
    )
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    subagent_work._session_inboxes_ref[PARENT_SESSION_ID] = inbox
    subagent_work.register_child_session(
        CHILD_SESSION_ID,
        parent_session_id=PARENT_SESSION_ID,
        title="reviewer:review",
        tool="reviewer",
        session_name="review",
        wrapper_label=wrapper_label,
    )
    entry = subagent_work.register_subagent_work(
        parent_session_id=PARENT_SESSION_ID,
        child_session_id=CHILD_SESSION_ID,
        agent="reviewer",
        title="review the diff",
        wrapper_label=wrapper_label,
    )
    _pin_child_harness(app)
    return app, inbox, entry.work_id


def _pane_status_publisher(app: Any) -> Any:
    """The runner-local publisher the status-file poller and pane watcher call."""
    publish = app.state.session_resource_registry._session_status_publisher
    assert publish is not None
    return publish


async def _post_status(
    client: Any,
    *,
    status: str,
    output: str | None = None,
    turn_completed: bool | None = None,
    response_id: str | None = None,
    failure_detail: str | None = None,
) -> Any:
    """POST an ``external_session_status`` edge to the child, as the forwarder does.

    A claude-native ``Stop`` carries ``turn_completed`` and names the turn it
    ends with ``response_id``; a ``StopFailure`` carries ``failure_detail``; a
    bare ``idle`` is the pane watcher's quiescence edge and is never a
    completion on its own.
    """
    data: dict[str, Any] = {"status": status}
    if output is not None:
        data["output"] = output
    if turn_completed is not None:
        data["turn_completed"] = turn_completed
    if response_id is not None:
        data["response_id"] = response_id
    if failure_detail is not None:
        data["failure_detail"] = failure_detail
    return await client.post(
        f"/v1/sessions/{CHILD_SESSION_ID}/events",
        json={"type": "external_session_status", "data": data},
    )


async def _deliver_and_drain_round_one(
    client: Any, inbox: asyncio.Queue[dict[str, Any]], server: NullServerClient
) -> None:
    """The worker reports round one and the orchestrator reads it with ``sys_read_inbox``."""
    r1 = await _post_status(
        client, status="idle", output="round one: found the bug", turn_completed=True
    )
    assert r1.status_code == 204
    assert inbox.qsize() == 1, (
        "control failed: the worker's first completion was not delivered to the orchestrator inbox"
    )
    drained_text = await tool_dispatch._drain_inbox(
        inbox, server_client=server, conversation_id=PARENT_SESSION_ID
    )
    assert "round one: found the bug" in drained_text


def _drain_queue(inbox: asyncio.Queue[Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    while not inbox.empty():
        items.append(inbox.get_nowait())
    return items


def _pop_stream_events(session_id: str) -> list[dict[str, Any]]:
    """Pop every event published on *session_id*'s own stream."""
    queue = runner_app._session_event_queues_ref.get(session_id)
    events: list[dict[str, Any]] = []
    while queue is not None and not queue.empty():
        event = queue.get_nowait()
        if isinstance(event, dict):
            events.append(event)
    return events


def _status_events(events: list[dict[str, Any]]) -> list[str]:
    return [str(e.get("status")) for e in events if e.get("type") == "session.status"]


def _parent_status_events() -> list[str]:
    """Pop the ``session.status`` values published on the orchestrator's own stream."""
    return _status_events(_pop_stream_events(PARENT_SESSION_ID))


def _child_row_statuses(events: list[dict[str, Any]]) -> list[str]:
    """The worker's ``current_task_status`` values mirrored onto the orchestrator's stream."""
    return [
        str(e["child"]["current_task_status"])
        for e in events
        if e.get("type") == "session.child_session.updated"
    ]


def _assert_fresh_result(
    second: list[dict[str, Any]], *, server: _ChildSnapshotServerClient, dispatched_id: str
) -> None:
    """The self-resumed result arrived once, under an id of its own stamped on the child."""
    assert second, (
        "the worker's self-resumed turn was invisible to the orchestrator: its "
        "external_session_status:idle produced no inbox item because the child was "
        "still remembered as drained, so mark_subagent_work_terminal answered "
        "'already delivered'."
    )
    assert second[0]["type"] == "sub_agent"
    assert second[0]["status"] == "completed"
    stamped = server.labels.get(subagent_work.SUBAGENT_DISPATCH_ID_LABEL_KEY)
    assert second[0]["work_id"] == stamped != dispatched_id, (
        "the self-resumed result must carry a dispatch id of its own, stamped on the "
        "child before delivery; under the drained turn's receipted id a runner restart "
        "before the drain would skip this result"
    )
    assert "round two" in second[0]["output"]


@pytest.mark.asyncio
async def test_selfresumed_worker_turn_reaches_orchestrator_inbox(
    _clean_subagent_registry: None,
) -> None:
    """A ``running`` edge on ``/events`` re-arms a drained worker's delivery."""
    server = _ChildSnapshotServerClient()
    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server,  # type: ignore[arg-type]
    )

    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    subagent_work._session_inboxes_ref[PARENT_SESSION_ID] = inbox

    # The orchestrator's sys_session_send dispatched this worker.
    entry = subagent_work.register_subagent_work(
        parent_session_id=PARENT_SESSION_ID,
        child_session_id=CHILD_SESSION_ID,
        agent="reviewer",
        title="review the diff",
    )
    work_id = entry.work_id

    async with _runner_client(app) as client:
        # Round 1: the worker finishes its first turn; the forwarder reports idle.
        r1 = await _post_status(client, status="idle", output="round one: found the bug")
        assert r1.status_code == 204
        assert inbox.qsize() == 1, (
            "control failed: the worker's first completion was not delivered to "
            "the orchestrator inbox"
        )

        # The orchestrator reads its inbox (sys_read_inbox); this drain marks the
        # child delivered/drained via unregister_subagent_work.
        drained_text = await tool_dispatch._drain_inbox(
            inbox, server_client=server, conversation_id=PARENT_SESSION_ID
        )
        assert "round one: found the bug" in drained_text
        assert CHILD_SESSION_ID in subagent_work._drained_delivered_subagent_children
        assert subagent_work.get_subagent_work(CHILD_SESSION_ID) is None

        # Round 2: Claude resumes the worker on its own -- no parent sys_session_send
        # -- and the turn ends with a question.
        r2_running = await _post_status(client, status="running")
        assert r2_running.status_code == 204
        r2_idle = await _post_status(
            client, status="idle", output="round two: should I open the PR?"
        )
        assert r2_idle.status_code == 204

        second = _drain_queue(inbox)

    _assert_fresh_result(second, server=server, dispatched_id=work_id)


@pytest.mark.asyncio
async def test_status_poller_running_edge_rearms_delivery_for_a_drained_worker(
    _clean_subagent_registry: None,
) -> None:
    """The runner-local ``running`` edge re-arms delivery; none arrives on ``/events``.

    A claude-native worker's ``running`` is never an ``external_session_status``:
    the forwarder maps only ``Stop`` / ``StopFailure``, and the status-file
    poller and pane watcher publish through the registry's status publisher
    inside the runner. That edge is the only "new work" signal the runner sees
    before the self-resumed turn's ``Stop``.
    """
    server = _ChildSnapshotServerClient()
    app, inbox, work_id = _dispatch_worker(server)
    publish_pane_status = _pane_status_publisher(app)

    async with _runner_client(app) as client:
        await _deliver_and_drain_round_one(client, inbox, server)
        assert subagent_work.get_subagent_work(CHILD_SESSION_ID) is None

        # Claude's status file flips back to busy: the poller publishes
        # running through the runner-local publisher, never through /events.
        publish_pane_status(CHILD_SESSION_ID, "running", None)

        r2_idle = await _post_status(
            client, status="idle", output="round two: should I open the PR?", turn_completed=True
        )
        assert r2_idle.status_code == 204

        second = _drain_queue(inbox)

    _assert_fresh_result(second, server=server, dispatched_id=work_id)


@pytest.mark.asyncio
async def test_running_edge_before_the_drain_keeps_the_selfresumed_result_deliverable(
    _clean_subagent_registry: None,
) -> None:
    """The poller re-asserts ``running`` before the orchestrator drains round one.

    Claude's status file stays ``busy`` across the ``Stop`` while a delegate keeps
    working, so the re-armed poller publishes ``running`` within a tick -- long
    before the orchestrator is woken and reads its inbox. In that order the
    drain must not discard the worker's live turn, and round two must still be
    delivered under the dispatch id stamped on the child.
    """
    server = _ChildSnapshotServerClient()
    app, inbox, work_id = _dispatch_worker(server)
    publish_pane_status = _pane_status_publisher(app)

    async with _runner_client(app) as client:
        r1 = await _post_status(
            client, status="idle", output="round one: found the bug", turn_completed=True
        )
        assert r1.status_code == 204
        assert inbox.qsize() == 1

        publish_pane_status(CHILD_SESSION_ID, "running", None)

        drained_text = await tool_dispatch._drain_inbox(
            inbox, server_client=server, conversation_id=PARENT_SESSION_ID
        )
        assert "round one: found the bug" in drained_text
        live = subagent_work.get_subagent_work(CHILD_SESSION_ID)
        status_after_drain = live.status if live is not None else None

        r2_idle = await _post_status(
            client, status="idle", output="round two: should I open the PR?", turn_completed=True
        )
        assert r2_idle.status_code == 204
        second = _drain_queue(inbox)

    _assert_fresh_result(second, server=server, dispatched_id=work_id)
    assert status_after_drain == "running", (
        "draining round one removed the worker's live entry although its next turn "
        "had already started"
    )
    assert CHILD_SESSION_ID not in subagent_work._drained_delivered_subagent_children


@pytest.mark.asyncio
async def test_selfresumed_worker_counts_as_running_when_the_orchestrator_turn_ends(
    _clean_subagent_registry: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An orchestrator whose turn ends while a self-resumed worker runs reads ``waiting``.

    ``_on_proxy_stream_end`` derives ``waiting`` from live work entries only, so
    the worker's re-arming ``running`` edge must leave one behind under the
    dispatch id stamped on the child.
    """
    monkeypatch.setattr(runner_app, "_server_version", "0.16.0")
    server = _ChildSnapshotServerClient()
    app, inbox, work_id = _dispatch_worker(server)
    publish_pane_status = _pane_status_publisher(app)

    async with _runner_client(app) as client:
        await _deliver_and_drain_round_one(client, inbox, server)
        assert subagent_work.get_subagent_work(CHILD_SESSION_ID) is None

        publish_pane_status(CHILD_SESSION_ID, "running", None)
        entry = subagent_work.get_subagent_work(CHILD_SESSION_ID)
        assert entry is not None and entry.status == "running", (
            "the self-resumed worker is not tracked as running, so the orchestrator "
            "cannot be shown waiting on it"
        )
        assert entry.work_id == work_id

        _parent_status_events()
        app.state.on_proxy_stream_end(PARENT_SESSION_ID)
        await asyncio.sleep(0)
        assert _parent_status_events() == ["waiting"]


@pytest.mark.asyncio
async def test_running_edge_keeps_a_result_the_orchestrator_has_not_received(
    _clean_subagent_registry: None,
) -> None:
    """A finished result still awaiting delivery is not replaced by new activity.

    With no orchestrator inbox on this runner yet, round one stays recorded
    undelivered; the worker's ``running`` edge must leave that result in place
    so the retry path can still hand it over.
    """
    server = _ChildSnapshotServerClient()
    app, _inbox, work_id = _dispatch_worker(server)
    subagent_work._session_inboxes_ref.pop(PARENT_SESSION_ID)
    publish_pane_status = _pane_status_publisher(app)

    async with _runner_client(app) as client:
        await _post_status(
            client, status="idle", output="round one: found the bug", turn_completed=True
        )
        recorded = subagent_work.get_subagent_work(CHILD_SESSION_ID)
        assert recorded is not None
        assert recorded.status == "completed" and not recorded.delivered

        publish_pane_status(CHILD_SESSION_ID, "running", None)

    kept = subagent_work.get_subagent_work(CHILD_SESSION_ID)
    assert kept is recorded
    assert kept.status == "completed" and not kept.delivered
    assert kept.output == "round one: found the bug"
    assert kept.work_id == work_id


class _HeldSleep:
    """Stand-in for ``_wake_retry_sleep`` that parks until the test releases it."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        await self.release.wait()


async def _let_settle_run() -> None:
    """Give a just-published idle edge the loop turns it needs to schedule its settle."""
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_spurious_running_edge_is_settled_when_the_file_reads_idle_again(
    _clean_subagent_registry: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A re-arm with no ``Stop`` behind it goes back to the drained state.

    The poller is re-armed by the hook's idle, so a ``busy`` left over from the
    turn that just ended can publish ``running`` once more. When the file then
    reads ``idle`` and no terminal report arrives within the grace window, the
    dispatch must not stay live: the orchestrator would read ``waiting`` forever
    and the trailing-idle dedup for the drained turn would be lost.
    """
    monkeypatch.setattr(runner_app, "_server_version", "0.16.0")
    held = _HeldSleep()
    monkeypatch.setattr(subagent_work, "_wake_retry_sleep", held)
    server = _ChildSnapshotServerClient()
    app, inbox, work_id = _dispatch_worker(server)
    publish_pane_status = _pane_status_publisher(app)

    async with _runner_client(app) as client:
        await _deliver_and_drain_round_one(client, inbox, server)
        app.state.native_pane_status[PARENT_SESSION_ID] = "idle"
        _parent_status_events()

        publish_pane_status(CHILD_SESSION_ID, "running", None)
        await asyncio.sleep(0)
        assert _parent_status_events() == ["waiting"]
        live = subagent_work.get_subagent_work(CHILD_SESSION_ID)
        assert live is not None and live.rearmed and live.status == "running"

        # Claude's file reads idle again and no Stop follows.
        publish_pane_status(CHILD_SESSION_ID, "idle", None)
        await _let_settle_run()
        assert held.calls == [runner_app._SUBAGENT_REARM_SETTLE_GRACE_S]
        assert subagent_work.get_subagent_work(CHILD_SESSION_ID) is live, (
            "the re-arm must survive until the grace window elapses"
        )
        held.release.set()
        await _await_settle(app, CHILD_SESSION_ID)

        assert subagent_work.get_subagent_work(CHILD_SESSION_ID) is None
        assert CHILD_SESSION_ID in subagent_work._drained_delivered_subagent_children
        assert subagent_work._drained_subagent_dispatches[CHILD_SESSION_ID].work_id == work_id
        assert _parent_status_events() == ["idle"], (
            "the orchestrator must stop reading waiting on a turn that never happened"
        )

        # The drained turn's trailing idle stays a no-op.
        r = await _post_status(client, status="idle", output="round one: found the bug")
        assert r.status_code == 204
        assert inbox.empty()


@pytest.mark.asyncio
async def test_turn_end_within_the_grace_window_keeps_the_rearmed_result(
    _clean_subagent_registry: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude writes ``idle`` before its ``Stop`` lands; the result is still delivered.

    At the end of a real self-resumed turn the file flips to idle moments before
    the forwarder's turn-end edge reaches the runner, so the grace window must
    leave the re-armed entry in place for that report.
    """
    held = _HeldSleep()
    monkeypatch.setattr(subagent_work, "_wake_retry_sleep", held)
    server = _ChildSnapshotServerClient()
    app, inbox, work_id = _dispatch_worker(server)
    publish_pane_status = _pane_status_publisher(app)

    async with _runner_client(app) as client:
        await _deliver_and_drain_round_one(client, inbox, server)
        publish_pane_status(CHILD_SESSION_ID, "running", None)
        publish_pane_status(CHILD_SESSION_ID, "idle", None)
        await _let_settle_run()

        r2_idle = await _post_status(
            client, status="idle", output="round two: should I open the PR?", turn_completed=True
        )
        assert r2_idle.status_code == 204
        second = _drain_queue(inbox)
        _assert_fresh_result(second, server=server, dispatched_id=work_id)

        held.release.set()
        await _await_settle(app, CHILD_SESSION_ID)
        settled = subagent_work.get_subagent_work(CHILD_SESSION_ID)
        assert settled is not None and settled.status == "completed" and settled.delivered
        assert not settled.rearmed
        assert CHILD_SESSION_ID not in subagent_work._drained_delivered_subagent_children


@pytest.mark.asyncio
@pytest.mark.parametrize("late_response_id", ["resp_round_two", None])
@pytest.mark.parametrize("drained", [True, False])
async def test_turn_end_after_the_grace_window_still_delivers_the_rearmed_result(
    _clean_subagent_registry: None,
    monkeypatch: pytest.MonkeyPatch,
    drained: bool,
    late_response_id: str | None,
) -> None:
    """A ``Stop`` delayed past the grace window is a real result, not a duplicate.

    The forwarder backs off for up to 30 s under an outage, so a self-resumed
    turn's completion can land after its re-arm settled. The settled state (the
    drain memory or the delivered entry) remembers that re-arm, so a report that
    names another turn, or none, is delivered exactly once instead of being
    answered "already delivered"; only a replay of round one's own report is.
    """
    held = _HeldSleep()
    monkeypatch.setattr(subagent_work, "_wake_retry_sleep", held)
    server = _ChildSnapshotServerClient()
    app, inbox, work_id = _dispatch_worker(server)
    publish_pane_status = _pane_status_publisher(app)

    async with _runner_client(app) as client:
        r1 = await _post_status(
            client,
            status="idle",
            output="round one: found the bug",
            turn_completed=True,
            response_id="resp_round_one",
        )
        assert r1.status_code == 204 and inbox.qsize() == 1
        if drained:
            await tool_dispatch._drain_inbox(
                inbox,
                server_client=server,  # type: ignore[arg-type]
                conversation_id=PARENT_SESSION_ID,
            )
        publish_pane_status(CHILD_SESSION_ID, "running", None)
        publish_pane_status(CHILD_SESSION_ID, "idle", None)
        await _let_settle_run()
        held.release.set()
        await _await_settle(app, CHILD_SESSION_ID)
        assert (subagent_work.get_subagent_work(CHILD_SESSION_ID) is None) is drained

        # Round one's own Stop replayed after the settle is still a duplicate.
        r_replay = await _post_status(
            client,
            status="idle",
            output="round one: found the bug",
            turn_completed=True,
            response_id="resp_round_one",
        )
        assert r_replay.status_code == 204
        assert inbox.qsize() == (0 if drained else 1)

        # The self-resumed turn's Stop lands only now.
        r2 = await _post_status(
            client,
            status="idle",
            output="round two: should I open the PR?",
            turn_completed=True,
            response_id=late_response_id,
        )
        assert r2.status_code == 204
        items = _drain_queue(inbox)
        assert len(items) == (1 if drained else 2), (
            "the settled re-arm answered the late completion 'already delivered'"
        )
        _assert_fresh_result(items[-1:], server=server, dispatched_id=work_id)

        # That welcome is spent: a repeat of the same Stop is a duplicate again.
        r_dup = await _post_status(
            client,
            status="idle",
            output="round two: should I open the PR?",
            turn_completed=True,
            response_id=late_response_id,
        )
        assert r_dup.status_code == 204
        assert inbox.empty()
        live = subagent_work.get_subagent_work(CHILD_SESSION_ID)
        assert live is not None and live.status == "completed" and live.delivered
        assert not live.rearmed
        assert CHILD_SESSION_ID not in subagent_work._drained_delivered_subagent_children


@pytest.mark.asyncio
async def test_spurious_running_edge_restores_a_delivered_result_the_parent_has_not_read(
    _clean_subagent_registry: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round one was delivered but not drained when a stale ``busy`` re-armed the child.

    Settling puts the delivered entry back, so the orchestrator's later drain
    still finds the dispatch it is receipting and a replay of round one's own
    report (the forwarder re-sending the same turn's Stop) is still answered as
    already delivered.
    """
    held = _HeldSleep()
    monkeypatch.setattr(subagent_work, "_wake_retry_sleep", held)
    server = _ChildSnapshotServerClient()
    app, inbox, work_id = _dispatch_worker(server)
    publish_pane_status = _pane_status_publisher(app)

    async with _runner_client(app) as client:
        r1 = await _post_status(
            client,
            status="idle",
            output="round one: found the bug",
            turn_completed=True,
            response_id="resp_round_one",
        )
        assert r1.status_code == 204
        delivered = subagent_work.get_subagent_work(CHILD_SESSION_ID)
        assert delivered is not None and delivered.delivered

        publish_pane_status(CHILD_SESSION_ID, "running", None)
        assert subagent_work.get_subagent_work(CHILD_SESSION_ID) is not delivered
        publish_pane_status(CHILD_SESSION_ID, "idle", None)
        await _let_settle_run()
        held.release.set()
        await _await_settle(app, CHILD_SESSION_ID)

        assert subagent_work.get_subagent_work(CHILD_SESSION_ID) is delivered
        assert delivered.work_id == work_id and delivered.status == "completed"
        r_dup = await _post_status(
            client,
            status="idle",
            output="round one: found the bug",
            turn_completed=True,
            response_id="resp_round_one",
        )
        assert r_dup.status_code == 204
        assert inbox.qsize() == 1, "the duplicate report must not deliver round one twice"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent_id", "rearm_events", "settle_events"),
    [
        ("ag_sdk_parent", ["waiting"], ["idle"]),
        ("ag_native_parent", [], []),
        ("ag_codex_parent", [], []),
        ("ag_antigravity_parent", [], []),
    ],
)
async def test_selfresume_waiting_edge_follows_the_parent_harness(
    _clean_subagent_registry: None,
    monkeypatch: pytest.MonkeyPatch,
    agent_id: str,
    rearm_events: list[str],
    settle_events: list[str],
) -> None:
    """An idle SDK parent is shown waiting, then idle again; a native parent is left alone.

    The parent's harness comes from its initialized spec, as in production. The
    synthetic edge is gated on that harness, not on what ``_publish_turn_status``
    drops: it lets ``waiting`` through for codex-native and antigravity-native
    parents while suppressing ``idle``, so publishing it for them would leave
    such a parent waiting forever once the re-arm settles.
    """
    monkeypatch.setattr(runner_app, "_server_version", "0.16.0")
    held = _HeldSleep()
    monkeypatch.setattr(subagent_work, "_wake_retry_sleep", held)
    server = _ChildSnapshotServerClient()
    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server,  # type: ignore[arg-type]
    )
    publish_pane_status = _pane_status_publisher(app)

    async with _runner_client(app) as client:
        created = await client.post(
            "/v1/sessions", json={"session_id": PARENT_SESSION_ID, "agent_id": agent_id}
        )
        assert created.status_code == 201, created.text
        _pin_child_harness(app)
        inbox = subagent_work._session_inboxes_ref.setdefault(PARENT_SESSION_ID, asyncio.Queue())
        subagent_work.register_child_session(
            CHILD_SESSION_ID,
            parent_session_id=PARENT_SESSION_ID,
            title="reviewer:review",
            tool="reviewer",
            session_name="review",
        )
        subagent_work.register_subagent_work(
            parent_session_id=PARENT_SESSION_ID,
            child_session_id=CHILD_SESSION_ID,
            agent="reviewer",
            title="review the diff",
        )
        await _deliver_and_drain_round_one(client, inbox, server)
        app.state.native_pane_status[PARENT_SESSION_ID] = "idle"
        _parent_status_events()

        publish_pane_status(CHILD_SESSION_ID, "running", None)
        await asyncio.sleep(0)
        assert _parent_status_events() == rearm_events

        # The file reads idle again with no Stop: the re-arm settles.
        publish_pane_status(CHILD_SESSION_ID, "idle", None)
        await _let_settle_run()
        held.release.set()
        await _await_settle(app, CHILD_SESSION_ID)
        assert subagent_work.get_subagent_work(CHILD_SESSION_ID) is None
        assert _parent_status_events() == settle_events
        assert app.state.native_pane_status[PARENT_SESSION_ID] == "idle", (
            "the parent was left waiting on a turn that never happened"
        )


@pytest.mark.asyncio
async def test_new_running_edge_cancels_a_pending_settle(
    _clean_subagent_registry: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real turn that starts while a settle is pending must not be reverted.

    Stale ``busy`` -> ``running`` -> file ``idle`` schedules a settle; the real
    self-resumed turn then publishes ``running`` again and may outlast the grace
    window, so the earlier settle must be cancelled or its ``Stop`` would meet a
    restored drain and be answered "already delivered".
    """
    held = _HeldSleep()
    monkeypatch.setattr(subagent_work, "_wake_retry_sleep", held)
    server = _ChildSnapshotServerClient()
    app, inbox, work_id = _dispatch_worker(server)
    publish_pane_status = _pane_status_publisher(app)

    async with _runner_client(app) as client:
        await _deliver_and_drain_round_one(client, inbox, server)
        publish_pane_status(CHILD_SESSION_ID, "running", None)
        publish_pane_status(CHILD_SESSION_ID, "idle", None)
        await _let_settle_run()
        assert held.calls == [runner_app._SUBAGENT_REARM_SETTLE_GRACE_S]

        # The real self-resumed turn starts before the grace window elapses.
        pending = app.state.rearm_settle_tasks[CHILD_SESSION_ID]
        publish_pane_status(CHILD_SESSION_ID, "running", None)
        await asyncio.wait({pending}, timeout=5)
        assert pending.cancelled()
        held.release.set()
        await asyncio.sleep(0)
        live = subagent_work.get_subagent_work(CHILD_SESSION_ID)
        assert live is not None and live.status == "running", (
            "the pending settle reverted a child that is genuinely working again"
        )
        assert CHILD_SESSION_ID not in subagent_work._drained_delivered_subagent_children

        r2_idle = await _post_status(
            client, status="idle", output="round two: should I open the PR?", turn_completed=True
        )
        assert r2_idle.status_code == 204
        _assert_fresh_result(_drain_queue(inbox), server=server, dispatched_id=work_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("settle_first", [CHILD_SESSION_ID, SECOND_CHILD_SESSION_ID])
async def test_settling_the_last_spurious_rearm_returns_a_waiting_parent_to_idle(
    _clean_subagent_registry: None, monkeypatch: pytest.MonkeyPatch, settle_first: str
) -> None:
    """With two re-armed workers, the parent stays waiting until the last one settles."""
    monkeypatch.setattr(runner_app, "_server_version", "0.16.0")
    held = _HeldSleep()
    monkeypatch.setattr(subagent_work, "_wake_retry_sleep", held)
    server = _ChildSnapshotServerClient()
    app, inbox, _work_id = _dispatch_worker(server)
    subagent_work.register_child_session(
        SECOND_CHILD_SESSION_ID,
        parent_session_id=PARENT_SESSION_ID,
        title="reviewer:review-two",
        tool="reviewer",
        session_name="review-two",
    )
    subagent_work.register_subagent_work(
        parent_session_id=PARENT_SESSION_ID,
        child_session_id=SECOND_CHILD_SESSION_ID,
        agent="reviewer",
        title="second review",
    )
    publish_pane_status = _pane_status_publisher(app)

    async with _runner_client(app) as client:
        await _deliver_and_drain_round_one(client, inbox, server)
        _pin_child_harness(app, SECOND_CHILD_SESSION_ID)
        r = await client.post(
            f"/v1/sessions/{SECOND_CHILD_SESSION_ID}/events",
            json={
                "type": "external_session_status",
                "data": {"status": "idle", "output": "second: done", "turn_completed": True},
            },
        )
        assert r.status_code == 204
        assert inbox.qsize() == 1
        await tool_dispatch._drain_inbox(
            inbox, server_client=server, conversation_id=PARENT_SESSION_ID
        )
        app.state.native_pane_status[PARENT_SESSION_ID] = "idle"
        _parent_status_events()

        publish_pane_status(CHILD_SESSION_ID, "running", None)
        publish_pane_status(SECOND_CHILD_SESSION_ID, "running", None)
        await asyncio.sleep(0)
        assert _parent_status_events() == ["waiting"]

        settle_last = (
            SECOND_CHILD_SESSION_ID if settle_first == CHILD_SESSION_ID else CHILD_SESSION_ID
        )
        publish_pane_status(settle_first, "idle", None)
        await _let_settle_run()
        held.release.set()
        await _await_settle(app, settle_first)
        assert subagent_work.get_subagent_work(settle_first) is None
        assert _parent_status_events() == [], "a live sibling keeps the parent waiting"

        held.release.clear()
        publish_pane_status(settle_last, "idle", None)
        await _let_settle_run()
        held.release.set()
        await _await_settle(app, settle_last)
        assert subagent_work.get_subagent_work(settle_last) is None
        assert _parent_status_events() == ["idle"]


@pytest.mark.asyncio
async def test_settle_returns_a_parent_that_went_waiting_at_its_own_turn_end_to_idle(
    _clean_subagent_registry: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A parent mid-turn during the re-arm derives waiting itself; settling still clears it.

    No status is published for a busy parent when the child re-arms, so the
    rollback must reconcile the parent from its live work rather than from a
    marker written at re-arm time.
    """
    monkeypatch.setattr(runner_app, "_server_version", "0.16.0")
    held = _HeldSleep()
    monkeypatch.setattr(subagent_work, "_wake_retry_sleep", held)
    server = _ChildSnapshotServerClient()
    app, inbox, _work_id = _dispatch_worker(server)
    publish_pane_status = _pane_status_publisher(app)

    async with _runner_client(app) as client:
        await _deliver_and_drain_round_one(client, inbox, server)
        app.state.native_pane_status[PARENT_SESSION_ID] = "running"
        app.state.active_turns[PARENT_SESSION_ID] = None
        _parent_status_events()

        publish_pane_status(CHILD_SESSION_ID, "running", None)
        await asyncio.sleep(0)
        assert _parent_status_events() == [], "a busy parent is left to its own turn end"

        app.state.on_proxy_stream_end(PARENT_SESSION_ID)
        await asyncio.sleep(0)
        assert _parent_status_events() == ["waiting"]

        publish_pane_status(CHILD_SESSION_ID, "idle", None)
        await _let_settle_run()
        held.release.set()
        await _await_settle(app, CHILD_SESSION_ID)
        assert subagent_work.get_subagent_work(CHILD_SESSION_ID) is None
        assert _parent_status_events() == ["idle"]


@pytest.mark.asyncio
async def test_pane_activity_of_another_native_harness_does_not_rearm(
    _clean_subagent_registry: None,
) -> None:
    """Only Claude's status-file edges re-arm through the runner-local publisher.

    A codex-native worker's pane repaint also reaches that publisher as
    ``running`` without marking a new turn, so it must leave a drained dispatch
    alone; its real turns re-arm through the forwarder's ``/events`` edges.
    """
    server = _ChildSnapshotServerClient()
    app, inbox, _work_id = _dispatch_worker(server)
    _pin_child_harness(app, harness="codex-native")
    publish_pane_status = _pane_status_publisher(app)

    async with _runner_client(app) as client:
        await _deliver_and_drain_round_one(client, inbox, server)

        publish_pane_status(CHILD_SESSION_ID, "running", None)
        await asyncio.sleep(0)

    assert subagent_work.get_subagent_work(CHILD_SESSION_ID) is None
    assert CHILD_SESSION_ID in subagent_work._drained_delivered_subagent_children


@pytest.mark.asyncio
async def test_pane_idle_of_another_native_harness_does_not_settle_an_events_rearm(
    _clean_subagent_registry: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-claude worker re-armed on ``/events`` is not rolled back by a pane lull.

    Its pane watcher publishes a quiescence ``idle`` mid-turn and no pane
    ``running`` could cancel a settle, so no settle may be scheduled for it; the
    forwarder's terminal edge settles the dispatch.
    """
    held = _HeldSleep()
    monkeypatch.setattr(subagent_work, "_wake_retry_sleep", held)
    server = _ChildSnapshotServerClient()
    app, inbox, work_id = _dispatch_worker(server)
    _pin_child_harness(app, harness="codex-native")
    publish_pane_status = _pane_status_publisher(app)

    async with _runner_client(app) as client:
        await _deliver_and_drain_round_one(client, inbox, server)
        r2_running = await _post_status(client, status="running")
        assert r2_running.status_code == 204
        live = subagent_work.get_subagent_work(CHILD_SESSION_ID)
        assert live is not None and live.rearmed and live.status == "running"

        publish_pane_status(CHILD_SESSION_ID, "idle", None)
        await _let_settle_run()
        assert held.calls == [] and not app.state.rearm_settle_tasks
        assert subagent_work.get_subagent_work(CHILD_SESSION_ID) is live

        r2_idle = await _post_status(
            client, status="idle", output="round two: should I open the PR?", turn_completed=True
        )
        assert r2_idle.status_code == 204
        _assert_fresh_result(_drain_queue(inbox), server=server, dispatched_id=work_id)


@pytest.mark.asyncio
async def test_follow_up_into_a_selfresumed_turn_is_recovered_after_a_restart(
    _clean_subagent_registry: None,
) -> None:
    """A ``sys_session_send`` into a re-armed turn gets a dispatch id of its own.

    The re-arm reuses round one's id, whose delivery receipt is already on the
    child. Reusing it for the follow-up too would leave ``dispatch_id ==
    delivered_id`` on the child, so a runner restart before the orchestrator
    drains the follow-up's result would skip it as already delivered.
    """
    server = _RecordingServerClient()
    app, inbox, work_id = _dispatch_worker(server)
    server.labels[subagent_work.SUBAGENT_DISPATCH_ID_LABEL_KEY] = work_id
    publish_pane_status = _pane_status_publisher(app)

    async with _runner_client(app) as client:
        await _deliver_and_drain_round_one(client, inbox, server)
        assert server.labels[subagent_work.SUBAGENT_DELIVERED_ID_LABEL_KEY] == work_id

        publish_pane_status(CHILD_SESSION_ID, "running", None)
        handle = await tool_dispatch._send_to_in_flight_child(
            CHILD_SESSION_ID,
            "also check the tests",
            server_client=server,  # type: ignore[arg-type]
            conversation_id=PARENT_SESSION_ID,
            agent="reviewer",
            title="review the diff",
            child_display_title="reviewer:review",
            wrapper_label=None,
        )
        assert json.loads(handle)["status"] == "running"
        live = subagent_work.get_subagent_work(CHILD_SESSION_ID)
        assert live is not None and live.status == "running" and not live.rearmed
        follow_up_id = live.work_id
        assert follow_up_id != work_id, (
            "the follow-up reused round one's receipted dispatch id, so a restart "
            "would skip its unread result"
        )
        assert server.labels[subagent_work.SUBAGENT_DISPATCH_ID_LABEL_KEY] == follow_up_id

        r2 = await _post_status(
            client, status="idle", output="round two: tests pass too", turn_completed=True
        )
        assert r2.status_code == 204
        assert inbox.qsize() == 1

    # The runner restarts before the orchestrator drains that result: the inbox
    # and registries are gone, and recovery reads the child's labels back.
    for registry in (
        subagent_work._subagent_work_by_child,
        subagent_work._subagent_work_by_parent,
        subagent_work._drained_delivered_subagent_children,
        subagent_work._drained_subagent_dispatches,
        subagent_work._child_session_parents,
    ):
        registry.clear()
    recovered_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    subagent_work._session_inboxes_ref[PARENT_SESSION_ID] = recovered_inbox
    server.child_output = "round two: tests pass too"
    woken: list[Any] = []
    await subagent_work._recover_subagent_results_from_server(
        server_client=server,  # type: ignore[arg-type]
        parent_id=PARENT_SESSION_ID,
        schedule_wake=woken.append,
    )
    recovered = _drain_queue(recovered_inbox)
    assert [item["work_id"] for item in recovered] == [follow_up_id]
    assert "round two" in recovered[0]["output"]
    assert len(woken) == 1


@pytest.mark.asyncio
async def test_rebuilt_drained_worker_keeps_its_wrapper_for_cancellation(
    _clean_subagent_registry: None,
) -> None:
    """A drained Claude worker that resumes is still hard-stopped by ``sys_cancel_task``.

    The re-armed entry is rebuilt from the runner's child record, so that record
    must carry the wrapper label; without it ``native_cancel_capability(None)``
    sends ``interrupt``, which ends the turn but leaves the worker alive.
    """
    server = _RecordingServerClient()
    app, inbox, _work_id = _dispatch_worker(server, wrapper_label=CLAUDE_SUBAGENT_WRAPPER)
    publish_pane_status = _pane_status_publisher(app)

    async with _runner_client(app) as client:
        await _deliver_and_drain_round_one(client, inbox, server)
        publish_pane_status(CHILD_SESSION_ID, "running", None)
        live = subagent_work.get_subagent_work(CHILD_SESSION_ID)
        assert live is not None and live.rearmed
        assert live.wrapper_label == CLAUDE_SUBAGENT_WRAPPER

        result = json.loads(
            await tool_dispatch._cancel_subagent_task(
                {"task_id": CHILD_SESSION_ID},
                conversation_id=PARENT_SESSION_ID,
                server_client=server,  # type: ignore[arg-type]
            )
        )

    assert result["cancel_requested"] is True
    child_events = [
        body
        for url, body in server.posts
        if url.endswith(f"/v1/sessions/{CHILD_SESSION_ID}/events")
    ]
    assert child_events == [{"type": "stop_session", "data": {}}], (
        "a Claude worker rebuilt without its wrapper is only interrupted, not stopped"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("drained", [False, True])
async def test_settling_a_stale_replay_restores_a_confirmed_failure(
    _clean_subagent_registry: None, monkeypatch: pytest.MonkeyPatch, drained: bool
) -> None:
    """A ``StopFailure`` the status file lags behind must still read as failed.

    The poller's resync after the failed edge can replay the file's leftover
    ``busy`` as ``running`` (clearing the sticky failure and its stored error)
    and then ``idle`` (completed). When that re-arm settles, the worker's
    confirmed failure must be shown again with its detail, whether or not the
    orchestrator already drained it.
    """
    held = _HeldSleep()
    monkeypatch.setattr(subagent_work, "_wake_retry_sleep", held)
    server = _ChildSnapshotServerClient()
    app, inbox, _work_id = _dispatch_worker(server)
    publish_pane_status = _pane_status_publisher(app)
    failure = {"code": "native_turn_error", "message": "API Error: 500 Internal server error"}

    async with _runner_client(app) as client:
        publish_pane_status(CHILD_SESSION_ID, "running", None)
        r = await _post_status(
            client, status="failed", failure_detail="API Error: 500 Internal server error"
        )
        assert r.status_code == 204
        failed = subagent_work.get_subagent_work(CHILD_SESSION_ID)
        assert failed is not None and failed.status == "failed" and failed.delivered
        if drained:
            await tool_dispatch._drain_inbox(
                inbox,
                server_client=server,  # type: ignore[arg-type]
                conversation_id=PARENT_SESSION_ID,
            )
            assert subagent_work.get_subagent_work(CHILD_SESSION_ID) is None
        assert _child_row_statuses(_pop_stream_events(PARENT_SESSION_ID))[-1] == "failed"
        _pop_stream_events(CHILD_SESSION_ID)

        # The file still reads busy after the StopFailure, then idle.
        publish_pane_status(CHILD_SESSION_ID, "running", None)
        publish_pane_status(CHILD_SESSION_ID, "idle", None)
        await _let_settle_run()
        assert held.calls == [runner_app._SUBAGENT_REARM_SETTLE_GRACE_S]
        held.release.set()
        await _await_settle(app, CHILD_SESSION_ID)

        child_events = [
            e for e in _pop_stream_events(CHILD_SESSION_ID) if e.get("type") == "session.status"
        ]
        assert child_events[-1]["status"] == "failed", (
            "the rolled-back replay left the worker showing completed"
        )
        assert child_events[-1].get("error") == failure, (
            "the restored failure lost the detail the stale running had cleared"
        )
        child_rows = [
            e["child"]
            for e in _pop_stream_events(PARENT_SESSION_ID)
            if e.get("type") == "session.child_session.updated"
        ]
        assert child_rows[-1]["current_task_status"] == "failed"
        assert child_rows[-1].get("last_task_error") == failure
        assert app.state.native_pane_status[CHILD_SESSION_ID] == "failed"
        if drained:
            assert subagent_work.get_subagent_work(CHILD_SESSION_ID) is None
            assert subagent_work._drained_subagent_dispatches[CHILD_SESSION_ID].status == "failed"
        else:
            assert subagent_work.get_subagent_work(CHILD_SESSION_ID) is failed
            assert failed.status == "failed"


def _recovery_with_snapshot(snapshot: Any) -> Any:
    """Build the real recovery helpers over a scripted ``_session_snapshot``."""
    from fastapi import FastAPI

    from omnigent.runner.subagent_recovery import build_subagent_recovery

    subagent_work._session_inboxes_ref.setdefault(PARENT_SESSION_ID, asyncio.Queue())
    return build_subagent_recovery(
        FastAPI(),
        _background_tasks=set(),
        _schedule_subagent_wake=lambda entry, *, is_rewake=False: None,
        _session_inboxes=subagent_work._session_inboxes_ref,
        _session_snapshot=snapshot,
        _session_sub_agent_names={},
        _subagent_recovery_tasks={},
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )


def _child_snapshot(*, ok: bool) -> Any:
    """The child's server snapshot, or a failed read that names no parent."""
    return runner_app._SessionSnapshot(
        ok=ok,
        status_code=200 if ok else 503,
        created_at=0.0,
        workspace=None,
        agent_id="ag_reviewer",
        sub_agent_name="reviewer",
        parent_session_id=PARENT_SESSION_ID if ok else None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("round_one_drained_at_resume", [True, False])
async def test_selfresumed_result_queued_before_restart_is_recovered_when_parent_has_not_drained(
    _clean_subagent_registry: None, round_one_drained_at_resume: bool
) -> None:
    """A self-resumed result waiting in the inbox survives a runner restart.

    The re-arm runs under round one's receipted id, so the result needs an id of
    its own on the child before it enters the volatile inbox; recovery then sees
    ``dispatch_id != delivered_id`` and re-queues exactly that result, whether
    the orchestrator drained round one before the resume or only afterwards.
    """
    server = _RecordingServerClient()
    app, inbox, work_id = _dispatch_worker(server)
    server.labels[subagent_work.SUBAGENT_DISPATCH_ID_LABEL_KEY] = work_id
    publish_pane_status = _pane_status_publisher(app)

    async with _runner_client(app) as client:
        r1 = await _post_status(
            client,
            status="idle",
            output="round one: found the bug",
            turn_completed=True,
            response_id="resp_round_one",
        )
        assert r1.status_code == 204 and inbox.qsize() == 1
        if round_one_drained_at_resume:
            await tool_dispatch._drain_inbox(
                inbox,
                server_client=server,  # type: ignore[arg-type]
                conversation_id=PARENT_SESSION_ID,
            )
        publish_pane_status(CHILD_SESSION_ID, "running", None)
        r2 = await _post_status(
            client,
            status="idle",
            output="round two: should I open the PR?",
            turn_completed=True,
            response_id="resp_round_two",
        )
        assert r2.status_code == 204
        resumed_id = server.labels[subagent_work.SUBAGENT_DISPATCH_ID_LABEL_KEY]
        assert resumed_id != work_id, "the resumed turn kept round one's receipted id"
        if not round_one_drained_at_resume:
            # The orchestrator reads round one only now; round two stays queued.
            round_one = inbox.get_nowait()
            assert round_one["work_id"] == work_id
            await tool_dispatch._cleanup_drained_subagent_work(
                round_one,
                server_client=server,  # type: ignore[arg-type]
            )
        assert inbox.qsize() == 1
        assert server.labels[subagent_work.SUBAGENT_DELIVERED_ID_LABEL_KEY] == work_id

    # The runner restarts before the orchestrator drains round two.
    for registry in (
        subagent_work._subagent_work_by_child,
        subagent_work._subagent_work_by_parent,
        subagent_work._drained_delivered_subagent_children,
        subagent_work._drained_subagent_dispatches,
        subagent_work._child_session_parents,
    ):
        registry.clear()
    recovered_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    subagent_work._session_inboxes_ref[PARENT_SESSION_ID] = recovered_inbox
    server.child_output = "round two: should I open the PR?"
    woken: list[Any] = []
    await subagent_work._recover_subagent_results_from_server(
        server_client=server,  # type: ignore[arg-type]
        parent_id=PARENT_SESSION_ID,
        schedule_wake=woken.append,
    )
    recovered = _drain_queue(recovered_inbox)
    assert [item["work_id"] for item in recovered] == [resumed_id], (
        "the unread self-resumed result was skipped as already delivered"
    )
    assert "round two" in recovered[0]["output"]
    assert len(woken) == 1


@pytest.mark.asyncio
async def test_failed_edge_rearms_the_poller_only_for_a_dispatched_child(
    _clean_subagent_registry: None,
) -> None:
    """A ``StopFailure`` re-reads the status file for a dispatched child, not a top-level session.

    A child's stale ``busy`` replay is settled and its failure restored; a
    top-level session has no settle, so replaying its stale ``busy`` would clear
    the server's sticky failure for good. A ``Stop`` still re-arms both.
    """

    class _FakePoller:
        def __init__(self) -> None:
            self.resyncs = 0

        def resync(self) -> None:
            self.resyncs += 1

    server = _ChildSnapshotServerClient()
    app, _inbox, _work_id = _dispatch_worker(server)
    registry = app.state.session_resource_registry
    child_poller, top_level_poller = _FakePoller(), _FakePoller()
    registry._status_pollers[CHILD_SESSION_ID] = child_poller
    registry._status_pollers["conv_top_level"] = top_level_poller

    async with _runner_client(app) as client:
        r = await _post_status(client, status="failed", output="Error: API 500")
        assert r.status_code == 204
        assert child_poller.resyncs == 1
        r = await client.post(
            "/v1/sessions/conv_top_level/events",
            json={"type": "external_session_status", "data": {"status": "failed"}},
        )
        assert r.status_code == 204
        assert top_level_poller.resyncs == 0, "a top-level failure must not replay a stale busy"
        r = await client.post(
            "/v1/sessions/conv_top_level/events",
            json={
                "type": "external_session_status",
                "data": {"status": "idle", "turn_completed": True},
            },
        )
        assert r.status_code == 204
        assert top_level_poller.resyncs == 1


@pytest.mark.asyncio
async def test_a_rebuild_that_fails_keeps_the_settled_drain_replay_guard(
    _clean_subagent_registry: None,
) -> None:
    """A late report whose rebuild fails must not spend the drain's replay protection.

    With no local child record the rebuild reads the server snapshot. When that
    read fails the tombstone must stay, so a replay of the drained turn's own
    report is still absorbed and the retried late report is rebuilt exactly once.
    """
    subagent_work._drained_delivered_subagent_children.add(CHILD_SESSION_ID)
    subagent_work._drained_subagent_dispatches[CHILD_SESSION_ID] = (
        subagent_work._FinishedSubagentDispatch(
            work_id="subagent_round_one",
            status="completed",
            response_id="resp_round_one",
            rearmed=True,
        )
    )
    reads: list[str] = []

    async def snapshot(session_id: str) -> Any:
        reads.append(session_id)
        return _child_snapshot(ok=len(reads) > 1)

    ensure = _recovery_with_snapshot(snapshot).ensure_subagent_work_entry

    # The late Stop's first delivery attempt cannot rebuild the entry.
    assert await ensure(CHILD_SESSION_ID, report_response_id="resp_round_two") is None
    assert CHILD_SESSION_ID in subagent_work._drained_delivered_subagent_children, (
        "a failed rebuild dropped the tombstone that absorbs replays"
    )
    # Round one's own Stop replayed meanwhile is still a duplicate.
    assert await ensure(CHILD_SESSION_ID, report_response_id="resp_round_one") is None
    # The forwarder retries the late Stop once the snapshot reads again.
    rebuilt = await ensure(CHILD_SESSION_ID, report_response_id="resp_round_two")
    assert rebuilt is not None and rebuilt.work_id == "subagent_round_one" and rebuilt.rearmed
    assert rebuilt.rearmed_over == subagent_work._FinishedSubagentDispatch(
        work_id="subagent_round_one",
        status="completed",
        response_id="resp_round_one",
        rearmed=True,
    ), "the rebuilt entry must still know the finished turn a replay would repeat"
    assert CHILD_SESSION_ID not in subagent_work._drained_delivered_subagent_children
    assert CHILD_SESSION_ID not in subagent_work._drained_subagent_dispatches


@pytest.mark.asyncio
async def test_a_forgotten_drain_still_absorbs_a_replay_of_its_own_report(
    _clean_subagent_registry: None,
) -> None:
    """A stale ``running`` with no local record forgets the tombstone, not the dedup.

    The drained dispatch record stays behind, so a replay of that turn's own
    report (same ``response_id``) is still refused, while another turn's report
    is rebuilt from the snapshot as the re-arm it confirms.
    """
    subagent_work._drained_subagent_dispatches[CHILD_SESSION_ID] = (
        subagent_work._FinishedSubagentDispatch(
            work_id="subagent_round_one", status="completed", response_id="resp_round_one"
        )
    )

    async def snapshot(session_id: str) -> Any:
        del session_id
        return _child_snapshot(ok=True)

    ensure = _recovery_with_snapshot(snapshot).ensure_subagent_work_entry

    assert await ensure(CHILD_SESSION_ID, report_response_id="resp_round_one") is None
    rebuilt = await ensure(CHILD_SESSION_ID, report_response_id="resp_round_two")
    assert rebuilt is not None and rebuilt.work_id == "subagent_round_one"
    assert rebuilt.rearmed, "the report must be able to stamp the new turn's own id"
    assert rebuilt.rearmed_over == subagent_work._FinishedSubagentDispatch(
        work_id="subagent_round_one", status="completed", response_id="resp_round_one"
    )


@pytest.mark.asyncio
async def test_a_replay_of_the_drained_turn_does_not_confirm_a_live_rearm(
    _clean_subagent_registry: None,
) -> None:
    """Round one's own ``Stop`` re-sent while the re-arm is live is not the new result.

    The forwarder retries a ``Stop`` whose acknowledgement was lost. Landing
    inside a live re-arm it must stay a duplicate: delivering it would hand the
    orchestrator round one twice and mark the re-arm delivered, so the real
    self-resumed result would then be answered "already delivered".
    """
    server = _RecordingServerClient()
    app, inbox, work_id = _dispatch_worker(server)
    publish_pane_status = _pane_status_publisher(app)

    async with _runner_client(app) as client:
        r1 = await _post_status(
            client,
            status="idle",
            output="round one: found the bug",
            turn_completed=True,
            response_id="resp_round_one",
        )
        assert r1.status_code == 204
        await tool_dispatch._drain_inbox(
            inbox,
            server_client=server,  # type: ignore[arg-type]
            conversation_id=PARENT_SESSION_ID,
        )
        publish_pane_status(CHILD_SESSION_ID, "running", None)

        replay = await _post_status(
            client,
            status="idle",
            output="round one: found the bug",
            turn_completed=True,
            response_id="resp_round_one",
        )
        assert replay.status_code == 204
        assert inbox.empty(), "the replayed Stop was delivered as the re-armed turn's result"
        live = subagent_work.get_subagent_work(CHILD_SESSION_ID)
        assert live is not None and live.status == "running" and live.rearmed
        assert subagent_work.SUBAGENT_DISPATCH_ID_LABEL_KEY not in server.labels

        r2 = await _post_status(
            client,
            status="idle",
            output="round two: should I open the PR?",
            turn_completed=True,
            response_id="resp_round_two",
        )
        assert r2.status_code == 204
        _assert_fresh_result(_drain_queue(inbox), server=server, dispatched_id=work_id)


@pytest.mark.asyncio
async def test_an_unnamed_report_inside_a_live_rearm_is_taken_as_the_resumed_turns_result(
    _clean_subagent_registry: None,
) -> None:
    """A report naming no turn confirms a live re-arm: delivered once, under a fresh id.

    A forwarder that names no turn leaves the runner unable to tell a replay of
    round one from the resumed turn's result, so the unnamed report is taken as
    that result (a duplicate beats a loss) and a repeat of it is absorbed. The
    cost is stated on ``_rearm_confirmed_by``: a real ``Stop`` landing after such
    a replay with no ``running`` edge in between is answered already delivered.
    """
    server = _RecordingServerClient()
    app, inbox, work_id = _dispatch_worker(server)
    publish_pane_status = _pane_status_publisher(app)

    async with _runner_client(app) as client:
        await _deliver_and_drain_round_one(client, inbox, server)
        publish_pane_status(CHILD_SESSION_ID, "running", None)

        unnamed = await _post_status(
            client, status="idle", output="round one: found the bug", turn_completed=True
        )
        assert unnamed.status_code == 204
        [item] = _drain_queue(inbox)
        stamped = server.labels.get(subagent_work.SUBAGENT_DISPATCH_ID_LABEL_KEY)
        assert item["work_id"] == stamped != work_id, "the unnamed report confirmed no re-arm"
        finished = subagent_work.get_subagent_work(CHILD_SESSION_ID)
        assert finished is not None and finished.status == "completed" and not finished.rearmed

        repeat = await _post_status(
            client, status="idle", output="round one: found the bug", turn_completed=True
        )
        assert repeat.status_code == 204
        assert inbox.empty(), "a repeat of the unnamed report was delivered twice"
        assert server.labels.get(subagent_work.SUBAGENT_DISPATCH_ID_LABEL_KEY) == stamped


class _StampFailingServerClient(_RecordingServerClient):
    """Reject dispatch-id stamps while still receipting drains."""

    class _Rejected:
        status_code = 503
        text = "label store unavailable"

    async def patch(self, url: str, **kwargs: Any) -> Any:
        body = kwargs.get("json") or {}
        if subagent_work.SUBAGENT_DISPATCH_ID_LABEL_KEY in (body.get("labels") or {}):
            return self._Rejected()
        return await super().patch(url, **kwargs)


@pytest.mark.asyncio
async def test_a_failed_dispatch_stamp_keeps_the_receipted_id(
    _clean_subagent_registry: None,
) -> None:
    """When the fresh id cannot be stamped, the result keeps the id the child carries.

    Delivering under an id the child never received would leave
    ``dispatch_id != delivered_id`` after the drain and make restart recovery
    re-queue the drained result; keeping the receipted id loses only the
    pre-drain recovery of this one turn, which the stamp failure lost anyway.
    """
    server = _StampFailingServerClient()
    app, inbox, work_id = _dispatch_worker(server)
    server.labels[subagent_work.SUBAGENT_DISPATCH_ID_LABEL_KEY] = work_id
    publish_pane_status = _pane_status_publisher(app)

    async with _runner_client(app) as client:
        await _deliver_and_drain_round_one(client, inbox, server)
        publish_pane_status(CHILD_SESSION_ID, "running", None)
        r2 = await _post_status(
            client,
            status="idle",
            output="round two: should I open the PR?",
            turn_completed=True,
            response_id="resp_round_two",
        )
        assert r2.status_code == 204
        [second] = _drain_queue(inbox)
        assert second["work_id"] == work_id
        assert server.labels[subagent_work.SUBAGENT_DISPATCH_ID_LABEL_KEY] == work_id
        await tool_dispatch._cleanup_drained_subagent_work(
            second,
            server_client=server,  # type: ignore[arg-type]
        )

    assert server.labels[subagent_work.SUBAGENT_DELIVERED_ID_LABEL_KEY] == work_id
    assert subagent_work.undelivered_subagent_dispatch_id(server.labels) is None, (
        "a restart would re-queue the drained result"
    )


@pytest.mark.asyncio
async def test_the_confirmation_stamp_waits_for_a_follow_up_holding_the_send_lock(
    _clean_subagent_registry: None,
) -> None:
    """The confirming ``Stop`` and a follow-up ``sys_session_send`` cannot stamp different ids.

    Both decide the re-armed entry's identity, so the Stop's check-and-stamp runs
    under the same per-child send lock the follow-up holds while it classifies
    and re-stamps the entry.
    """
    server = _RecordingServerClient()
    app, inbox, work_id = _dispatch_worker(server)
    server.labels[subagent_work.SUBAGENT_DISPATCH_ID_LABEL_KEY] = work_id
    publish_pane_status = _pane_status_publisher(app)

    async with _runner_client(app) as client:
        await _deliver_and_drain_round_one(client, inbox, server)
        publish_pane_status(CHILD_SESSION_ID, "running", None)

        lock = subagent_work._in_flight_send_lock(CHILD_SESSION_ID)
        await lock.acquire()
        stop = asyncio.create_task(
            _post_status(
                client,
                status="idle",
                output="round two: should I open the PR?",
                turn_completed=True,
                response_id="resp_round_two",
            )
        )
        await asyncio.sleep(0.05)
        assert not stop.done()
        assert server.labels[subagent_work.SUBAGENT_DISPATCH_ID_LABEL_KEY] == work_id, (
            "the Stop stamped the child while a follow-up held the send lock"
        )
        assert inbox.empty()
        lock.release()
        r2 = await stop
        assert r2.status_code == 204
        _assert_fresh_result(_drain_queue(inbox), server=server, dispatched_id=work_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("deleted", [CHILD_SESSION_ID, PARENT_SESSION_ID])
async def test_deleting_a_session_cancels_its_pending_rearm_settles(
    _clean_subagent_registry: None, monkeypatch: pytest.MonkeyPatch, deleted: str
) -> None:
    """Tearing down the child or its parent drops the child's pending settle task."""
    held = _HeldSleep()
    monkeypatch.setattr(subagent_work, "_wake_retry_sleep", held)
    server = _ChildSnapshotServerClient()
    app, inbox, _work_id = _dispatch_worker(server)
    publish_pane_status = _pane_status_publisher(app)

    async with _runner_client(app) as client:
        await _deliver_and_drain_round_one(client, inbox, server)
        publish_pane_status(CHILD_SESSION_ID, "running", None)
        publish_pane_status(CHILD_SESSION_ID, "idle", None)
        await _let_settle_run()
        pending = app.state.rearm_settle_tasks[CHILD_SESSION_ID]

        r = await client.delete(f"/v1/sessions/{deleted}")
        assert r.status_code < 300, r.text
        await asyncio.sleep(0)
        assert pending.cancelled(), "the settle task outlived the session"
        assert CHILD_SESSION_ID not in app.state.rearm_settle_tasks


@pytest.mark.asyncio
async def test_concurrent_terminal_reports_stamp_the_child_once(
    _clean_subagent_registry: None,
) -> None:
    """Two reports racing for one re-armed turn stamp one id and deliver one result.

    The forwarder can re-send a Stop whose acknowledgement was lost while the
    first copy is still being recorded. Check, stamp and record run under the
    child's send lock, so the second copy finds the re-arm already consumed
    instead of stamping a second id the entry never takes.
    """
    server = _RecordingServerClient()
    app, inbox, work_id = _dispatch_worker(server)
    server.labels[subagent_work.SUBAGENT_DISPATCH_ID_LABEL_KEY] = work_id
    publish_pane_status = _pane_status_publisher(app)

    async with _runner_client(app) as client:
        await _deliver_and_drain_round_one(client, inbox, server)
        publish_pane_status(CHILD_SESSION_ID, "running", None)
        stop = {
            "status": "idle",
            "output": "round two: should I open the PR?",
            "turn_completed": True,
            "response_id": "resp_round_two",
        }
        first, second = await asyncio.gather(
            _post_status(client, **stop), _post_status(client, **stop)
        )
        assert first.status_code == 204 and second.status_code == 204

    items = _drain_queue(inbox)
    assert len(items) == 1
    stamps = [p for p in server.patches if subagent_work.SUBAGENT_DISPATCH_ID_LABEL_KEY in p]
    assert len(stamps) == 1, "a concurrent copy of the report stamped a second dispatch id"
    assert items[0]["work_id"] == server.labels[subagent_work.SUBAGENT_DISPATCH_ID_LABEL_KEY]
    assert items[0]["work_id"] != work_id


@pytest.mark.asyncio
async def test_a_follow_up_whose_stamp_fails_keeps_the_receipted_id(
    _clean_subagent_registry: None,
) -> None:
    """A follow-up whose fresh id cannot be stamped leaves entry and child on one id.

    The id is persisted before the entry takes it, so a rejected stamp keeps the
    receipted id on both sides: the follow-up's result is delivered live and its
    drain receipt matches the child's label, so a restart neither duplicates
    nor misattributes it (it cannot recover it either, as before).
    """
    server = _StampFailingServerClient()
    app, inbox, work_id = _dispatch_worker(server)
    server.labels[subagent_work.SUBAGENT_DISPATCH_ID_LABEL_KEY] = work_id
    publish_pane_status = _pane_status_publisher(app)

    async with _runner_client(app) as client:
        await _deliver_and_drain_round_one(client, inbox, server)
        publish_pane_status(CHILD_SESSION_ID, "running", None)
        handle = await tool_dispatch._send_to_in_flight_child(
            CHILD_SESSION_ID,
            "also check the tests",
            server_client=server,  # type: ignore[arg-type]
            conversation_id=PARENT_SESSION_ID,
            agent="reviewer",
            title="review the diff",
            child_display_title="reviewer:review",
            wrapper_label=None,
        )
        assert json.loads(handle)["status"] == "running"
        live = subagent_work.get_subagent_work(CHILD_SESSION_ID)
        assert live is not None and live.work_id == work_id, (
            "the entry took an id the child never received"
        )
        assert server.labels[subagent_work.SUBAGENT_DISPATCH_ID_LABEL_KEY] == work_id
        assert not live.rearmed, (
            "the parent's follow-up confirms the turn; it is no longer provisional"
        )
        # A stale idle read must not schedule a settle against the steered turn.
        publish_pane_status(CHILD_SESSION_ID, "idle", None)
        await _let_settle_run()
        assert not app.state.rearm_settle_tasks
        assert subagent_work.get_subagent_work(CHILD_SESSION_ID) is live

        r2 = await _post_status(
            client,
            status="idle",
            output="round two: tests pass too",
            turn_completed=True,
            response_id="resp_round_two",
        )
        assert r2.status_code == 204
        [second] = _drain_queue(inbox)
        assert second["work_id"] == work_id
        await tool_dispatch._cleanup_drained_subagent_work(
            second,
            server_client=server,  # type: ignore[arg-type]
        )

    assert subagent_work.undelivered_subagent_dispatch_id(server.labels) is None


class _BlockingStampServerClient(_RecordingServerClient):
    """Hold every dispatch-id stamp until the test releases it."""

    def __init__(self) -> None:
        super().__init__()
        self.release_stamps = asyncio.Event()

    async def patch(self, url: str, **kwargs: Any) -> Any:
        body = kwargs.get("json") or {}
        if subagent_work.SUBAGENT_DISPATCH_ID_LABEL_KEY in (body.get("labels") or {}):
            await self.release_stamps.wait()
        return await super().patch(url, **kwargs)


def _wake_posts(server: _RecordingServerClient) -> list[Any]:
    """The orchestrator wake notices the runner posted."""
    return [
        body
        for url, body in server.posts
        if url.endswith(f"/v1/sessions/{PARENT_SESSION_ID}/events")
        and isinstance(body, dict)
        and body.get("type") == "message"
    ]


@pytest.mark.asyncio
async def test_terminal_stamp_racing_rearm_settle_delivers_once(
    _clean_subagent_registry: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The grace window ending while the Stop's stamp is in flight must not lose the result.

    The settle takes the same per-child lock as the report, so it runs either
    before the report (which then rebuilds the entry it owes) or after it (and
    finds the turn confirmed); it can no longer tombstone the live entry between
    the stamp and the record and leave the report answered "already delivered".
    """
    held = _HeldSleep()
    monkeypatch.setattr(subagent_work, "_wake_retry_sleep", held)
    server = _BlockingStampServerClient()
    app, inbox, work_id = _dispatch_worker(server)
    publish_pane_status = _pane_status_publisher(app)

    async with _runner_client(app) as client:
        await _deliver_and_drain_round_one(client, inbox, server)
        # The orchestrator acted on round one's wake notice; a new result wakes it again.
        app.state.subagent_wake_pending.discard(PARENT_SESSION_ID)
        wakes_before = len(_wake_posts(server))
        publish_pane_status(CHILD_SESSION_ID, "running", None)
        publish_pane_status(CHILD_SESSION_ID, "idle", None)
        await _let_settle_run()
        assert held.calls == [runner_app._SUBAGENT_REARM_SETTLE_GRACE_S]

        stop = asyncio.create_task(
            _post_status(
                client,
                status="idle",
                output="round two: should I open the PR?",
                turn_completed=True,
                response_id="resp_round_two",
            )
        )
        await asyncio.sleep(0.05)
        assert not stop.done(), "the Stop should be waiting on its stamp"
        # The grace window ends while the stamp is still in flight.
        held.release.set()
        await asyncio.sleep(0.05)
        server.release_stamps.set()
        r2 = await stop
        assert r2.status_code == 204
        await _await_settle(app, CHILD_SESSION_ID)
        for _ in range(10):
            await asyncio.sleep(0)

        _assert_fresh_result(_drain_queue(inbox), server=server, dispatched_id=work_id)
        assert len(_wake_posts(server)) == wakes_before + 1, "the orchestrator was not woken once"

    live = subagent_work.get_subagent_work(CHILD_SESSION_ID)
    assert live is not None and live.status == "completed" and live.delivered
    assert CHILD_SESSION_ID not in subagent_work._drained_delivered_subagent_children


@pytest.mark.asyncio
async def test_follow_up_restamp_racing_rearm_settle_keeps_the_turn(
    _clean_subagent_registry: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The grace window ending during a follow-up's stamp must not revert the steered turn.

    The follow-up holds the per-child lock across its stamp, the settle waits
    for it, and by then the follow-up has confirmed the turn, so the settle
    finds nothing provisional to undo.
    """
    held = _HeldSleep()
    monkeypatch.setattr(subagent_work, "_wake_retry_sleep", held)
    server = _BlockingStampServerClient()
    app, inbox, work_id = _dispatch_worker(server)
    server.labels[subagent_work.SUBAGENT_DISPATCH_ID_LABEL_KEY] = work_id
    publish_pane_status = _pane_status_publisher(app)

    async with _runner_client(app) as client:
        await _deliver_and_drain_round_one(client, inbox, server)
        publish_pane_status(CHILD_SESSION_ID, "running", None)
        publish_pane_status(CHILD_SESSION_ID, "idle", None)
        await _let_settle_run()

        follow_up = asyncio.create_task(
            tool_dispatch._send_to_in_flight_child(
                CHILD_SESSION_ID,
                "also check the tests",
                server_client=server,  # type: ignore[arg-type]
                conversation_id=PARENT_SESSION_ID,
                agent="reviewer",
                title="review the diff",
                child_display_title="reviewer:review",
                wrapper_label=None,
            )
        )
        await asyncio.sleep(0.05)
        assert not follow_up.done(), "the follow-up should be waiting on its stamp"
        held.release.set()
        await asyncio.sleep(0.05)
        server.release_stamps.set()
        assert json.loads(await follow_up)["status"] == "running"
        await _await_settle(app, CHILD_SESSION_ID)

        live = subagent_work.get_subagent_work(CHILD_SESSION_ID)
        assert live is not None and live.status == "running" and not live.rearmed, (
            "the settle reverted a turn the orchestrator had just steered"
        )
        assert live.work_id == server.labels[subagent_work.SUBAGENT_DISPATCH_ID_LABEL_KEY]
        assert live.work_id != work_id
        assert CHILD_SESSION_ID not in subagent_work._drained_delivered_subagent_children

        r2 = await _post_status(
            client,
            status="idle",
            output="round two: tests pass too",
            turn_completed=True,
            response_id="resp_round_two",
        )
        assert r2.status_code == 204
        [second] = _drain_queue(inbox)
        assert second["work_id"] == live.work_id


@pytest.mark.asyncio
async def test_unregistering_a_child_keeps_a_send_lock_someone_holds(
    _clean_subagent_registry: None,
) -> None:
    """A child's send lock survives the drain's cleanup while anyone holds or awaits it.

    ``asyncio.Lock.locked()`` reads ``False`` between a release and the woken
    waiter's re-acquire, so a guarded waiter must count as a user too; the lock
    is forgotten once nobody relies on it and the child has no entry left.
    """
    subagent_work.register_child_session(
        CHILD_SESSION_ID,
        parent_session_id=PARENT_SESSION_ID,
        title="reviewer:review",
        tool="reviewer",
        session_name="review",
    )
    subagent_work.register_subagent_work(
        parent_session_id=PARENT_SESSION_ID,
        child_session_id=CHILD_SESSION_ID,
        agent="reviewer",
        title="review the diff",
    )
    lock = subagent_work._in_flight_send_lock(CHILD_SESSION_ID)
    await lock.acquire()
    entered = asyncio.Event()
    proceed = asyncio.Event()

    async def _guarded_user() -> None:
        async with subagent_work.in_flight_send_guard(CHILD_SESSION_ID):
            entered.set()
            await proceed.wait()

    waiter = asyncio.create_task(_guarded_user())
    await asyncio.sleep(0)
    assert not entered.is_set(), "the guarded user should be waiting on the held lock"

    subagent_work.unregister_subagent_work(CHILD_SESSION_ID)
    assert subagent_work._in_flight_send_lock(CHILD_SESSION_ID) is lock, (
        "the cleanup minted a second lock while the first was held"
    )
    lock.release()
    await entered.wait()
    assert subagent_work._in_flight_send_lock(CHILD_SESSION_ID) is lock, (
        "the cleanup dropped a lock a waiter was about to take"
    )
    proceed.set()
    await waiter
    assert CHILD_SESSION_ID not in subagent_work._in_flight_send_locks, (
        "the last user left an unregistered child's lock behind"
    )


@pytest.mark.asyncio
async def test_deleting_the_parent_forgets_its_children_s_failure_details(
    _clean_subagent_registry: None,
) -> None:
    """The per-child failure memo does not outlive a parent torn down with its children.

    The failed result is drained first, so the child has already left the work
    registry when the parent goes; the parent's child records still name it.
    """
    server = _ChildSnapshotServerClient()
    app, inbox, _work_id = _dispatch_worker(server)

    async with _runner_client(app) as client:
        r = await _post_status(client, status="failed", failure_detail="API Error: 500")
        assert r.status_code == 204
        assert CHILD_SESSION_ID in app.state.native_pane_failures
        await tool_dispatch._drain_inbox(
            inbox,
            server_client=server,  # type: ignore[arg-type]
            conversation_id=PARENT_SESSION_ID,
        )
        assert subagent_work.get_subagent_work(CHILD_SESSION_ID) is None
        assert CHILD_SESSION_ID in subagent_work._drained_delivered_subagent_children
        r = await client.delete(f"/v1/sessions/{PARENT_SESSION_ID}")
        assert r.status_code < 300, r.text

    assert CHILD_SESSION_ID not in app.state.native_pane_failures
    assert CHILD_SESSION_ID not in subagent_work._drained_delivered_subagent_children
    assert CHILD_SESSION_ID not in subagent_work._drained_subagent_dispatches
    assert CHILD_SESSION_ID not in subagent_work._child_session_parents
