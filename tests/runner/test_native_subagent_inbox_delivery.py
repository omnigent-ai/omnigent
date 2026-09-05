"""Native sub-agent completions must reach the parent inbox.

A native CLI sub-agent's completion is the forwarder's ``external_session_status:
idle`` (or ``failed``) event POSTed to the child's ``/events``. The runner turns
that into a ``sub_agent`` payload in the parent's async inbox (``sys_read_inbox``)
so the orchestrator wakes instead of busy-polling ``sys_session_get_history``.

Delivery is dropped whenever the runner's in-memory work entry for the child is
missing — a reconnect / restart wiped ``_subagent_work_by_child`` mid-turn, or a
``sys_session_create`` child never registered one (the server records a
``parent_session_id`` but no ``sub_agent_name``). The old code then returned
HTTP 204 and lost the completion. The fix rebuilds the entry from the server
snapshot before delivering, and returns 503 (so the forwarder retries) when
delivery still can't be confirmed on this runner.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest

from omnigent.runner import app as runner_app
from omnigent.runner import create_runner_app
from omnigent.spec.types import AgentSpec, ExecutorSpec
from tests.runner.conftest import (
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
    _sse,
)

# Reuse the proven runner-turn stubs from the sessions-native suite.
from tests.runner.helpers import NullServerClient

PARENT_SESSION_ID = "conv_parent_orchestrator"
CHILD_SESSION_ID = "conv_child_reviewer"


@pytest.fixture
def _clean_subagent_registry() -> Iterator[None]:
    """Snapshot and restore the process-wide sub-agent / inbox maps.

    The sub-agent work registry and inbox queues live in module-level dicts on
    ``omnigent.runner.app`` that otherwise leak across tests. Clear them before
    the test and restore the originals after.
    """
    saved = (
        dict(runner_app._subagent_work_by_child),
        {k: set(v) for k, v in runner_app._subagent_work_by_parent.items()},
        dict(runner_app._session_inboxes_ref),
        set(runner_app._drained_delivered_subagent_children),
        set(runner_app._subagent_recovery_done),
        dict(runner_app._subagent_recovery_locks),
    )
    runner_app._subagent_work_by_child.clear()
    runner_app._subagent_work_by_parent.clear()
    runner_app._session_inboxes_ref.clear()
    runner_app._drained_delivered_subagent_children.clear()
    runner_app._subagent_recovery_done.clear()
    runner_app._subagent_recovery_locks.clear()
    try:
        yield
    finally:
        runner_app._subagent_work_by_child.clear()
        runner_app._subagent_work_by_child.update(saved[0])
        runner_app._subagent_work_by_parent.clear()
        runner_app._subagent_work_by_parent.update(saved[1])
        runner_app._session_inboxes_ref.clear()
        runner_app._session_inboxes_ref.update(saved[2])
        runner_app._drained_delivered_subagent_children.clear()
        runner_app._drained_delivered_subagent_children.update(saved[3])
        runner_app._subagent_recovery_done.clear()
        runner_app._subagent_recovery_done.update(saved[4])
        runner_app._subagent_recovery_locks.clear()
        runner_app._subagent_recovery_locks.update(saved[5])


class _SnapshotServerClient(NullServerClient):
    """Server client whose ``GET /v1/sessions/{child}`` carries the sub-agent snapshot.

    Mirrors ``SessionResponse`` (server routes/sessions.py): the authoritative
    source the runner uses to rebuild a lost sub-agent work entry. The body is
    configurable so a test can model a declared sub-agent (``sub_agent_name``
    set), a ``sys_session_create`` child (``sub_agent_name`` null but
    ``parent_session_id`` set + ``agent_name``), or a top-level session (no
    parent). All other endpoints fall through to the empty-200 base.
    """

    def __init__(self, child_body: dict[str, Any]) -> None:
        """Configure the JSON body returned for the child session GET."""
        self._child_body = child_body

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
            return self._Resp(self._child_body)
        if url.rstrip("/").endswith("/items"):
            return self._Resp({"data": [], "has_more": False})
        return self._Response()


DISPATCH_ID = "subagent_dispatch0001"
_CHILD_RESULT_ITEM: dict[str, Any] = {
    "type": "message",
    "role": "assistant",
    "content": [{"type": "output_text", "text": "review complete: LGTM"}],
}


def _child_summary(**overrides: Any) -> dict[str, Any]:
    """
    Build a terminal child-session summary as the sessions API returns it.

    :param overrides: Field overrides, e.g. ``current_task_status="failed"``.
    :returns: Child summary carrying an undrained dispatch id by default.
    """
    summary: dict[str, Any] = {
        "id": CHILD_SESSION_ID,
        "tool": "reviewer",
        "session_name": "review",
        "current_task_status": "completed",
        "labels": {runner_app.SUBAGENT_DISPATCH_ID_LABEL_KEY: DISPATCH_ID},
    }
    summary.update(overrides)
    return summary


class _RecoveryServerClient(NullServerClient):
    """Serve the durable child records that restart recovery reads."""

    def __init__(
        self,
        children: list[dict[str, Any]],
        *,
        child_items: list[dict[str, Any]] | None = None,
        failed_item_sessions: set[str] | None = None,
    ) -> None:
        """
        Configure the child list and transcript returned by the fake server.

        :param children: Parent's child-session summaries.
        :param child_items: Child transcript, newest first.
        :param failed_item_sessions: Session ids whose item read returns 503.
        """
        self.children = children
        self.child_items = [_CHILD_RESULT_ITEM] if child_items is None else child_items
        self.failed_item_sessions = failed_item_sessions or set()
        self.requests: list[tuple[str, dict[str, Any]]] = []

    class _Resp:
        """Minimal HTTP response carrying a JSON payload."""

        def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
            """
            Store one JSON response payload.

            :param payload: JSON object returned by :meth:`json`.
            :param status_code: HTTP status exposed to production code.
            """
            self.status_code = status_code
            self._payload = payload

        def json(self) -> dict[str, Any]:
            """Return the configured JSON payload."""
            return self._payload

    async def get(self, url: str, **kwargs: Any) -> Any:
        """
        Return child summaries and transcripts for recovery reads.

        :param url: Requested sessions API path.
        :param kwargs: HTTP request options; ``params`` are recorded.
        :returns: Minimal response for the requested resource.
        """
        params = dict(kwargs.get("params") or {})
        self.requests.append((url, params))
        if url.endswith(f"/{PARENT_SESSION_ID}/child_sessions"):
            return self._Resp({"data": self.children, "has_more": False})
        if url.endswith("/items"):
            session_id = url.rstrip("/").split("/")[-2]
            if session_id in self.failed_item_sessions:
                return self._Resp({}, status_code=503)
            return self._Resp({"data": self.child_items, "has_more": False})
        return self._Resp({"data": [], "has_more": False})


def _child_snapshot(
    *,
    sub_agent_name: str | None,
    parent_session_id: str | None,
    agent_name: str | None = "cursor-native-ui",
) -> dict[str, Any]:
    """Build a child ``SessionResponse``-shaped body."""
    return {
        "id": CHILD_SESSION_ID,
        "agent_id": "ag_reviewer",
        "agent_name": agent_name,
        "sub_agent_name": sub_agent_name,
        "parent_session_id": parent_session_id,
        "created_at": 0,
        "workspace": None,
    }


async def _post_native_idle(
    *,
    child_body: dict[str, Any],
    seed_parent_inbox: bool,
    register_work: bool,
    output: str = "review complete: LGTM",
) -> tuple[int, list[dict[str, Any]]]:
    """POST a native ``external_session_status: idle`` and return (http, inbox items).

    Models the forwarder reporting a finished native sub-agent turn.
    ``register_work`` seeds the in-memory work entry (the healthy case); leaving
    it ``False`` models a reconnect-wiped map or a ``sys_session_create`` child
    the dispatch never registered. ``seed_parent_inbox`` controls whether the
    parent's inbox queue is present on this runner.
    """
    if seed_parent_inbox:
        runner_app._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    if register_work:
        runner_app.register_subagent_work(
            parent_session_id=PARENT_SESSION_ID,
            child_session_id=CHILD_SESSION_ID,
            agent="reviewer",
            title="review",
        )

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return AgentSpec(
            spec_version=1,
            name="reviewer",
            executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
        )

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=_SnapshotServerClient(child_body),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        resp = await client.post(
            f"/v1/sessions/{CHILD_SESSION_ID}/events",
            json={
                "type": "external_session_status",
                "data": {"status": "idle", "output": output},
            },
        )

    inbox = runner_app._session_inboxes_ref.get(PARENT_SESSION_ID)
    items: list[dict[str, Any]] = []
    if inbox is not None:
        while not inbox.empty():
            items.append(inbox.get_nowait())
    return resp.status_code, items


@pytest.mark.asyncio
async def test_native_completion_recovers_reconnect_wiped_work_entry(
    _clean_subagent_registry: None,
) -> None:
    """A declared native sub-agent still delivers after its work entry was lost.

    With no in-memory work entry (a reconnect wiped it mid-turn), the idle edge
    dropped silently on the old code. The fix rebuilds the entry from the
    snapshot's ``parent_session_id`` + ``sub_agent_name`` and delivers.
    """
    http, items = await _post_native_idle(
        child_body=_child_snapshot(sub_agent_name="reviewer", parent_session_id=PARENT_SESSION_ID),
        seed_parent_inbox=True,
        register_work=False,
    )

    assert items, (
        "native sub-agent reported idle but nothing was delivered to the parent "
        "inbox: the work entry was missing (reconnect-wiped) and the idle edge "
        f"was silently 204-acked. (http={http})"
    )
    payload = items[0]
    assert payload["type"] == "sub_agent"
    assert payload["conversation_id"] == CHILD_SESSION_ID
    assert payload["status"] == "completed"
    assert payload["output"] == "review complete: LGTM"


@pytest.mark.asyncio
async def test_sys_session_create_child_without_sub_agent_name_delivers(
    _clean_subagent_registry: None,
) -> None:
    """A ``sys_session_create`` child (no ``sub_agent_name``) still wakes the parent.

    The child has ``agent_name: cursor-native-ui`` but ``sub_agent_name: null``,
    and the dispatch never registered a work entry. The fix recovers the parent
    link from the snapshot (keying on ``parent_session_id``) and labels the work
    with the agent name.
    """
    http, items = await _post_native_idle(
        child_body=_child_snapshot(
            sub_agent_name=None,
            parent_session_id=PARENT_SESSION_ID,
            agent_name="cursor-native-ui",
        ),
        seed_parent_inbox=True,
        register_work=False,
    )

    assert items, (
        f"sys_session_create child reported idle but the parent inbox stayed empty. (http={http})"
    )
    assert items[0]["status"] == "completed"
    assert items[0]["agent"] == "cursor-native-ui"


@pytest.mark.asyncio
async def test_healthy_registered_work_entry_still_delivers(
    _clean_subagent_registry: None,
) -> None:
    """Control: the normal path (work entry present) keeps delivering.

    Guards against the fix regressing the common case where dispatch already
    registered the work entry on this runner.
    """
    http, items = await _post_native_idle(
        child_body=_child_snapshot(sub_agent_name="reviewer", parent_session_id=PARENT_SESSION_ID),
        seed_parent_inbox=True,
        register_work=True,
    )

    assert http == 204
    assert items and items[0]["status"] == "completed"


@pytest.mark.asyncio
async def test_undeliverable_native_completion_returns_503_not_silent_204(
    _clean_subagent_registry: None,
) -> None:
    """A recoverable sub-agent whose parent inbox is elsewhere must 503, not 204.

    When the parent inbox is not on this runner (the parent lives on a different
    runner, or the runner restarted and lost it), delivery cannot be confirmed.
    The handler must return 503 so the forwarder retries and server-side recovery
    re-routes to the parent's runner — instead of a silent 204 that drops it.
    """
    http, items = await _post_native_idle(
        child_body=_child_snapshot(sub_agent_name="reviewer", parent_session_id=PARENT_SESSION_ID),
        seed_parent_inbox=False,
        register_work=False,
    )

    assert http == 503, (
        "an undeliverable native sub-agent completion was acked with "
        f"http={http}; expected 503 so the forwarder retries. Items={items!r}"
    )


@pytest.mark.asyncio
async def test_replayed_idle_after_drain_does_not_redeliver(
    _clean_subagent_registry: None,
) -> None:
    """The recovery must not re-deliver a child already delivered and drained.

    Guards the snapshot-recovery arm against a duplicate: once a completion was
    delivered and the parent drained it, the runner keeps a delivered tombstone.
    A replayed idle whose snapshot *does* carry a ``parent_session_id`` (the
    production shape) must NOT rebuild the work entry and re-enqueue — it stays a
    benign already-delivered 204. (The existing suite's dedup test uses a stub
    snapshot with no parent, so it would not catch a recovery-induced re-deliver.)
    """
    child_body = _child_snapshot(sub_agent_name="reviewer", parent_session_id=PARENT_SESSION_ID)
    # First completion delivers normally.
    http1, items1 = await _post_native_idle(
        child_body=child_body, seed_parent_inbox=True, register_work=True
    )
    assert http1 == 204
    assert len(items1) == 1  # drained by the helper

    # Mark the child delivered-and-drained, exactly as sys_read_inbox does.
    runner_app.unregister_subagent_work(CHILD_SESSION_ID, remember_drained_delivery=True)
    assert runner_app.get_subagent_work(CHILD_SESSION_ID) is None

    # Replay the idle — snapshot carries a parent, so a naive recovery would
    # rebuild the entry and re-deliver. The tombstone guard must prevent that.
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return AgentSpec(
            spec_version=1,
            name="reviewer",
            executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
        )

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=_SnapshotServerClient(child_body),  # type: ignore[arg-type]
    )
    inbox = runner_app._session_inboxes_ref[PARENT_SESSION_ID]
    async with _runner_client(app) as client:
        replay = await client.post(
            f"/v1/sessions/{CHILD_SESSION_ID}/events",
            json={"type": "external_session_status", "data": {"status": "idle", "output": "x"}},
        )

    assert replay.status_code == 204
    assert inbox.qsize() == 0, "replayed idle re-delivered a duplicate to the parent inbox"


@pytest.mark.asyncio
async def test_top_level_session_idle_is_noop(
    _clean_subagent_registry: None,
) -> None:
    """A top-level session (no parent) idle edge stays a quiet 204 no-op.

    Ensures the recovery arm does not mis-classify a non-sub-agent sender as a
    sub-agent and start 503-ing or fabricating inbox deliveries.
    """
    http, items = await _post_native_idle(
        child_body=_child_snapshot(sub_agent_name=None, parent_session_id=None),
        seed_parent_inbox=True,
        register_work=False,
    )

    assert http == 204
    assert items == []


@pytest.mark.asyncio
async def test_runner_restart_recovers_undrained_terminal_child(
    _clean_subagent_registry: None,
) -> None:
    """A fresh runner rebuilds an undrained child result from the receipt gap.

    Nothing survives the process: no work entry, inbox item, or drained
    tombstone. The child carries a dispatch id but no delivered-id receipt,
    so initializing the parent must re-queue its result under that same id.
    """
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """
        Return the parent spec needed by the public initialization route.

        :param agent_id: Bound agent id supplied by initialization.
        :param session_id: Parent session id being initialized.
        :returns: Minimal parent agent specification.
        """
        del agent_id, session_id
        return AgentSpec(spec_version=1, name="orchestrator")

    server_client = _RecoveryServerClient([_child_summary()])
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server_client,  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        response = await client.post(
            "/v1/sessions",
            json={"session_id": PARENT_SESSION_ID, "agent_id": "ag_orchestrator"},
        )

    assert response.status_code == 201
    inbox = runner_app._session_inboxes_ref[PARENT_SESSION_ID]
    assert inbox.qsize() == 1
    payload = inbox.get_nowait()
    assert payload["conversation_id"] == CHILD_SESSION_ID
    assert payload["status"] == "completed"
    assert payload["output"] == "review complete: LGTM"
    assert payload["work_id"] == DISPATCH_ID
    child_reads = [
        params
        for url, params in server_client.requests
        if url.endswith(f"/{CHILD_SESSION_ID}/items")
    ]
    assert child_reads == [{"limit": "100", "order": "desc"}]
    request_count = len(server_client.requests)
    await app.state.recover_undrained_subagent_results(PARENT_SESSION_ID)
    assert len(server_client.requests) == request_count


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "labels",
    [
        {
            runner_app.SUBAGENT_DISPATCH_ID_LABEL_KEY: DISPATCH_ID,
            runner_app.SUBAGENT_DELIVERED_ID_LABEL_KEY: DISPATCH_ID,
        },
        {},
    ],
)
async def test_runner_restart_skips_drained_and_unstamped_children(
    _clean_subagent_registry: None,
    labels: dict[str, str],
) -> None:
    """A matching receipt, or a child created before receipts, is not replayed.

    :param _clean_subagent_registry: Isolates module-level runner state.
    :param labels: Either a drained turn's labels or a legacy child's none.
    """
    runner_app._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    server_client = _RecoveryServerClient([_child_summary(labels=labels)])
    app = create_runner_app(server_client=server_client)  # type: ignore[arg-type]

    await app.state.recover_undrained_subagent_results(PARENT_SESSION_ID)

    assert runner_app._session_inboxes_ref[PARENT_SESSION_ID].empty()
    assert runner_app.get_subagent_work(CHILD_SESSION_ID) is None
    assert not any(url.endswith("/items") for url, _ in server_client.requests)


@pytest.mark.asyncio
async def test_runner_restart_replays_continued_turn_with_stale_receipt(
    _clean_subagent_registry: None,
) -> None:
    """A receipt for an earlier turn cannot mask a continued child's new turn."""
    runner_app._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    labels = {
        runner_app.SUBAGENT_DISPATCH_ID_LABEL_KEY: "subagent_turn2",
        runner_app.SUBAGENT_DELIVERED_ID_LABEL_KEY: "subagent_turn1",
    }
    app = create_runner_app(
        server_client=_RecoveryServerClient([_child_summary(labels=labels)]),  # type: ignore[arg-type]
    )

    await app.state.recover_undrained_subagent_results(PARENT_SESSION_ID)

    payload = runner_app._session_inboxes_ref[PARENT_SESSION_ID].get_nowait()
    assert payload["work_id"] == "subagent_turn2"
    assert payload["output"] == "review complete: LGTM"


@pytest.mark.asyncio
async def test_drain_before_session_init_still_recovers(
    _clean_subagent_registry: None,
) -> None:
    """A drain that runs before the session is initialized still recovers.

    After a reconnect the server can dispatch a pending message before it
    re-initializes the session on the replacement runner, so the parent has no
    inbox yet when ``sys_read_inbox`` runs. Recovery must create the inbox and
    queue the result rather than treat the missing inbox as nothing to do.
    """
    app = create_runner_app(
        server_client=_RecoveryServerClient([_child_summary()]),  # type: ignore[arg-type]
    )
    assert PARENT_SESSION_ID not in runner_app._session_inboxes_ref

    await app.state.recover_undrained_subagent_results(PARENT_SESSION_ID)

    payload = runner_app._session_inboxes_ref[PARENT_SESSION_ID].get_nowait()
    assert payload["conversation_id"] == CHILD_SESSION_ID
    assert payload["output"] == "review complete: LGTM"


@pytest.mark.asyncio
async def test_runner_restart_recovers_text_less_final_turn_as_no_output(
    _clean_subagent_registry: None,
) -> None:
    """The newest assistant message wins even without text, as in live delivery.

    Walking past it to an older message would surface a previous turn's text
    as this turn's result.
    """
    runner_app._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    child_items = [
        {"type": "message", "role": "assistant", "content": []},
        _CHILD_RESULT_ITEM,
    ]
    app = create_runner_app(
        server_client=_RecoveryServerClient([_child_summary()], child_items=child_items),  # type: ignore[arg-type]
    )

    await app.state.recover_undrained_subagent_results(PARENT_SESSION_ID)

    payload = runner_app._session_inboxes_ref[PARENT_SESSION_ID].get_nowait()
    assert payload["status"] == "completed"
    assert payload["output"] == ""


@pytest.mark.asyncio
async def test_runner_restart_recovers_failed_child_error(
    _clean_subagent_registry: None,
) -> None:
    """A failed child replays its durable error without reading its transcript."""
    runner_app._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    server_client = _RecoveryServerClient(
        [
            _child_summary(
                current_task_status="failed",
                last_task_error={"code": "required_terminal_exited", "message": "pane died"},
            )
        ]
    )
    app = create_runner_app(server_client=server_client)  # type: ignore[arg-type]

    await app.state.recover_undrained_subagent_results(PARENT_SESSION_ID)

    payload = runner_app._session_inboxes_ref[PARENT_SESSION_ID].get_nowait()
    assert payload["status"] == "failed"
    assert payload["output"] == "pane died"
    assert not any(url.endswith("/items") for url, _ in server_client.requests)


@pytest.mark.asyncio
async def test_runner_restart_retries_after_child_history_read_failure(
    _clean_subagent_registry: None,
) -> None:
    """A failed transcript read leaves the scan unfinished so the drain retries it."""
    runner_app._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    server_client = _RecoveryServerClient(
        [_child_summary()], failed_item_sessions={CHILD_SESSION_ID}
    )
    app = create_runner_app(server_client=server_client)  # type: ignore[arg-type]

    await app.state.recover_undrained_subagent_results(PARENT_SESSION_ID)
    assert runner_app._session_inboxes_ref[PARENT_SESSION_ID].empty()
    assert PARENT_SESSION_ID not in runner_app._subagent_recovery_done

    server_client.failed_item_sessions.clear()
    await app.state.recover_undrained_subagent_results(PARENT_SESSION_ID)

    payload = runner_app._session_inboxes_ref[PARENT_SESSION_ID].get_nowait()
    assert payload["conversation_id"] == CHILD_SESSION_ID
    assert PARENT_SESSION_ID in runner_app._subagent_recovery_done


@pytest.mark.asyncio
async def test_concurrent_recovery_scans_deliver_exactly_once(
    _clean_subagent_registry: None,
) -> None:
    """Session initialization racing a ``sys_read_inbox`` drain queues one result."""

    class _YieldingRecoveryServerClient(_RecoveryServerClient):
        """Yield to the event loop on every read, exposing the interleaving."""

        async def get(self, url: str, **kwargs: Any) -> Any:
            await asyncio.sleep(0)
            return await super().get(url, **kwargs)

    runner_app._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    app = create_runner_app(
        server_client=_YieldingRecoveryServerClient([_child_summary()]),  # type: ignore[arg-type]
    )

    await asyncio.gather(
        app.state.recover_undrained_subagent_results(PARENT_SESSION_ID),
        app.state.recover_undrained_subagent_results(PARENT_SESSION_ID),
    )

    assert runner_app._session_inboxes_ref[PARENT_SESSION_ID].qsize() == 1


@pytest.mark.asyncio
async def test_routed_child_off_its_native_spec_still_delivers(
    _clean_subagent_registry: None,
) -> None:
    """A child routed onto an SDK harness delivers, though its spec says native.

    The Smart Routing shape: polly's ``claude_code`` worker declares
    ``claude-native``, but a routed child is forwarded ``harness_override`` and
    actually runs ``claude-sdk``. The runner read the harness off the cached
    SPEC, so the turn looked native and its completion was left to a native
    path that never runs — the parent waited forever while the pi sibling
    (whose spec harness is already non-native) was the only one to report.
    """
    runner_app._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    runner_app.register_subagent_work(
        parent_session_id=PARENT_SESSION_ID,
        child_session_id=CHILD_SESSION_ID,
        agent="claude_code",
        title="joke-claude",
    )
    harness_client = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_1"}}),
            _sse({"type": "response.output_text.delta", "delta": "knock knock"}),
            _sse({"type": "response.completed", "response": {"id": "resp_1"}}),
        ]
    )
    pm = _FakeProcessManager(harness_client)

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return AgentSpec(
            spec_version=1,
            name="claude_code",
            executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
        )

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    inbox = runner_app._session_inboxes_ref[PARENT_SESSION_ID]
    async with _runner_client(app) as client:
        resp = await client.post(
            f"/v1/sessions/{CHILD_SESSION_ID}/events",
            json={
                "type": "message",
                "agent_id": "ag_reviewer",
                "harness_override": "claude-sdk",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "tell me a joke"}],
                },
            },
        )
        assert resp.status_code in (200, 202), resp.text
        for _ in range(200):
            if not inbox.empty():
                break
            await asyncio.sleep(0.01)

    items: list[dict[str, Any]] = []
    while not inbox.empty():
        items.append(inbox.get_nowait())
    assert items, (
        "a routed child finished its turn but nothing reached the parent inbox: "
        "the runner read the harness off the spec (claude-native) instead of the "
        "forwarded harness_override (claude-sdk)"
    )
    assert items[0]["status"] == "completed"
    assert items[0]["conversation_id"] == CHILD_SESSION_ID
