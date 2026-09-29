"""A claude-native worker's self-resumed turn must still reach the orchestrator.

An orchestrator dispatches a claude-native worker with ``sys_session_send``,
which calls ``register_subagent_work``. The worker finishes its first turn; the
forwarder POSTs ``external_session_status: idle`` to the child's ``/events``; the
runner delivers a ``sub_agent`` item to the parent's inbox; and the orchestrator
drains it with ``sys_read_inbox``. Draining a delivered result remembers the
child in ``_drained_delivered_subagent_children``
(``unregister_subagent_work(remember_drained_delivery=True)``) so a *duplicate*
report of that same turn is acknowledged as already delivered instead of
re-queued.

Claude Code can then resume the same worker on its own -- a background task or an
internal sub-agent hands back -- without the orchestrator sending anything. No
``sys_session_send`` runs, so only the child's own new activity can clear the
drained memory. When the self-resumed turn ends, its ``external_session_status:
idle`` edge must be delivered to the parent as a fresh result, under the dispatch
id stamped on the child, instead of being mistaken for a duplicate of the
already-drained turn. While that turn runs, the worker counts as a running child
of the orchestrator, so the orchestrator reads ``waiting`` rather than idle.

The tests drive the real runner app's ``/events`` handler, the runner-local
status publisher that the claude-native status-file poller and pane watcher use,
and the real ``sys_read_inbox`` drain path.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest

from omnigent.runner import app as runner_app
from omnigent.runner import create_runner_app, tool_dispatch
from omnigent.spec.types import AgentSpec, ExecutorSpec
from tests.runner.conftest import _FakeProcessManager, _runner_client, _ScriptedHarnessClient
from tests.runner.helpers import NullServerClient

PARENT_SESSION_ID = "conv_parent_orchestrator"
CHILD_SESSION_ID = "conv_child_reviewer"


_REGISTRY_MAPS = (
    "_subagent_work_by_child",
    "_subagent_work_by_parent",
    "_session_inboxes_ref",
    "_session_event_queues_ref",
    "_child_session_parents",
    "_drained_delivered_subagent_children",
    "_drained_subagent_work_ids",
    "_subagent_recovery_done",
    "_subagent_recovery_locks",
)


@pytest.fixture
def _clean_subagent_registry() -> Iterator[None]:
    """Snapshot and restore the process-wide sub-agent / inbox maps.

    The sub-agent work registry, child records, inbox and event queues live in
    module-level dicts on ``omnigent.runner.app`` that otherwise leak across
    tests. Maps a tree does not define yet are skipped so the tests still run
    as a fail-to-pass check against it.
    """
    maps: list[dict[Any, Any] | set[Any]] = [
        getattr(runner_app, name) for name in _REGISTRY_MAPS if hasattr(runner_app, name)
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


async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
    del agent_id, session_id
    return AgentSpec(
        spec_version=1,
        name="reviewer",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )


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
    runner_app._session_inboxes_ref[PARENT_SESSION_ID] = inbox
    runner_app.register_child_session(
        CHILD_SESSION_ID,
        parent_session_id=PARENT_SESSION_ID,
        title="reviewer:review",
        tool="reviewer",
        session_name="review",
    )
    entry = runner_app.register_subagent_work(
        parent_session_id=PARENT_SESSION_ID,
        child_session_id=CHILD_SESSION_ID,
        agent="reviewer",
        title="review the diff",
    )
    return app, inbox, entry.work_id


def _pane_status_publisher(app: Any) -> Any:
    """The runner-local publisher the status-file poller and pane watcher call."""
    publish = app.state.session_resource_registry._session_status_publisher
    assert publish is not None
    return publish


async def _post_status(client: Any, *, status: str, output: str | None = None) -> Any:
    """POST an ``external_session_status`` edge to the child, as the forwarder does."""
    data: dict[str, Any] = {"status": status}
    if output is not None:
        data["output"] = output
    return await client.post(
        f"/v1/sessions/{CHILD_SESSION_ID}/events",
        json={"type": "external_session_status", "data": data},
    )


async def _deliver_and_drain_round_one(
    client: Any, inbox: asyncio.Queue[dict[str, Any]], server: NullServerClient
) -> None:
    """The worker reports round one and the orchestrator reads it with ``sys_read_inbox``."""
    r1 = await _post_status(client, status="idle", output="round one: found the bug")
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
    runner_app._session_inboxes_ref[PARENT_SESSION_ID] = inbox

    # The orchestrator's sys_session_send dispatched this worker.
    entry = runner_app.register_subagent_work(
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
        assert CHILD_SESSION_ID in runner_app._drained_delivered_subagent_children
        assert runner_app.get_subagent_work(CHILD_SESSION_ID) is None

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
        assert runner_app.get_subagent_work(CHILD_SESSION_ID) is None

        # Claude's status file flips back to busy: the poller publishes
        # running through the runner-local publisher, never through /events.
        publish_pane_status(CHILD_SESSION_ID, "running", None)

        r2_idle = await _post_status(
            client, status="idle", output="round two: should I open the PR?"
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
        r1 = await _post_status(client, status="idle", output="round one: found the bug")
        assert r1.status_code == 204
        assert inbox.qsize() == 1

        publish_pane_status(CHILD_SESSION_ID, "running", None)

        drained_text = await tool_dispatch._drain_inbox(
            inbox, server_client=server, conversation_id=PARENT_SESSION_ID
        )
        assert "round one: found the bug" in drained_text
        live = runner_app.get_subagent_work(CHILD_SESSION_ID)
        status_after_drain = live.status if live is not None else None

        r2_idle = await _post_status(
            client, status="idle", output="round two: should I open the PR?"
        )
        assert r2_idle.status_code == 204
        second = _drain_queue(inbox)

    _assert_fresh_result(second, work_id=work_id)
    assert status_after_drain == "running", (
        "draining round one removed the worker's live entry although its next turn "
        "had already started"
    )
    assert CHILD_SESSION_ID not in runner_app._drained_delivered_subagent_children


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
        assert runner_app.get_subagent_work(CHILD_SESSION_ID) is None

        publish_pane_status(CHILD_SESSION_ID, "running", None)
        entry = runner_app.get_subagent_work(CHILD_SESSION_ID)
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
async def test_selfresume_shows_an_idle_orchestrator_waiting(
    _clean_subagent_registry: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An orchestrator already idle when its worker self-resumes is shown waiting on it.

    The orchestrator read round one and its turn ended idle; the worker's
    re-arming ``running`` edge is the only signal before the self-resumed turn's
    ``Stop``, so it is what moves the orchestrator to ``waiting``.
    """
    monkeypatch.setattr(runner_app, "_server_version", "0.16.0")
    server = _ChildSnapshotServerClient()
    app, inbox, _work_id = _dispatch_worker(server)
    publish_pane_status = _pane_status_publisher(app)

    async with _runner_client(app) as client:
        await _deliver_and_drain_round_one(client, inbox, server)
        app.state.native_pane_status[PARENT_SESSION_ID] = "idle"
        _parent_status_events()

        publish_pane_status(CHILD_SESSION_ID, "running", None)
        await asyncio.sleep(0)

    assert _parent_status_events() == ["waiting"], (
        "the orchestrator stayed idle while its self-resumed worker was running"
    )


@pytest.mark.asyncio
async def test_selfresume_leaves_a_mid_turn_orchestrator_to_its_own_turn_end(
    _clean_subagent_registry: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An orchestrator with a turn in flight is not moved by the worker's edge.

    Its own turn end computes ``waiting`` from the live entries, so publishing
    here would race that convergence point.
    """
    monkeypatch.setattr(runner_app, "_server_version", "0.16.0")
    server = _ChildSnapshotServerClient()
    app, inbox, _work_id = _dispatch_worker(server)
    publish_pane_status = _pane_status_publisher(app)

    async with _runner_client(app) as client:
        await _deliver_and_drain_round_one(client, inbox, server)
        app.state.native_pane_status[PARENT_SESSION_ID] = "idle"
        app.state.active_turns[PARENT_SESSION_ID] = None
        _parent_status_events()

        publish_pane_status(CHILD_SESSION_ID, "running", None)
        await asyncio.sleep(0)

    assert _parent_status_events() == []


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
    runner_app._session_inboxes_ref.pop(PARENT_SESSION_ID)
    publish_pane_status = _pane_status_publisher(app)

    async with _runner_client(app) as client:
        await _post_status(client, status="idle", output="round one: found the bug")
        recorded = runner_app.get_subagent_work(CHILD_SESSION_ID)
        assert recorded is not None
        assert recorded.status == "completed" and not recorded.delivered

        publish_pane_status(CHILD_SESSION_ID, "running", None)

    kept = runner_app.get_subagent_work(CHILD_SESSION_ID)
    assert kept is recorded
    assert kept.status == "completed" and not kept.delivered
    assert kept.output == "round one: found the bug"
    assert kept.work_id == work_id
