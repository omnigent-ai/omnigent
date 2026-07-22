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
    user_id: str


class FakeHostStore:
    def __init__(self, hosts: dict[str, _FakeHost] | None = None) -> None:
        self.hosts = hosts or {}

    def get_host(self, host_id: str) -> _FakeHost | None:
        return self.hosts.get(host_id)

    def list_hosts(self, owner: str) -> list[_FakeHost]:
        # Mirrors the real store: most-recently-active first. Insertion order in
        # the dict stands in for that ordering here.
        return [h for h in self.hosts.values() if h.user_id == owner]


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
        "user_id": None,
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
    store = FakeScheduledTaskStore(rows={"task_1": _task(user_id="alice@example.com")})

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

    await dispatch(_FakeConversation(id="conv_1", agent_id="ag_1"), _task(user_id=None))

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
async def test_unset_host_resolves_owner_online_host_and_runs() -> None:
    """An unset host_id means 'run on the owner's live host', not 'run hostless':
    the fire resolves the owner's online host, creates a session bound to it, and
    records a run."""
    perm = FakePermissionStore()
    conv_store = FakeConversationStore()
    store = FakeScheduledTaskStore(
        rows={"task_1": _task(user_id="alice@example.com", host_id=None, workspace="/repo")}
    )
    launched: list[Any] = []

    async def _launch(conv: Any, task: Any) -> None:
        launched.append((conv, task))

    on_fire = build_on_fire(
        _deps(
            store,
            permission_store=perm,
            conversation_store=conv_store,
            host_store=FakeHostStore({"host_9": _FakeHost("host_9", "alice@example.com")}),
            host_registry=FakeHostRegistry(online={"host_9"}),
        ),
        launch_dispatch=_launch,
    )
    await on_fire(0, "task_1")
    await _drain()

    # The session bound to the RESOLVED host (not None), carrying the workspace.
    assert len(conv_store.created) == 1
    assert conv_store.created[0]["host_id"] == "host_9"
    assert conv_store.created[0]["workspace"] == "/repo"
    # The dispatch saw the resolved host on its effective task.
    assert len(launched) == 1
    assert launched[0][1].host_id == "host_9"
    # A running run was recorded; the stored row keeps its null host_id.
    assert len(store.runs) == 1
    assert store.runs[0]["status"] == "running"
    assert store._rows["task_1"].host_id is None


@pytest.mark.asyncio
async def test_unset_host_no_online_host_records_failed() -> None:
    """An unset host_id with no live host is an honest failure, not a no-op: it
    records a failed run with the no_online_host code and creates no session."""
    conv_store = FakeConversationStore()
    store = FakeScheduledTaskStore(
        rows={"task_1": _task(user_id="alice@example.com", host_id=None, workspace=None)}
    )
    launched: list[Any] = []

    async def _launch(conv: Any, task: Any) -> None:
        launched.append(conv)

    on_fire = build_on_fire(
        _deps(
            store,
            conversation_store=conv_store,
            # Owner has a host, but it is offline (not in the registry).
            host_store=FakeHostStore({"host_9": _FakeHost("host_9", "alice@example.com")}),
            host_registry=FakeHostRegistry(online=set()),
        ),
        launch_dispatch=_launch,
    )
    await on_fire(0, "task_1")
    await _drain()

    assert launched == []
    assert conv_store.created == []
    assert len(store.runs) == 1
    assert store.runs[0]["status"] == "failed"
    assert store.runs[0]["error_code"] == "no_online_host"
    assert store.runs[0]["conversation_id"] is None


@pytest.mark.asyncio
async def test_no_workspace_resolved_host_launches_with_canonical_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A task with no workspace still launches: the fire resolves the host's home
    dir to an ABSOLUTE realpath (never the literal '~') and stores that."""
    conv_store = FakeConversationStore()
    store = FakeScheduledTaskStore(
        rows={"task_1": _task(user_id="alice@example.com", host_id=None, workspace=None)}
    )
    launched: list[Any] = []

    async def _launch(conv: Any, task: Any) -> None:
        launched.append((conv, task))

    # The default-workspace resolution is a host.stat round-trip; stub it to the
    # canonical home path the host would return so the fire path is exercised
    # without a live host tunnel.
    async def _fake_resolve(deps: Any, host_id: str) -> str:
        assert host_id == "host_9"
        return "/home/alice"

    monkeypatch.setattr(fire_mod, "_resolve_default_workspace", _fake_resolve)

    on_fire = build_on_fire(
        _deps(
            store,
            conversation_store=conv_store,
            host_store=FakeHostStore({"host_9": _FakeHost("host_9", "alice@example.com")}),
            host_registry=FakeHostRegistry(online={"host_9"}),
        ),
        launch_dispatch=_launch,
    )
    await on_fire(0, "task_1")
    await _drain()

    # Resolved host + absolute canonical workspace (not the literal '~').
    assert len(conv_store.created) == 1
    assert conv_store.created[0]["host_id"] == "host_9"
    assert conv_store.created[0]["workspace"] == "/home/alice"
    assert launched[0][1].workspace == "/home/alice"
    assert len(store.runs) == 1
    assert store.runs[0]["status"] == "running"


@pytest.mark.asyncio
async def test_pinned_host_no_workspace_defaults_to_host_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A task that PINS a host but omits the workspace launches on that pinned
    host with the workspace defaulted to its canonical HOME — the pinned host is
    NOT re-resolved to some other live host."""
    conv_store = FakeConversationStore()
    store = FakeScheduledTaskStore(
        rows={"task_1": _task(user_id="alice@example.com", host_id="host_pinned", workspace=None)}
    )
    launched: list[Any] = []

    async def _launch(conv: Any, task: Any) -> None:
        launched.append((conv, task))

    async def _fake_resolve(deps: Any, host_id: str) -> str:
        # Defaulting runs against the PINNED host, not a re-resolved one.
        assert host_id == "host_pinned"
        return "/home/alice"

    monkeypatch.setattr(fire_mod, "_resolve_default_workspace", _fake_resolve)

    on_fire = build_on_fire(
        _deps(
            store,
            conversation_store=conv_store,
            host_store=FakeHostStore(
                {
                    "host_pinned": _FakeHost("host_pinned", "alice@example.com"),
                    "host_other": _FakeHost("host_other", "alice@example.com"),
                }
            ),
            host_registry=FakeHostRegistry(online={"host_pinned", "host_other"}),
        ),
        launch_dispatch=_launch,
    )
    await on_fire(0, "task_1")
    await _drain()

    assert len(conv_store.created) == 1
    assert conv_store.created[0]["host_id"] == "host_pinned"
    assert conv_store.created[0]["workspace"] == "/home/alice"
    assert launched[0][1].host_id == "host_pinned"
    assert launched[0][1].workspace == "/home/alice"
    assert len(store.runs) == 1
    assert store.runs[0]["status"] == "running"


@pytest.mark.asyncio
async def test_pinned_nonowned_host_no_workspace_rejected_before_stat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pinning ANOTHER owner's online host with no workspace fails host_not_owned
    WITHOUT dispatching the default-workspace stat RPC to the non-owned host —
    ownership is authorized before any RPC reaches it."""
    conv_store = FakeConversationStore()
    store = FakeScheduledTaskStore(
        rows={"task_1": _task(user_id="alice@example.com", host_id="host_bob", workspace=None)}
    )

    resolve_calls: list[str] = []

    async def _spy_resolve(deps: Any, host_id: str) -> str:
        resolve_calls.append(host_id)
        return "/home/bob"

    monkeypatch.setattr(fire_mod, "_resolve_default_workspace", _spy_resolve)

    on_fire = build_on_fire(
        _deps(
            store,
            conversation_store=conv_store,
            # The pinned host is online but owned by bob, not alice.
            host_store=FakeHostStore({"host_bob": _FakeHost("host_bob", "bob@example.com")}),
            host_registry=FakeHostRegistry(online={"host_bob"}),
        )
    )
    await on_fire(0, "task_1")
    await _drain()

    # Rejected on ownership; NO stat RPC dispatched to the non-owned host.
    assert resolve_calls == []
    assert conv_store.created == []
    assert len(store.runs) == 1
    assert store.runs[0]["status"] == "failed"
    assert store.runs[0]["error_code"] == "host_not_owned"
    assert store.runs[0]["conversation_id"] is None


@pytest.mark.asyncio
async def test_no_workspace_unresolvable_home_records_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the host can't resolve its home dir, the fire records an honest failed
    run rather than launching with a bogus workspace."""
    conv_store = FakeConversationStore()
    store = FakeScheduledTaskStore(
        rows={"task_1": _task(user_id="alice@example.com", host_id=None, workspace=None)}
    )

    async def _boom(deps: Any, host_id: str) -> str:
        raise fire_mod._CannotLaunchScheduledFire(
            "home dir unresolved", error_code="default_workspace_unresolved"
        )

    monkeypatch.setattr(fire_mod, "_resolve_default_workspace", _boom)

    on_fire = build_on_fire(
        _deps(
            store,
            conversation_store=conv_store,
            host_store=FakeHostStore({"host_9": _FakeHost("host_9", "alice@example.com")}),
            host_registry=FakeHostRegistry(online={"host_9"}),
        )
    )
    await on_fire(0, "task_1")
    await _drain()

    assert conv_store.created == []
    assert len(store.runs) == 1
    assert store.runs[0]["status"] == "failed"
    assert store.runs[0]["error_code"] == "default_workspace_unresolved"


@pytest.mark.asyncio
async def test_defaulted_workspace_is_boundary_validated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The resolved default HOME workspace is validated against the agent's
    os_env.cwd boundary, exactly like a caller-supplied one — the check is gated
    on the RESOLVED workspace, not the (null) stored value. A boundary failure
    records a failed run and creates no session."""
    conv_store = FakeConversationStore()
    store = FakeScheduledTaskStore(
        rows={"task_1": _task(user_id="alice@example.com", host_id=None, workspace=None)}
    )

    async def _fake_resolve(deps: Any, host_id: str) -> str:
        return "/home/alice"

    seen: dict[str, Any] = {}

    async def _fake_validate(deps: Any, task: Any, *, validate_workspace: bool):
        # Record that the boundary check was requested for the resolved workspace.
        seen["validate_workspace"] = validate_workspace
        seen["workspace"] = task.workspace
        if validate_workspace:
            return ("workspace is outside the agent boundary", "invalid_input")
        return None

    monkeypatch.setattr(fire_mod, "_resolve_default_workspace", _fake_resolve)
    monkeypatch.setattr(fire_mod, "_validate_fire_session_inputs", _fake_validate)

    # No launch_dispatch override → the real preflight runs, so validation is on.
    on_fire = build_on_fire(
        _deps(
            store,
            conversation_store=conv_store,
            host_store=FakeHostStore({"host_9": _FakeHost("host_9", "alice@example.com")}),
            host_registry=FakeHostRegistry(online={"host_9"}),
        )
    )
    await on_fire(0, "task_1")
    await _drain()

    # The boundary check ran against the resolved absolute workspace.
    assert seen["validate_workspace"] is True
    assert seen["workspace"] == "/home/alice"
    # The boundary failure was recorded honestly; no session was created.
    assert conv_store.created == []
    assert len(store.runs) == 1
    assert store.runs[0]["status"] == "failed"
    assert store.runs[0]["error_code"] == "invalid_input"


@pytest.mark.asyncio
async def test_no_host_store_records_failed_when_host_unset() -> None:
    """No host store/registry configured + an unset host is an honest failure."""
    conv_store = FakeConversationStore()
    store = FakeScheduledTaskStore(rows={"task_1": _task(host_id=None, workspace=None)})

    on_fire = build_on_fire(
        _deps(store, conversation_store=conv_store, host_store=None, host_registry=None),
    )
    await on_fire(0, "task_1")
    await _drain()

    assert conv_store.created == []
    assert len(store.runs) == 1
    assert store.runs[0]["status"] == "failed"
    assert store.runs[0]["error_code"] == "host_registry_unavailable"
    assert store.runs[0]["conversation_id"] is None


@pytest.mark.asyncio
async def test_resolve_default_workspace_returns_canonical_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default workspace is the host's stat'd canonical home path, not '~'."""
    import omnigent.server.routes._workspace_validation as wsv

    captured: dict[str, Any] = {}

    async def _fake_stat(*, host_registry: Any, host_conn: Any, path: str) -> dict[str, Any]:
        captured["path"] = path
        return {
            "status": "ok",
            "exists": True,
            "type": "directory",
            "canonical_path": "/home/alice",
        }

    monkeypatch.setattr(wsv, "_ask_host_stat", _fake_stat)
    deps = _deps(
        FakeScheduledTaskStore(),
        host_registry=FakeHostRegistry(online={"host_9"}),
    )
    result = await fire_mod._resolve_default_workspace(deps, "host_9")
    assert result == "/home/alice"
    # The server sends the tilde; the host expands it (server never expands ~).
    assert captured["path"] == "~"


@pytest.mark.asyncio
async def test_resolve_default_workspace_raises_when_home_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stat that returns no canonical path is an honest launch failure."""
    import omnigent.server.routes._workspace_validation as wsv

    async def _fake_stat(*, host_registry: Any, host_conn: Any, path: str) -> dict[str, Any]:
        return {"status": "ok", "exists": False, "type": None, "canonical_path": None}

    monkeypatch.setattr(wsv, "_ask_host_stat", _fake_stat)
    deps = _deps(
        FakeScheduledTaskStore(),
        host_registry=FakeHostRegistry(online={"host_9"}),
    )
    with pytest.raises(fire_mod._CannotLaunchScheduledFire) as excinfo:
        await fire_mod._resolve_default_workspace(deps, "host_9")
    assert excinfo.value.error_code == "default_workspace_unresolved"


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
    store = FakeScheduledTaskStore(rows={"task_1": _task(user_id="alice@example.com")})

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
