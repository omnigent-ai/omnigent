"""A confirmed completion corrects a native sub-agent's optimistic cancellation.

A ``turn_completed`` Stop-hook idle supersedes a delivered or drained ``cancelled``."""

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
CHILD_SESSION_ID = "conv_child_researcher"
DISPATCH_ID = "subagent_dispatch0001"
SURVIVOR_VERDICT = "verdict: all three citations check out"
_REGISTRY_NAMES = (
    "_subagent_work_by_child",
    "_subagent_work_by_parent",
    "_session_inboxes_ref",
    "_drained_delivered_subagent_children",
    "_drained_cancelled_subagent_work",
)


@pytest.fixture
def _clean_subagent_registry() -> Iterator[None]:
    """Snapshot, clear, and restore the runner's process-wide sub-agent registries."""
    registries = [
        getattr(runner_app, name) for name in _REGISTRY_NAMES if hasattr(runner_app, name)
    ]
    saved = [type(registry)(registry) for registry in registries]
    for registry in registries:
        registry.clear()
    try:
        yield
    finally:
        for registry, snapshot in zip(registries, saved, strict=True):
            registry.clear()
            registry.update(snapshot)


class _ChildSnapshotServerClient(NullServerClient):
    """Serve the child's ``SessionResponse`` snapshot the runner reads to rebuild lost work."""

    _CHILD_BODY: dict[str, Any] = {
        "id": CHILD_SESSION_ID,
        "agent_id": "ag_researcher",
        "agent_name": "claude-native-ui",
        "sub_agent_name": "researcher",
        "parent_session_id": PARENT_SESSION_ID,
        "created_at": 0,
        "workspace": None,
    }

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
            return self._Resp(self._CHILD_BODY)
        if url.rstrip("/").endswith("/items"):
            return self._Resp({"data": [], "has_more": False})
        return self._Response()


def _dispatch_child(*, work_id: str = DISPATCH_ID) -> None:
    """Register one ``sys_session_send`` dispatch to the child with the parent inbox present."""
    runner_app._session_inboxes_ref.setdefault(PARENT_SESSION_ID, asyncio.Queue())
    runner_app.register_subagent_work(
        parent_session_id=PARENT_SESSION_ID,
        child_session_id=CHILD_SESSION_ID,
        agent="researcher",
        title="cite-check",
        work_id=work_id,
    )


def _interrupt_child() -> None:
    """Report the optimistic ``cancelled`` the interrupt path delivers to the parent inbox."""
    ack = runner_app.mark_subagent_work_terminal(
        CHILD_SESSION_ID, status="cancelled", output="[System: sub-agent interrupted]"
    )
    assert ack.delivered_now


async def _drain(payload: dict[str, Any]) -> str:
    """Run the ``sys_read_inbox`` drain bookkeeping for one payload and return its rendering."""
    text = tool_dispatch._format_async_task_item(payload)
    await tool_dispatch._cleanup_drained_subagent_work(payload, server_client=None)
    return text


async def _drain_next() -> str:
    """Drain the next payload from the parent inbox."""
    return await _drain(runner_app._session_inboxes_ref[PARENT_SESSION_ID].get_nowait())


async def _post_child_idle(
    *, output: str, turn_completed: bool | None = None
) -> tuple[int, list[dict[str, Any]]]:
    """POST the child's idle edge; ``turn_completed=True`` marks Claude's ``Stop`` hook."""
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return AgentSpec(
            spec_version=1,
            name="researcher",
            executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
        )

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=_ChildSnapshotServerClient(),  # type: ignore[arg-type]
    )
    data: dict[str, Any] = {"status": "idle", "output": output}
    if turn_completed is not None:
        data["turn_completed"] = turn_completed
    async with _runner_client(app) as client:
        resp = await client.post(
            f"/v1/sessions/{CHILD_SESSION_ID}/events",
            json={"type": "external_session_status", "data": data},
        )
    inbox = runner_app._session_inboxes_ref.get(PARENT_SESSION_ID)
    items: list[dict[str, Any]] = []
    if inbox is not None:
        while not inbox.empty():
            items.append(inbox.get_nowait())
    return resp.status_code, items


@pytest.mark.asyncio
async def test_confirmed_completion_corrects_drained_cancellation(
    _clean_subagent_registry: None,
) -> None:
    """A child that survives Stop and finishes still reaches the parent after the drain."""
    _dispatch_child()
    _interrupt_child()
    assert "cancelled" in await _drain_next()
    assert runner_app.get_subagent_work(CHILD_SESSION_ID) is None

    http, items = await _post_child_idle(output=SURVIVOR_VERDICT, turn_completed=True)

    assert http == 204
    assert [item["status"] for item in items] == ["completed"]
    corrected = items[0]
    assert corrected["work_id"] == DISPATCH_ID
    assert corrected["output"] == SURVIVOR_VERDICT
    assert corrected["corrected_status"] == "cancelled"
    text = await _drain(corrected)
    assert "completed, superseding its earlier cancelled notice" in text
    assert SURVIVOR_VERDICT in text

    # The drained correction is final: a replayed Stop-hook idle delivers nothing more.
    http, items = await _post_child_idle(output=SURVIVOR_VERDICT, turn_completed=True)
    assert (http, items) == (204, [])


@pytest.mark.asyncio
async def test_bare_idle_after_drained_cancellation_settles_nothing(
    _clean_subagent_registry: None,
) -> None:
    """A quiescence idle after a Stop proves nothing; the confirmed completion still can."""
    _dispatch_child()
    _interrupt_child()
    await _drain_next()

    http, items = await _post_child_idle(output="partial text before the interrupt landed")
    assert (http, items) == (204, [])

    http, items = await _post_child_idle(output=SURVIVOR_VERDICT, turn_completed=True)
    assert http == 204
    assert [(item["status"], item["work_id"]) for item in items] == [("completed", DISPATCH_ID)]


@pytest.mark.asyncio
async def test_confirmed_completion_supersedes_undrained_cancellation(
    _clean_subagent_registry: None,
) -> None:
    """A cancellation still sitting in the inbox is followed by the real result."""
    _dispatch_child()
    _interrupt_child()

    http, items = await _post_child_idle(output=SURVIVOR_VERDICT, turn_completed=True)

    assert http == 204
    assert [item["status"] for item in items] == ["cancelled", "completed"]
    assert items[1]["work_id"] == DISPATCH_ID
    assert items[1]["corrected_status"] == "cancelled"
    entry = runner_app.get_subagent_work(CHILD_SESSION_ID)
    assert entry is not None
    assert (entry.status, entry.output, entry.delivered) == ("completed", SURVIVOR_VERDICT, True)


@pytest.mark.asyncio
async def test_drained_completion_is_final(_clean_subagent_registry: None) -> None:
    """Only a cancellation is provisional; a drained completion is not re-delivered."""
    _dispatch_child()
    http, items = await _post_child_idle(output="first result", turn_completed=True)
    assert (http, [item["status"] for item in items]) == (204, ["completed"])
    await _drain(items[0])

    http, items = await _post_child_idle(output="replayed", turn_completed=True)
    assert (http, items) == (204, [])


@pytest.mark.asyncio
async def test_new_dispatch_owns_the_next_completion(_clean_subagent_registry: None) -> None:
    """A newer send to the same child owns its completion; the old cancel is not revived."""
    _dispatch_child()
    _interrupt_child()
    await _drain_next()
    _dispatch_child(work_id="subagent_dispatch0002")

    http, items = await _post_child_idle(output="second result", turn_completed=True)

    assert http == 204
    assert [(item["work_id"], item["status"]) for item in items] == [
        ("subagent_dispatch0002", "completed")
    ]
    assert "corrected_status" not in items[0]


@pytest.mark.asyncio
async def test_deleted_parent_forgets_drained_cancellation(
    _clean_subagent_registry: None,
) -> None:
    _dispatch_child()
    _interrupt_child()
    await _drain_next()
    runner_app.unregister_subagent_work_for_session(PARENT_SESSION_ID)

    http, items = await _post_child_idle(output=SURVIVOR_VERDICT, turn_completed=True)

    assert (http, items) == (204, [])
