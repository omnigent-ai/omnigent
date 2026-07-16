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


class FakeScheduledTaskStore:
    """Records update/create_run calls and serves get() from a dict."""

    def __init__(self, rows: dict[str, ScheduledTask] | None = None) -> None:
        self._rows = rows or {}
        self.updates: list[dict[str, Any]] = []
        self.runs: list[dict[str, Any]] = []

    def get(self, scheduled_task_id: str) -> ScheduledTask | None:
        return self._rows.get(scheduled_task_id)

    def update(self, scheduled_task_id: str, **kwargs: Any) -> ScheduledTask | None:
        self.updates.append({"id": scheduled_task_id, **kwargs})
        return self._rows.get(scheduled_task_id)

    def create_run(
        self, run_id: str, scheduled_task_id: str, status: str, scheduled_at: int, **kwargs: Any
    ) -> Any:
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


class FakeConversationStore:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self._seq = 0

    def create_conversation(self, **kwargs: Any) -> _FakeConversation:
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


class FakePermissionStore:
    def __init__(self) -> None:
        self.ensured: list[str] = []
        self.grants: list[tuple[str, str, int]] = []

    def ensure_user(self, user_id: str, *, is_admin: bool = False) -> None:
        self.ensured.append(user_id)

    def grant(self, user_id: str, conversation_id: str, level: int) -> Any:
        self.grants.append((user_id, conversation_id, level))
        return None


def _deps(sched_store: FakeScheduledTaskStore, **overrides: Any) -> FireDeps:
    return FireDeps(
        scheduled_task_store=sched_store,
        conversation_store=overrides.get("conversation_store", FakeConversationStore()),
        agent_store=overrides.get("agent_store", object()),
        permission_store=overrides.get("permission_store", FakePermissionStore()),
        host_store=overrides.get("host_store", object()),
        host_registry=overrides.get("host_registry", object()),
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
        "state": "active",
        "execution_target": "connected_host",
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
    await on_fire("task_1")
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
    await on_fire("task_1")
    await _drain()

    assert launches == []
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
    await on_fire("task_1")
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
async def test_explicit_owner_is_granted() -> None:
    perm = FakePermissionStore()
    store = FakeScheduledTaskStore(rows={"task_1": _task(owner_user_id="alice@example.com")})

    async def _launch(conv: Any, task: Any) -> None:
        return None

    on_fire = build_on_fire(_deps(store, permission_store=perm), launch_dispatch=_launch)
    await on_fire("task_1")
    await _drain()

    assert perm.grants and perm.grants[0][0] == "alice@example.com"


@pytest.mark.asyncio
async def test_on_fire_returns_before_launch_completes() -> None:
    """on_fire must return fast so the scheduler timer re-arms immediately."""
    store = FakeScheduledTaskStore(rows={"task_1": _task()})
    release = asyncio.Event()

    async def _slow_launch(conv: Any, task: Any) -> None:
        await release.wait()

    on_fire = build_on_fire(_deps(store), launch_dispatch=_slow_launch)

    t0 = time.monotonic()
    await on_fire("task_1")
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
    await on_fire("task_1")
    await _drain()


@pytest.mark.asyncio
async def test_managed_sandbox_is_skipped_and_recorded() -> None:
    """v1: managed_sandbox target logs + records a skipped run, does not launch."""
    store = FakeScheduledTaskStore(rows={"task_1": _task(execution_target="managed_sandbox")})
    launched: list[Any] = []

    async def _launch(conv: Any, task: Any) -> None:
        launched.append(conv)

    on_fire = build_on_fire(_deps(store), launch_dispatch=_launch)
    await on_fire("task_1")
    await _drain()

    assert launched == []
    assert len(store.runs) == 1
    assert store.runs[0]["status"] == "skipped"
