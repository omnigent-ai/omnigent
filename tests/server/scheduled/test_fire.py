"""Tests for the scheduled-task fire path (:mod:`omnigent.server.scheduled.fire`).

Exercises the ``on_fire`` callback the scheduler invokes when a task is due:

* **Re-read invariant** — the armed timer is never trusted; the row is re-read
  and a missing / non-active row is a logged no-op.
* **Create + grant + record** — an active task creates a conversation, writes
  the ``LEVEL_OWNER`` grant (resolving a NULL owner to ``"local"``), launches
  the runner via the injected launch seam, and records the run.
* **Fire-and-forget** — ``on_fire`` returns before the launch seam completes so
  the scheduler timer can re-arm immediately; a launch failure is swallowed and
  never propagates out of ``on_fire``.

The runner-launch integration is injected as a seam so the orchestration is
unit-tested without a live host/runner.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import pytest

from omnigent.db.db_models import current_workspace_id
from omnigent.entities import ScheduledTask
from omnigent.server.auth import LEVEL_OWNER, RESERVED_USER_LOCAL
from omnigent.server.scheduled import fire as fire_mod
from omnigent.server.scheduled.fire import FireDeps, build_on_fire

# ── Fakes ──────────────────────────────────────────────────────────────────


@dataclass
class _FakeConversation:
    id: str
    agent_id: str
    workspace: str | None = None
    host_id: str | None = None
    git_branch: str | None = None


@dataclass
class _FakeAgent:
    id: str
    bundle_location: str | None = None
    session_id: str | None = None


class FakeAgentStore:
    def __init__(self, agents: dict[str, _FakeAgent] | None = None) -> None:
        self.agents = agents or {"ag_1": _FakeAgent("ag_1")}

    def get(self, agent_id: str) -> _FakeAgent | None:
        return self.agents.get(agent_id)


class FakeScheduledTaskStore:
    """Records update/create_run calls and serves get() from a dict."""

    def __init__(self, rows: dict[str, ScheduledTask] | None = None) -> None:
        self._rows = rows or {}
        self.updates: list[dict[str, Any]] = []
        self.runs: list[dict[str, Any]] = []
        self.get_workspace_ids: list[int] = []
        self.update_workspace_ids: list[int] = []
        self.run_workspace_ids: list[int] = []

    def get(self, scheduled_task_id: str) -> ScheduledTask | None:
        self.get_workspace_ids.append(current_workspace_id())
        return self._rows.get(scheduled_task_id)

    def update(self, scheduled_task_id: str, **kwargs: Any) -> ScheduledTask | None:
        self.update_workspace_ids.append(current_workspace_id())
        self.updates.append({"id": scheduled_task_id, **kwargs})
        return self._rows.get(scheduled_task_id)

    def create_run(
        self, run_id: str, scheduled_task_id: str, status: str, scheduled_at: int, **kwargs: Any
    ) -> Any:
        self.run_workspace_ids.append(current_workspace_id())
        self.runs.append(
            {
                "run_id": run_id,
                "scheduled_task_id": scheduled_task_id,
                "status": status,
                "scheduled_at": scheduled_at,
                **kwargs,
            }
        )
        return None


class SequencedScheduledTaskStore(FakeScheduledTaskStore):
    """Returns scripted rows for consecutive get() calls."""

    def __init__(self, sequence: list[ScheduledTask | None]) -> None:
        super().__init__()
        self._sequence = sequence

    def get(self, scheduled_task_id: str) -> ScheduledTask | None:
        self.get_workspace_ids.append(current_workspace_id())
        if self._sequence:
            return self._sequence.pop(0)
        return None


class FakeConversationStore:
    def __init__(self, *, fail_create: bool = False) -> None:
        self.created: list[dict[str, Any]] = []
        self.create_workspace_ids: list[int] = []
        self._seq = 0
        self.fail_create = fail_create

    def create_conversation(self, **kwargs: Any) -> _FakeConversation:
        self.create_workspace_ids.append(current_workspace_id())
        if self.fail_create:
            raise RuntimeError("create failed")
        self._seq += 1
        conv = _FakeConversation(
            id=f"conv_{self._seq}",
            agent_id=kwargs.get("agent_id", ""),
            workspace=kwargs.get("workspace"),
            host_id=kwargs.get("host_id"),
            git_branch=kwargs.get("git_branch"),
        )
        self.created.append(kwargs)
        return conv

    def update_conversation(self, conversation_id: str, **kwargs: Any) -> _FakeConversation:
        return _FakeConversation(id=conversation_id, agent_id="")

    def get_conversation(self, conversation_id: str) -> _FakeConversation | None:
        return _FakeConversation(id=conversation_id, agent_id="ag_1")


class FakePermissionStore:
    def __init__(self, *, fail_grant: bool = False) -> None:
        self.ensured: list[str] = []
        self.grants: list[tuple[str, str, int]] = []
        self.grant_workspace_ids: list[int] = []
        self.fail_grant = fail_grant

    def ensure_user(self, user_id: str, *, is_admin: bool = False) -> None:
        self.ensured.append(user_id)

    def grant(self, user_id: str, conversation_id: str, level: int) -> Any:
        self.grant_workspace_ids.append(current_workspace_id())
        if self.fail_grant:
            raise RuntimeError("grant failed")
        self.grants.append((user_id, conversation_id, level))
        return None


@dataclass
class _FakeHost:
    host_id: str
    owner: str


class FakeHostStore:
    def __init__(self, hosts: dict[str, _FakeHost] | None = None) -> None:
        self.hosts = hosts or {}

    def get_host(self, host_id: str) -> _FakeHost | None:
        return self.hosts.get(host_id)


class FakeHostRegistry:
    def __init__(self, online: set[str] | None = None) -> None:
        self.online = online or set()

    def get(self, host_id: str) -> object | None:
        if host_id in self.online:
            return object()
        return None


def _deps(sched_store: FakeScheduledTaskStore, **overrides: Any) -> FireDeps:
    return FireDeps(
        scheduled_task_store=sched_store,
        agent_store=overrides.get("agent_store", FakeAgentStore()),
        conversation_store=overrides.get("conversation_store", FakeConversationStore()),
        permission_store=overrides.get("permission_store", FakePermissionStore()),
        host_store=overrides.get("host_store", FakeHostStore()),
        host_registry=overrides.get("host_registry", FakeHostRegistry()),
        agent_cache=overrides.get("agent_cache"),
        runner_router=overrides.get("runner_router"),
        tunnel_registry=overrides.get("tunnel_registry"),
        file_store=overrides.get("file_store"),
        artifact_store=overrides.get("artifact_store"),
    )


def _task(**overrides: Any) -> ScheduledTask:
    base: dict[str, Any] = {
        "id": "task_1",
        "name": "nightly",
        "prompt": "do the thing",
        "rrule": "FREQ=HOURLY",
        "owner_user_id": None,
        "agent_id": "ag_1",
        "timezone": "UTC",
        "created_at": 1_800_000_000,
        "workspace_id": 0,
        "state": "active",
        "execution_target": "connected_host",
        "workspace": "/repo",
        "host_id": "host_1",
    }
    base.update(overrides)
    return ScheduledTask(**base)


# ── Tests ────────────────────────────────────────────────────────────────────


async def _drain() -> None:
    """Await every in-flight background fire task to completion.

    The fire body uses ``asyncio.to_thread`` (real thread-pool round-trips), so
    a few event-loop ticks aren't enough — await the actual tasks instead.
    """
    for _ in range(50):
        pending = [t for t in fire_mod._PENDING_FIRES if not t.done()]
        if not pending:
            await asyncio.sleep(0)
            if not any(not t.done() for t in fire_mod._PENDING_FIRES):
                return
        await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_missing_row_is_noop() -> None:
    store = FakeScheduledTaskStore(rows={})  # task_1 absent
    launches: list[Any] = []

    async def _launch(conv: Any, task: Any) -> None:
        launches.append(conv)

    on_fire = build_on_fire(_deps(store), launch_dispatch=_launch)
    await on_fire(0, "task_1")
    await _drain()

    assert launches == []
    assert store.runs == []


@pytest.mark.asyncio
async def test_inactive_row_is_noop() -> None:
    store = FakeScheduledTaskStore(rows={"task_1": _task(state="paused")})
    launches: list[Any] = []

    async def _launch(conv: Any, task: Any) -> None:
        launches.append(conv)

    on_fire = build_on_fire(_deps(store), launch_dispatch=_launch)
    await on_fire(0, "task_1")
    await _drain()

    assert launches == []
    assert store.runs == []


@pytest.mark.asyncio
async def test_pause_between_on_fire_and_run_fire_is_noop() -> None:
    store = SequencedScheduledTaskStore([_task(), _task(state="paused")])
    conv_store = FakeConversationStore()
    launches: list[Any] = []

    async def _launch(conv: Any, task: Any) -> None:
        launches.append(conv)

    on_fire = build_on_fire(_deps(store, conversation_store=conv_store), launch_dispatch=_launch)
    await on_fire(0, "task_1")
    await _drain()

    assert launches == []
    assert conv_store.created == []
    assert store.runs == []


@pytest.mark.asyncio
async def test_delete_between_on_fire_and_run_fire_is_noop() -> None:
    store = SequencedScheduledTaskStore([_task(), None])
    conv_store = FakeConversationStore()
    launches: list[Any] = []

    async def _launch(conv: Any, task: Any) -> None:
        launches.append(conv)

    on_fire = build_on_fire(_deps(store, conversation_store=conv_store), launch_dispatch=_launch)
    await on_fire(0, "task_1")
    await _drain()

    assert launches == []
    assert conv_store.created == []
    assert store.runs == []


@pytest.mark.asyncio
async def test_active_creates_session_grant_and_run() -> None:
    perm = FakePermissionStore()
    conv_store = FakeConversationStore()
    store = FakeScheduledTaskStore(rows={"task_1": _task()})
    launched: list[Any] = []

    async def _launch(conv: Any, task: Any) -> None:
        launched.append((conv, task))

    on_fire = build_on_fire(
        _deps(store, permission_store=perm, conversation_store=conv_store),
        launch_dispatch=_launch,
    )
    await on_fire(0, "task_1")
    await _drain()

    # A conversation was created bound to the task's agent.
    assert len(conv_store.created) == 1
    assert conv_store.created[0]["agent_id"] == "ag_1"
    # NULL owner resolved to "local" and granted LEVEL_OWNER.
    assert perm.grants and perm.grants[0][0] == RESERVED_USER_LOCAL
    assert perm.grants[0][2] == LEVEL_OWNER
    # The launch seam was invoked.
    assert len(launched) == 1
    # A run row was recorded and last_run_* stamped on the task.
    assert len(store.runs) == 1
    assert any("last_run_at" in u for u in store.updates)
    assert any("last_run_conversation_id" in u for u in store.updates)


@pytest.mark.asyncio
async def test_fire_runs_under_task_workspace_scope() -> None:
    perm = FakePermissionStore()
    conv_store = FakeConversationStore()
    store = FakeScheduledTaskStore(rows={"task_1": _task(workspace_id=42)})

    async def _launch(conv: Any, task: Any) -> None:
        return None

    on_fire = build_on_fire(
        _deps(store, permission_store=perm, conversation_store=conv_store),
        launch_dispatch=_launch,
    )
    await on_fire(42, "task_1")
    await _drain()

    assert store.get_workspace_ids == [42, 42]
    assert conv_store.create_workspace_ids == [42]
    assert perm.grant_workspace_ids == [42]
    assert store.update_workspace_ids == [42]
    assert store.run_workspace_ids == [42]


@pytest.mark.asyncio
async def test_overlapping_fire_skips_second_launch() -> None:
    conv_store = FakeConversationStore()
    store = FakeScheduledTaskStore(rows={"task_1": _task()})
    release = asyncio.Event()

    async def _slow_launch(conv: Any, task: Any) -> None:
        await release.wait()

    on_fire = build_on_fire(
        _deps(store, conversation_store=conv_store),
        launch_dispatch=_slow_launch,
    )
    await on_fire(0, "task_1")
    await on_fire(0, "task_1")

    for _ in range(100):
        if conv_store.created:
            break
        await asyncio.sleep(0.01)
    assert len(conv_store.created) == 1
    release.set()
    await _drain()
    assert len(conv_store.created) == 1
    assert len(store.runs) == 1


@pytest.mark.asyncio
async def test_explicit_owner_is_granted() -> None:
    perm = FakePermissionStore()
    store = FakeScheduledTaskStore(rows={"task_1": _task(owner_user_id="alice@example.com")})

    async def _launch(conv: Any, task: Any) -> None:
        return None

    on_fire = build_on_fire(_deps(store, permission_store=perm), launch_dispatch=_launch)
    await on_fire(0, "task_1")
    await _drain()

    assert perm.grants and perm.grants[0][0] == "alice@example.com"


@pytest.mark.asyncio
async def test_connected_host_dispatch_uses_resolved_local_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import omnigent.server.routes._host_launch as host_launch
    import omnigent.server.routes.sessions as sessions_routes

    captured: dict[str, Any] = {}

    def _resolve_host_launch(**kwargs: Any) -> Any:
        captured["user_id"] = kwargs["user_id"]
        return type(
            "Target",
            (),
            {"conv": kwargs["conversation_store"].get_conversation("conv_1"), "conn": object()},
        )()

    async def _launch_runner_on_host(
        conv: Any, conversation_store: Any, host_registry: Any, conn: Any
    ) -> Any:
        return type("Attempt", (), {"error": None, "runner_id": "runner_1"})()

    async def _wait_for_runner_client(*args: Any, **kwargs: Any) -> object:
        return object()

    async def _ensure_runner_session_initialized(*args: Any, **kwargs: Any) -> None:
        return None

    async def _dispatch_session_event_to_runner(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(host_launch, "resolve_host_launch", _resolve_host_launch)
    monkeypatch.setattr(sessions_routes, "_launch_runner_on_host", _launch_runner_on_host)
    monkeypatch.setattr(sessions_routes, "_wait_for_runner_client", _wait_for_runner_client)
    monkeypatch.setattr(
        sessions_routes,
        "_ensure_runner_session_initialized",
        _ensure_runner_session_initialized,
    )
    monkeypatch.setattr(
        sessions_routes,
        "_dispatch_session_event_to_runner",
        _dispatch_session_event_to_runner,
    )

    store = FakeScheduledTaskStore(rows={"task_1": _task()})
    dispatch = fire_mod._make_connected_host_dispatch(
        _deps(
            store,
            conversation_store=FakeConversationStore(),
            host_store=FakeHostStore({"host_1": _FakeHost("host_1", RESERVED_USER_LOCAL)}),
            host_registry=FakeHostRegistry(online={"host_1"}),
        )
    )

    await dispatch(_FakeConversation(id="conv_1", agent_id="ag_1"), _task(owner_user_id=None))

    assert captured["user_id"] == RESERVED_USER_LOCAL


@pytest.mark.asyncio
async def test_on_fire_returns_before_launch_completes() -> None:
    """on_fire must return fast so the scheduler timer re-arms immediately."""
    store = FakeScheduledTaskStore(rows={"task_1": _task()})
    release = asyncio.Event()

    async def _slow_launch(conv: Any, task: Any) -> None:
        await release.wait()

    on_fire = build_on_fire(_deps(store), launch_dispatch=_slow_launch)

    t0 = time.monotonic()
    await on_fire(0, "task_1")
    elapsed = time.monotonic() - t0

    # Returned without waiting on the (still-blocked) launch.
    assert elapsed < 0.5
    release.set()
    await _drain()


@pytest.mark.asyncio
async def test_launch_failure_is_swallowed() -> None:
    store = FakeScheduledTaskStore(rows={"task_1": _task()})

    async def _boom(conv: Any, task: Any) -> None:
        raise RuntimeError("launch exploded")

    on_fire = build_on_fire(_deps(store), launch_dispatch=_boom)
    # Must not raise, even though the background launch throws.
    await on_fire(0, "task_1")
    await _drain()
    assert store.runs[0]["status"] == "failed"
    assert store.runs[0]["error_code"] == "launch_failed"


@pytest.mark.asyncio
async def test_validation_failure_records_failed_without_session() -> None:
    conv_store = FakeConversationStore()
    store = FakeScheduledTaskStore(rows={"task_1": _task(model_override="--danger")})

    async def _launch(conv: Any, task: Any) -> None:
        return None

    on_fire = build_on_fire(
        _deps(store, conversation_store=conv_store),
        launch_dispatch=_launch,
    )
    await on_fire(0, "task_1")
    await _drain()

    assert conv_store.created == []
    assert len(store.runs) == 1
    assert store.runs[0]["status"] == "failed"
    assert store.runs[0]["error_code"] == "invalid_input"
    assert store.runs[0]["conversation_id"] is None


@pytest.mark.asyncio
async def test_create_failure_records_failed_without_session() -> None:
    store = FakeScheduledTaskStore(rows={"task_1": _task()})

    async def _launch(conv: Any, task: Any) -> None:
        return None

    on_fire = build_on_fire(
        _deps(store, conversation_store=FakeConversationStore(fail_create=True)),
        launch_dispatch=_launch,
    )
    await on_fire(0, "task_1")
    await _drain()

    assert len(store.runs) == 1
    assert store.runs[0]["status"] == "failed"
    assert store.runs[0]["error_code"] == "session_create_failed"
    assert store.runs[0]["conversation_id"] is None


@pytest.mark.asyncio
async def test_grant_failure_records_failed_with_session() -> None:
    store = FakeScheduledTaskStore(rows={"task_1": _task()})
    perm = FakePermissionStore(fail_grant=True)

    async def _launch(conv: Any, task: Any) -> None:
        return None

    on_fire = build_on_fire(_deps(store, permission_store=perm), launch_dispatch=_launch)
    await on_fire(0, "task_1")
    await _drain()

    assert len(store.runs) == 1
    assert store.runs[0]["status"] == "failed"
    assert store.runs[0]["error_code"] == "owner_grant_failed"
    assert store.runs[0]["conversation_id"] == "conv_1"


@pytest.mark.asyncio
async def test_missing_execution_inputs_record_failed_without_session() -> None:
    conv_store = FakeConversationStore()
    store = FakeScheduledTaskStore(rows={"task_1": _task(host_id=None, workspace=None)})
    launched: list[Any] = []

    async def _launch(conv: Any, task: Any) -> None:
        launched.append(conv)

    on_fire = build_on_fire(
        _deps(store, conversation_store=conv_store),
        launch_dispatch=_launch,
    )
    await on_fire(0, "task_1")
    await _drain()

    assert launched == []
    assert conv_store.created == []
    assert len(store.runs) == 1
    assert store.runs[0]["status"] == "failed"
    assert store.runs[0]["error_code"] == "missing_host_id"
    assert store.runs[0]["conversation_id"] is None


@pytest.mark.asyncio
async def test_no_host_registry_records_failed_without_session() -> None:
    conv_store = FakeConversationStore()
    store = FakeScheduledTaskStore(rows={"task_1": _task()})

    on_fire = build_on_fire(
        _deps(store, conversation_store=conv_store, host_store=None, host_registry=None)
    )
    await on_fire(0, "task_1")
    await _drain()

    assert conv_store.created == []
    assert len(store.runs) == 1
    assert store.runs[0]["status"] == "failed"
    assert store.runs[0]["error_code"] == "host_registry_unavailable"
    assert store.runs[0]["conversation_id"] is None


@pytest.mark.asyncio
async def test_offline_connected_host_records_failed_without_session() -> None:
    conv_store = FakeConversationStore()
    store = FakeScheduledTaskStore(rows={"task_1": _task(owner_user_id="alice@example.com")})

    on_fire = build_on_fire(
        _deps(
            store,
            conversation_store=conv_store,
            host_store=FakeHostStore({"host_1": _FakeHost("host_1", "alice@example.com")}),
            host_registry=FakeHostRegistry(online=set()),
        )
    )
    await on_fire(0, "task_1")
    await _drain()

    assert conv_store.created == []
    assert len(store.runs) == 1
    assert store.runs[0]["status"] == "failed"
    assert store.runs[0]["error_code"] == "host_offline"
    assert store.runs[0]["conversation_id"] is None


@pytest.mark.asyncio
async def test_managed_sandbox_is_skipped_and_recorded() -> None:
    """Managed-sandbox targets are recorded as skipped and do not launch."""
    store = FakeScheduledTaskStore(rows={"task_1": _task(execution_target="managed_sandbox")})
    launched: list[Any] = []

    async def _launch(conv: Any, task: Any) -> None:
        launched.append(conv)

    on_fire = build_on_fire(_deps(store), launch_dispatch=_launch)
    await on_fire(0, "task_1")
    await _drain()

    assert launched == []
    assert len(store.runs) == 1
    assert store.runs[0]["status"] == "skipped"
