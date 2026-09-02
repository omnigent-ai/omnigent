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
        set(runner_app._subagent_recovery_scanned_parents),
    )
    runner_app._subagent_work_by_child.clear()
    runner_app._subagent_work_by_parent.clear()
    runner_app._session_inboxes_ref.clear()
    runner_app._drained_delivered_subagent_children.clear()
    runner_app._subagent_recovery_scanned_parents.clear()
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
        runner_app._subagent_recovery_scanned_parents.clear()
        runner_app._subagent_recovery_scanned_parents.update(saved[4])


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


class _RecoveryServerClient(NullServerClient):
    """Serve durable parent/child history used by restart recovery tests."""

    def __init__(
        self,
        *,
        parent_items: list[dict[str, Any]],
        child_items: list[dict[str, Any]] | None = None,
        compaction_boundaries: dict[str, str] | None = None,
        failed_item_sessions: set[str] | None = None,
    ) -> None:
        """
        Configure persisted parent items returned by the fake server.

        :param parent_items: Parent transcript items, newest first.
        :param child_items: Optional child transcript override, oldest first.
        :param compaction_boundaries: Optional session-to-last-item cursor map.
        :param failed_item_sessions: Session ids whose non-compaction item read
            should return HTTP 503.
        """
        self._parent_items = parent_items
        self._child_items = child_items
        self._compaction_boundaries = compaction_boundaries or {}
        self.failed_item_sessions = failed_item_sessions or set()
        self.requests: list[tuple[str, dict[str, Any]]] = []

    class _Resp:
        """Minimal successful HTTP response carrying a JSON payload."""

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
        Return child summaries and persisted transcripts for recovery.

        :param url: Requested sessions API path.
        :param kwargs: Ignored HTTP request options.
        :returns: Minimal successful response for the requested resource.
        """
        params = kwargs.get("params") or {}
        self.requests.append((url, dict(params)))
        if params.get("type") == "compaction":
            session_id = url.rstrip("/").split("/")[-2]
            boundary = self._compaction_boundaries.get(session_id)
            rows = (
                [
                    {
                        "id": f"compact_{session_id}",
                        "type": "compaction",
                        "summary": "Prior context summarized.",
                        "last_item_id": boundary,
                    }
                ]
                if boundary is not None
                else []
            )
            return self._Resp({"data": rows, "has_more": False})
        if url.endswith("/items"):
            session_id = url.rstrip("/").split("/")[-2]
            if session_id in self.failed_item_sessions:
                return self._Resp({}, status_code=503)
        if url.endswith(f"/{PARENT_SESSION_ID}/child_sessions"):
            return self._Resp(
                {
                    "data": [
                        {
                            "id": CHILD_SESSION_ID,
                            "tool": "reviewer",
                            "session_name": "review",
                            "current_task_status": "completed",
                            "updated_at": 200,
                        }
                    ],
                    "has_more": False,
                }
            )
        if url.endswith(f"/{PARENT_SESSION_ID}/items"):
            return self._Resp({"data": self._parent_items, "has_more": False})
        if url.endswith(f"/{CHILD_SESSION_ID}/items"):
            if self._child_items is not None:
                return self._Resp({"data": self._child_items, "has_more": False})
            return self._Resp(
                {
                    "data": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "created_at": 200,
                            "content": [{"type": "output_text", "text": "review complete: LGTM"}],
                        }
                    ],
                    "has_more": False,
                }
            )
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
    """A new runner queue is rebuilt from durable child state and transcript.

    This is the OMNI-4482 regression: no work entry, inbox item, or drained
    tombstone survives the process restart. The test supplies only server-side
    session/history records and requires recovery to recreate the exact result.
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

    server_client = _RecoveryServerClient(
        parent_items=[],
        compaction_boundaries={
            PARENT_SESSION_ID: "parent_boundary",
            CHILD_SESSION_ID: "child_boundary",
        },
    )
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
    item_requests = [params for url, params in server_client.requests if url.endswith("/items")]
    assert {params.get("after") for params in item_requests} >= {
        "parent_boundary",
        "child_boundary",
    }
    assert sum(params.get("type") == "compaction" for params in item_requests) == 2
    request_count = len(server_client.requests)
    await app.state.recover_undrained_subagent_results(PARENT_SESSION_ID)
    assert len(server_client.requests) == request_count


@pytest.mark.asyncio
async def test_runner_restart_does_not_replay_persisted_drain(
    _clean_subagent_registry: None,
) -> None:
    """A persisted inbox output newer than completion prevents duplicate replay.

    The test's parent function output is the durable acknowledgement already
    produced by the real ``sys_read_inbox`` path. Removing the acknowledgement
    check makes this test enqueue a duplicate and fail.
    """
    runner_app._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    app = create_runner_app(
        server_client=_RecoveryServerClient(
            parent_items=[
                {
                    "type": "function_call",
                    "created_at": 201,
                    "call_id": "call_inbox",
                    "name": "sys_read_inbox",
                },
                {
                    "type": "function_call_output",
                    "created_at": 201,
                    "call_id": "call_inbox",
                    "output": (
                        f"[System: sub-agent task {CHILD_SESSION_ID} completed — "
                        "reviewer:review returned: review complete: LGTM]"
                    ),
                },
            ]
        ),  # type: ignore[arg-type]
    )

    await app.state.recover_undrained_subagent_results(PARENT_SESSION_ID)

    assert runner_app._session_inboxes_ref[PARENT_SESSION_ID].empty()
    assert runner_app.get_subagent_work(CHILD_SESSION_ID) is None


@pytest.mark.asyncio
async def test_runner_restart_recovers_output_from_compaction_summary(
    _clean_subagent_registry: None,
) -> None:
    """A child result covered by compaction is delivered from its summary."""
    runner_app._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    server_client = _RecoveryServerClient(
        parent_items=[],
        child_items=[{"type": "message", "role": "user", "created_at": 201, "content": []}],
        compaction_boundaries={CHILD_SESSION_ID: "child_boundary"},
    )
    app = create_runner_app(server_client=server_client)  # type: ignore[arg-type]

    await app.state.recover_undrained_subagent_results(PARENT_SESSION_ID)

    payload = runner_app._session_inboxes_ref[PARENT_SESSION_ID].get_nowait()
    assert payload["conversation_id"] == CHILD_SESSION_ID
    assert payload["output"] == "Prior context summarized."


@pytest.mark.asyncio
async def test_runner_restart_retries_after_child_history_read_failure(
    _clean_subagent_registry: None,
) -> None:
    """A partial child read failure does not memoize recovery as complete."""
    runner_app._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    server_client = _RecoveryServerClient(parent_items=[], failed_item_sessions={CHILD_SESSION_ID})
    app = create_runner_app(server_client=server_client)  # type: ignore[arg-type]

    await app.state.recover_undrained_subagent_results(PARENT_SESSION_ID)
    assert runner_app._session_inboxes_ref[PARENT_SESSION_ID].empty()
    assert PARENT_SESSION_ID not in runner_app._subagent_recovery_scanned_parents

    server_client.failed_item_sessions.clear()
    await app.state.recover_undrained_subagent_results(PARENT_SESSION_ID)

    payload = runner_app._session_inboxes_ref[PARENT_SESSION_ID].get_nowait()
    assert payload["conversation_id"] == CHILD_SESSION_ID
    assert payload["output"] == "review complete: LGTM"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "parent_items",
    [
        [
            {
                "type": "function_call",
                "created_at": 199,
                "call_id": "call_old_inbox",
                "name": "sys_read_inbox",
            },
            {
                "type": "function_call_output",
                "created_at": 199,
                "call_id": "call_old_inbox",
                "output": f"[System: sub-agent task {CHILD_SESSION_ID} completed]",
            },
        ],
        [
            {
                "type": "function_call_output",
                "created_at": 201,
                "call_id": "call_session_list",
                "output": f"child session: {CHILD_SESSION_ID}",
            }
        ],
    ],
)
async def test_runner_restart_replays_after_old_or_unrelated_child_reference(
    _clean_subagent_registry: None,
    parent_items: list[dict[str, Any]],
) -> None:
    """Only a newer, correlated ``sys_read_inbox`` output proves consumption.

    :param _clean_subagent_registry: Isolates module-level runner state.
    :param parent_items: Either an older inbox drain from a previous child turn
        or an unrelated tool output that happens to mention the child id.
    """
    runner_app._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    app = create_runner_app(
        server_client=_RecoveryServerClient(parent_items=parent_items),  # type: ignore[arg-type]
    )

    await app.state.recover_undrained_subagent_results(PARENT_SESSION_ID)

    payload = runner_app._session_inboxes_ref[PARENT_SESSION_ID].get_nowait()
    assert payload["conversation_id"] == CHILD_SESSION_ID
    assert payload["output"] == "review complete: LGTM"


@pytest.mark.asyncio
async def test_runner_restart_never_suppresses_continued_child_generation(
    _clean_subagent_registry: None,
) -> None:
    """A child-id acknowledgement cannot suppress a later continued turn.

    Both turns and the old drain intentionally share one-second timestamps.
    Without the at-least-once continued-child guard, recovery mistakes the old
    result for acknowledgement of the new one and leaves the inbox empty.
    """
    runner_app._session_inboxes_ref[PARENT_SESSION_ID] = asyncio.Queue()
    parent_items = [
        {
            "type": "function_call",
            "created_at": 200,
            "call_id": "call_old_inbox",
            "name": "sys_read_inbox",
        },
        {
            "type": "function_call_output",
            "created_at": 200,
            "call_id": "call_old_inbox",
            "output": f"[System: sub-agent task {CHILD_SESSION_ID} completed]",
        },
    ]
    child_items = [
        {"type": "message", "role": "user", "created_at": 199, "content": []},
        {"type": "message", "role": "user", "created_at": 200, "content": []},
        {
            "type": "message",
            "role": "assistant",
            "created_at": 200,
            "content": [{"type": "output_text", "text": "new turn result"}],
        },
    ]
    app = create_runner_app(
        server_client=_RecoveryServerClient(
            parent_items=parent_items,
            child_items=child_items,
        ),  # type: ignore[arg-type]
    )

    await app.state.recover_undrained_subagent_results(PARENT_SESSION_ID)

    payload = runner_app._session_inboxes_ref[PARENT_SESSION_ID].get_nowait()
    assert payload["conversation_id"] == CHILD_SESSION_ID
    assert payload["output"] == "new turn result"

    # The restart may replay an ambiguous continued result once, but after the
    # parent drains that replay the process-local tombstone must keep every
    # later sys_read_inbox call from resurrecting it forever.
    runner_app.unregister_subagent_work(CHILD_SESSION_ID, remember_drained_delivery=True)
    await app.state.recover_undrained_subagent_results(PARENT_SESSION_ID)
    assert runner_app._session_inboxes_ref[PARENT_SESSION_ID].empty()


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
