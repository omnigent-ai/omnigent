"""A worker's self-resumed turn must still reach the orchestrator.

Ordering invariant: a drained or delivered dispatch is re-armed by the child's
own ``running`` edge, under the dispatch id stamped on the child; the next turn
end is delivered as a fresh result, while a trailing ``idle`` with no new
``running`` stays deduplicated. The tests drive the real ``/events`` handler, the
runner-local status publisher the status-file poller uses, and the real
``sys_read_inbox`` drain.
"""

from __future__ import annotations

import asyncio
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


_REGISTRY_MAPS = (
    (subagent_work, "_subagent_work_by_child"),
    (subagent_work, "_subagent_work_by_parent"),
    (subagent_work, "_session_inboxes_ref"),
    (runner_app, "_session_event_queues_ref"),
    (subagent_work, "_child_session_parents"),
    (subagent_work, "_drained_delivered_subagent_children"),
    (subagent_work, "_drained_subagent_work_ids"),
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
    """Serve the child's sub-agent snapshot and ALLOW the inbox-drain policy.

    ``GET /v1/sessions/{child}`` returns the child ``SessionResponse`` (parent
    link + ``sub_agent_name``) the runner reads to rebuild a lost work entry.
    ``POST .../policies/evaluate`` returns ``ALLOW`` so the real ``sys_read_inbox``
    drain formats and cleans up the delivered item rather than re-queuing it.
    """

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


_HARNESS_BY_AGENT = {"ag_sdk_parent": "claude-sdk", "ag_native_parent": "claude-native"}


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
    await asyncio.wait({task}, timeout=5)


def _dispatch_worker(
    server: NullServerClient,
) -> tuple[Any, asyncio.Queue[dict[str, Any]], str]:
    """Build the runner app with the orchestrator's inbox and one dispatched worker.

    Mirrors what ``sys_session_send`` leaves behind: the child->parent record
    and the dispatch entry.

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
    )
    entry = subagent_work.register_subagent_work(
        parent_session_id=PARENT_SESSION_ID,
        child_session_id=CHILD_SESSION_ID,
        agent="reviewer",
        title="review the diff",
    )
    _pin_child_harness(app)
    return app, inbox, entry.work_id


def _pane_status_publisher(app: Any) -> Any:
    """The runner-local publisher the status-file poller and pane watcher call."""
    publish = app.state.session_resource_registry._session_status_publisher
    assert publish is not None
    return publish


async def _post_status(
    client: Any, *, status: str, output: str | None = None, turn_completed: bool | None = None
) -> Any:
    """POST an ``external_session_status`` edge to the child, as the forwarder does.

    A claude-native ``Stop`` carries ``turn_completed``; a bare ``idle`` is the
    pane watcher's quiescence edge and is never a completion on its own.
    """
    data: dict[str, Any] = {"status": status}
    if output is not None:
        data["output"] = output
    if turn_completed is not None:
        data["turn_completed"] = turn_completed
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


def _parent_status_events() -> list[str]:
    """Pop the ``session.status`` values published on the orchestrator's own stream."""
    queue = runner_app._session_event_queues_ref.get(PARENT_SESSION_ID)
    statuses: list[str] = []
    while queue is not None and not queue.empty():
        event = queue.get_nowait()
        if isinstance(event, dict) and event.get("type") == "session.status":
            statuses.append(str(event.get("status")))
    return statuses


def _assert_fresh_result(second: list[dict[str, Any]], *, work_id: str) -> None:
    assert second, (
        "the worker's self-resumed turn was invisible to the orchestrator: its "
        "external_session_status:idle produced no inbox item because the child was "
        "still remembered as drained, so mark_subagent_work_terminal answered "
        "'already delivered'."
    )
    assert second[0]["type"] == "sub_agent"
    assert second[0]["status"] == "completed"
    assert second[0]["work_id"] == work_id, (
        "the self-resumed result must carry the dispatch id stamped on the child; "
        "a fresh id would make the drain receipt disagree with the child's label "
        "and re-deliver this result after a runner restart"
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

    _assert_fresh_result(second, work_id=work_id)


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

    _assert_fresh_result(second, work_id=work_id)


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

    _assert_fresh_result(second, work_id=work_id)
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
        assert subagent_work._drained_subagent_work_ids[CHILD_SESSION_ID] == work_id
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
        _assert_fresh_result(second, work_id=work_id)

        held.release.set()
        await _await_settle(app, CHILD_SESSION_ID)
        settled = subagent_work.get_subagent_work(CHILD_SESSION_ID)
        assert settled is not None and settled.status == "completed" and settled.delivered
        assert not settled.rearmed
        assert CHILD_SESSION_ID not in subagent_work._drained_delivered_subagent_children


@pytest.mark.asyncio
async def test_spurious_running_edge_restores_a_delivered_result_the_parent_has_not_read(
    _clean_subagent_registry: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round one was delivered but not drained when a stale ``busy`` re-armed the child.

    Settling puts the delivered entry back, so the orchestrator's later drain
    still finds the dispatch it is receipting and a duplicate report of round
    one is still answered as already delivered.
    """
    held = _HeldSleep()
    monkeypatch.setattr(subagent_work, "_wake_retry_sleep", held)
    server = _ChildSnapshotServerClient()
    app, inbox, work_id = _dispatch_worker(server)
    publish_pane_status = _pane_status_publisher(app)

    async with _runner_client(app) as client:
        r1 = await _post_status(
            client, status="idle", output="round one: found the bug", turn_completed=True
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
            client, status="idle", output="round one: found the bug", turn_completed=True
        )
        assert r_dup.status_code == 204
        assert inbox.qsize() == 1, "the duplicate report must not deliver round one twice"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent_id", "expected"),
    [("ag_sdk_parent", ["waiting"]), ("ag_native_parent", [])],
)
async def test_selfresume_waiting_edge_follows_the_parent_harness(
    _clean_subagent_registry: None,
    monkeypatch: pytest.MonkeyPatch,
    agent_id: str,
    expected: list[str],
) -> None:
    """An idle SDK parent is shown waiting; a native parent's status stays with its terminal.

    The parent's harness comes from its initialized spec, as in production:
    ``_publish_turn_status`` publishes for an SDK parent and defers to the
    terminal-owned status of a native one.
    """
    monkeypatch.setattr(runner_app, "_server_version", "0.16.0")
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

    assert _parent_status_events() == expected


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
        _assert_fresh_result(_drain_queue(inbox), work_id=work_id)


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
        _assert_fresh_result(_drain_queue(inbox), work_id=work_id)
