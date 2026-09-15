"""Tests for proactive adoption of a dead runner's orphaned sub-agent children.

A sub-agent child inherits its parent's runner at create time, and the
reactive stale-binding heal fires only when someone messages the child — with
the parent on the same dead runner, nobody ever does, so a runner death used
to strand every mid-turn child until an explicit message arrived. These tests
cover the proactive path: adoption onto an already-live same-owner runner at
reconciliation time, parking plus adoption when the owner's next runner
connects, the adoptable-population filter, and the tombstone status settle on
archive.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from omnigent.server.routes import sessions as sessions_module
from omnigent.server.routes._sessions import orchestration as orch
from omnigent.stores.conversation_store import ConversationNotFoundError


def _conv(
    session_id: str,
    *,
    kind: str = "sub_agent",
    host_id: str | None = None,
    agent_id: str | None = "ag_worker",
    runner_id: str | None = "runner_dead",
    archived: bool = False,
    labels: dict[str, str] | None = None,
    title: str | None = None,
    live_status: str | None = None,
    parent_conversation_id: str | None = None,
    root_conversation_id: str | None = None,
    harness_override: str | None = None,
) -> Any:
    """Build a conversation-shaped row exposing the fields adoption reads."""
    return SimpleNamespace(
        id=session_id,
        kind=kind,
        host_id=host_id,
        agent_id=agent_id,
        runner_id=runner_id,
        archived=archived,
        labels=labels or {},
        title=title,
        live_status=live_status,
        parent_conversation_id=parent_conversation_id,
        root_conversation_id=root_conversation_id or session_id,
        harness_override=harness_override,
    )


class _AdoptionStore:
    """Minimal conversation store recording re-binds for the adoption path."""

    def __init__(
        self,
        conv: Any = None,
        owner: str | None = None,
        *,
        owners: dict[str, str | None] | None = None,
        convs: dict[str, Any] | None = None,
        runner_bound: dict[str, list[Any]] | None = None,
    ) -> None:
        self.conv = conv
        self.owner = owner
        self.owners = owners
        self.convs = convs
        self.runner_bound = runner_bound or {}
        self.rebinds: list[tuple[str, str]] = []

    def _lookup(self, conversation_id: str) -> Any:
        if self.convs is not None:
            return self.convs.get(conversation_id)
        return self.conv

    def replace_runner_id(self, conversation_id: str, runner_id: str) -> Any:
        self.rebinds.append((conversation_id, runner_id))
        conv = self._lookup(conversation_id)
        if conv is None:
            raise ConversationNotFoundError(conversation_id)
        conv.runner_id = runner_id
        return conv

    def get_session_owner(self, conversation_id: str) -> str | None:
        if self.owners is not None:
            return self.owners.get(conversation_id)
        return self.owner

    def get_conversation(self, conversation_id: str) -> Any:
        return self._lookup(conversation_id)

    def list_conversations_by_runner_id(self, runner_id: str) -> list[Any]:
        return self.runner_bound.get(runner_id, [])


class _FakeTunnelRegistry:
    """Registry stub mapping online runner ids to their owners."""

    def __init__(
        self,
        online: dict[str, str | None],
        harnesses: dict[str, list[str]] | None = None,
    ) -> None:
        self._online = online
        self._harnesses = harnesses or {}

    def online_runner_ids(self) -> list[str]:
        return list(self._online)

    def runner_owner(self, runner_id: str) -> str | None:
        return self._online.get(runner_id)

    def get(self, runner_id: str) -> Any:
        if runner_id not in self._online:
            return None
        return SimpleNamespace(hello=SimpleNamespace(harnesses=self._harnesses.get(runner_id, [])))


class _FakeRunnerRouter:
    """Router stub resolving every session to one recorded client."""

    def __init__(self) -> None:
        self.client = object()

    def client_for_session_resources(self, conversation_id: str) -> Any:
        return SimpleNamespace(runner_id="resolved", client=self.client)


@pytest.fixture(autouse=True)
def _clear_orphan_registry() -> Any:
    """Isolate the module-level parked-orphan registry per test."""
    orch._orphaned_subagent_sessions.clear()
    yield
    orch._orphaned_subagent_sessions.clear()


def _capture_relays(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Replace the relay starter with a recorder; returns the call log."""
    relays: list[tuple[str, str]] = []

    def _record(session_id: str, runner_id: str, client: Any, store: Any = None) -> None:
        relays.append((session_id, runner_id))

    monkeypatch.setattr(orch, "_ensure_runner_relay", _record)
    return relays


@pytest.mark.asyncio
async def test_reconciliation_adopts_orphan_onto_live_same_owner_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mid-turn orphan is re-bound, initialized, and relayed at once.

    When the dead runner's reconciliation runs while a live runner owned by
    the same user is already online, the orphan must not wait for anything:
    it is adopted immediately instead of staying pinned to the dead runner.
    """
    relays = _capture_relays(monkeypatch)
    conv = _conv("c_adopt_now", live_status="running")
    store = _AdoptionStore(conv=conv, owner=None)
    inits: list[tuple[str, Any]] = []

    async def _init(adopted: Any, client: Any) -> None:
        inits.append((adopted.id, client))

    router = _FakeRunnerRouter()
    await orch._adopt_or_park_orphaned_subagents(
        [conv],
        "runner_dead",
        conversation_store=store,  # type: ignore[arg-type]
        runner_router=router,  # type: ignore[arg-type]
        tunnel_registry=_FakeTunnelRegistry({"runner_live": None}),  # type: ignore[arg-type]
        initialize_session=_init,
    )

    assert store.rebinds == [("c_adopt_now", "runner_live")]
    assert inits == [("c_adopt_now", router.client)]
    assert relays == [("c_adopt_now", "runner_live")]
    assert "c_adopt_now" not in orch._orphaned_subagent_sessions


@pytest.mark.asyncio
async def test_dead_runners_own_id_is_never_the_adoption_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The departed runner may still look online to a stale registry read.

    Adoption must skip the dead runner's own id even if the registry lists
    it, otherwise the orphan is "re-bound" right back onto its dead runner.
    """
    _capture_relays(monkeypatch)
    conv = _conv("c_skip_dead", live_status="running")
    store = _AdoptionStore(conv=conv, owner=None)

    await orch._adopt_or_park_orphaned_subagents(
        [conv],
        "runner_dead",
        conversation_store=store,  # type: ignore[arg-type]
        runner_router=_FakeRunnerRouter(),  # type: ignore[arg-type]
        tunnel_registry=_FakeTunnelRegistry({"runner_dead": None}),  # type: ignore[arg-type]
        initialize_session=None,
    )

    assert store.rebinds == []
    assert "c_skip_dead" in orch._orphaned_subagent_sessions


@pytest.mark.asyncio
async def test_parked_orphan_adopted_when_owners_runner_connects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No live runner at reconciliation parks the orphan for a later connect.

    The reported incident shape: the orchestrator's runner dies, and the
    user's next runner comes online minutes later under a fresh id. Another
    user's runner must not pick the orphan up; the owner's runner must.
    """
    relays = _capture_relays(monkeypatch)
    conv = _conv("c_parked", runner_id="runner_dead", live_status="running")
    store = _AdoptionStore(conv=conv, owner="alice@example.com")

    await orch._adopt_or_park_orphaned_subagents(
        [conv],
        "runner_dead",
        conversation_store=store,  # type: ignore[arg-type]
        runner_router=_FakeRunnerRouter(),  # type: ignore[arg-type]
        tunnel_registry=_FakeTunnelRegistry({}),  # type: ignore[arg-type]
        initialize_session=None,
    )
    assert store.rebinds == []
    parked = orch._orphaned_subagent_sessions["c_parked"]
    assert parked.dead_runner_id == "runner_dead"
    assert parked.owner == "alice@example.com"

    # Another user's runner connecting leaves the orphan parked.
    await orch._adopt_parked_orphans_onto_connected_runner(
        "runner_bob",
        conversation_store=store,  # type: ignore[arg-type]
        runner_router=_FakeRunnerRouter(),  # type: ignore[arg-type]
        tunnel_registry=_FakeTunnelRegistry(  # type: ignore[arg-type]
            {"runner_bob": "bob@example.com"}
        ),
        initialize_session=None,
    )
    assert store.rebinds == []
    assert "c_parked" in orch._orphaned_subagent_sessions

    # The owner's fresh runner adopts it and the entry is evicted.
    await orch._adopt_parked_orphans_onto_connected_runner(
        "runner_alice_2",
        conversation_store=store,  # type: ignore[arg-type]
        runner_router=_FakeRunnerRouter(),  # type: ignore[arg-type]
        tunnel_registry=_FakeTunnelRegistry(  # type: ignore[arg-type]
            {"runner_alice_2": "alice@example.com"}
        ),
        initialize_session=None,
    )
    assert store.rebinds == [("c_parked", "runner_alice_2")]
    assert relays == [("c_parked", "runner_alice_2")]
    assert "c_parked" not in orch._orphaned_subagent_sessions


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda c: setattr(c, "archived", True), "archived"),
        (lambda c: c.labels.update({"omnigent.closed": "true"}), "closed label"),
        (lambda c: setattr(c, "runner_id", "runner_other"), "healed elsewhere"),
    ],
)
async def test_parked_orphan_dropped_when_no_longer_adoptable(
    monkeypatch: pytest.MonkeyPatch,
    mutate: Any,
    reason: str,
) -> None:
    """A parked entry that went stale is dropped instead of adopted.

    Between parking and the next runner connect the child can be archived,
    tombstoned by ``sys_session_close``, or healed onto another runner; the
    drain must re-validate against the store and never revive those.
    """
    _capture_relays(monkeypatch)
    conv = _conv("c_stale", runner_id="runner_dead")
    store = _AdoptionStore(conv=conv, owner=None)
    orch._orphaned_subagent_sessions["c_stale"] = orch._OrphanedSubagent(
        dead_runner_id="runner_dead", owner=None
    )
    mutate(conv)

    await orch._adopt_parked_orphans_onto_connected_runner(
        "runner_new",
        conversation_store=store,  # type: ignore[arg-type]
        runner_router=_FakeRunnerRouter(),  # type: ignore[arg-type]
        tunnel_registry=_FakeTunnelRegistry({"runner_new": None}),  # type: ignore[arg-type]
        initialize_session=None,
    )

    assert store.rebinds == [], f"stale ({reason}) orphan was adopted"
    assert "c_stale" not in orch._orphaned_subagent_sessions


@pytest.mark.parametrize(
    ("conv", "adoptable"),
    [
        (_conv("a"), True),
        (_conv("b", kind="default"), False),
        (_conv("c", host_id="host_1"), False),
        (_conv("d", agent_id=None), False),
        (_conv("e", archived=True), False),
        (_conv("f", labels={"omnigent.closed": "true"}), False),
        (_conv("g", title="worker:task:closed:conv_g"), False),
    ],
)
def test_adoptable_population_is_plain_open_subagents(conv: Any, adoptable: bool) -> None:
    """Only plain, still-open sub-agent children may be adopted.

    Host-bound sessions have a dedicated respawn path, a top-level session's
    runner is the user-facing process itself, and a closed or archived child
    was ended on purpose — none may be silently repointed at another runner.
    """
    assert orch._subagent_is_adoptable(conv) is adoptable


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("live_status", "error_code", "orphaned"),
    [
        # Still mid-turn: the reconciliation has not settled it yet.
        ("running", None, True),
        ("waiting", None, True),
        # Already settled failed by whichever watcher won the race (the
        # per-session relay or the disconnect grace) — the persisted cause
        # is what identifies the runner's death.
        ("failed", "runner_disconnected", True),
        ("failed", "runner_failed_to_start", True),
        # A genuine task error is not an orphan and is left alone.
        ("failed", "llm_error", False),
        ("failed", None, False),
        # Idle: the child finished its work before the runner died.
        ("idle", None, False),
        (None, None, False),
    ],
)
async def test_orphan_evidence_is_mid_turn_or_runner_death_failure(
    live_status: str | None,
    error_code: str | None,
    orphaned: bool,
) -> None:
    """Orphan detection must not depend on which watcher settled the status.

    A dead runner's interrupted sessions are flipped to ``failed`` by either
    the per-session relay's give-up branch or the disconnect-grace
    reconciliation, in racy order — so both the still-mid-turn and the
    already-failed-with-runner-death-cause shapes count, while genuine task
    failures and finished work never do.
    """
    labels: dict[str, str] = {}
    if error_code is not None:
        labels = {
            sessions_module._LAST_TASK_ERROR_CODE_LABEL_KEY: error_code,
            sessions_module._LAST_TASK_ERROR_MESSAGE_LABEL_KEY: "the runner went away",
        }
    conv = _conv("c_evidence", live_status=live_status, labels=labels)
    store = _AdoptionStore(conv=conv)

    fresh = await orch._dead_runner_orphaned_subagent(conv, store)  # type: ignore[arg-type]

    assert (fresh is not None) is orphaned


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cached", "live_status", "expected"),
    [
        # A held turn on a dead runner: archive must settle it at once
        # instead of letting the disconnect grace answer "running".
        ("running", None, "idle"),
        ("waiting", None, "idle"),
        # Cache miss falls back to the persisted row value.
        (None, "running", "idle"),
        # An already-settled failure is preserved, not clobbered to idle.
        ("failed", None, "failed"),
    ],
)
async def test_archive_stop_settles_leftover_mid_turn_status(
    monkeypatch: pytest.MonkeyPatch,
    cached: str | None,
    live_status: str | None,
    expected: str,
) -> None:
    """Archiving (the ``sys_session_close`` tombstone) never leaves "running".

    A dead or wedged runner cannot deliver the best-effort stop, and nothing
    else settles the cached mid-turn status until the disconnect grace
    expires — so a tombstoned child kept reporting ``running`` for the full
    grace window. The archive teardown itself must settle it.
    """
    from omnigent.runtime import session_stream

    session_id = "c9e3a7b5f1d84e2b8c6a5d4f3e2b1a09"

    async def _noop_stop(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(sessions_module, "_best_effort_stop", _noop_stop)
    if cached is not None:
        sessions_module._session_status_cache[session_id] = cached
    store = _AdoptionStore(
        conv=_conv(session_id, runner_id="runner_dead", live_status=live_status)
    )

    try:
        await orch._archive_stop(session_id, store, None, None)  # type: ignore[arg-type]
        assert sessions_module._session_status_cache.get(session_id) == expected
    finally:
        sessions_module._session_status_cache.pop(session_id, None)
        session_stream.close(session_id)
        # The settle publish is synchronous, but give any stray task a tick.
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_child_without_direct_grant_uses_root_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sub-agent child with no direct grant matches via the root's owner.

    In authenticated deployments only the root session carries an owner
    grant; the direct lookup returns None for the child. The owner gate must
    fall back to the root session's owner, or adoption never fires exactly
    where multiple runners exist.
    """
    relays = _capture_relays(monkeypatch)
    child = _conv("c_granted", root_conversation_id="c_root", live_status="running")
    root = _conv("c_root", kind="default", runner_id="runner_dead")
    store = _AdoptionStore(
        owners={"c_root": "alice@example.com"},
        convs={"c_granted": child, "c_root": root},
    )

    await orch._adopt_or_park_orphaned_subagents(
        [child],
        "runner_dead",
        conversation_store=store,  # type: ignore[arg-type]
        runner_router=_FakeRunnerRouter(),  # type: ignore[arg-type]
        tunnel_registry=_FakeTunnelRegistry(  # type: ignore[arg-type]
            {"runner_live": "alice@example.com"}
        ),
        initialize_session=None,
    )

    assert store.rebinds == [("c_granted", "runner_live")]
    assert relays == [("c_granted", "runner_live")]
    assert "c_granted" not in orch._orphaned_subagent_sessions


@pytest.mark.asyncio
async def test_rejected_session_init_rolls_back_and_parks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed resume handshake is not an adoption.

    The re-bind is rolled back to the dead runner and the orphan parked, so
    the next candidate or connecting runner retries instead of the turn
    being silently lost.
    """
    relays = _capture_relays(monkeypatch)
    conv = _conv("c_init_fail", live_status="running")
    store = _AdoptionStore(conv=conv, owner=None)

    async def _rejecting_init(adopted: Any, client: Any) -> None:
        raise RuntimeError("runner rejected the session init")

    await orch._adopt_or_park_orphaned_subagents(
        [conv],
        "runner_dead",
        conversation_store=store,  # type: ignore[arg-type]
        runner_router=_FakeRunnerRouter(),  # type: ignore[arg-type]
        tunnel_registry=_FakeTunnelRegistry({"runner_live": None}),  # type: ignore[arg-type]
        initialize_session=_rejecting_init,
    )

    assert store.rebinds == [("c_init_fail", "runner_live"), ("c_init_fail", "runner_dead")]
    assert relays == []
    assert "c_init_fail" in orch._orphaned_subagent_sessions


@pytest.mark.asyncio
async def test_unresolvable_client_rolls_back_and_parks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No client for the new runner means the orphan stays recoverable.

    A re-bound session that was never initialized or relayed would wait for
    a message that never comes (the parent shares the dead runner), so the
    binding is rolled back and the orphan parked for retry.
    """
    from omnigent.errors import ErrorCode, OmnigentError

    relays = _capture_relays(monkeypatch)
    conv = _conv("c_no_client", live_status="running")
    store = _AdoptionStore(conv=conv, owner=None)

    class _UnroutableRouter:
        def client_for_session_resources(self, conversation_id: str) -> Any:
            raise OmnigentError("runner is offline", code=ErrorCode.RUNNER_UNAVAILABLE)

    await orch._adopt_or_park_orphaned_subagents(
        [conv],
        "runner_dead",
        conversation_store=store,  # type: ignore[arg-type]
        runner_router=_UnroutableRouter(),  # type: ignore[arg-type]
        tunnel_registry=_FakeTunnelRegistry({"runner_live": None}),  # type: ignore[arg-type]
        initialize_session=None,
    )

    assert store.rebinds == [("c_no_client", "runner_live"), ("c_no_client", "runner_dead")]
    assert relays == []
    assert "c_no_client" in orch._orphaned_subagent_sessions


@pytest.mark.asyncio
async def test_dedicated_runner_is_never_an_adoption_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host-bound session's dedicated runner must not receive orphans.

    Host-launched and managed-sandbox runners serve one session's workspace;
    resuming a foreign orphan inside that sandbox would be an isolation
    break, so the orphan parks instead.
    """
    _capture_relays(monkeypatch)
    conv = _conv("c_sandboxed", live_status="running")
    store = _AdoptionStore(
        conv=conv,
        owner=None,
        runner_bound={"runner_dedicated": [_conv("c_host", host_id="host_1")]},
    )

    await orch._adopt_or_park_orphaned_subagents(
        [conv],
        "runner_dead",
        conversation_store=store,  # type: ignore[arg-type]
        runner_router=_FakeRunnerRouter(),  # type: ignore[arg-type]
        tunnel_registry=_FakeTunnelRegistry({"runner_dedicated": None}),  # type: ignore[arg-type]
        initialize_session=None,
    )

    assert store.rebinds == []
    assert "c_sandboxed" in orch._orphaned_subagent_sessions


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("advertised", "adopted"),
    [(["codex"], False), (["claude-native"], True)],
)
async def test_candidate_must_advertise_orphans_harness(
    monkeypatch: pytest.MonkeyPatch,
    advertised: list[str],
    adopted: bool,
) -> None:
    """Adoption applies the same capability gate as pinned-runner dispatch.

    A runner that cannot spawn the orphan's harness would accept the re-bind
    and then fail the resume, so it is filtered out up front.
    """
    _capture_relays(monkeypatch)
    conv = _conv("c_harness", live_status="running", harness_override="claude-native")
    store = _AdoptionStore(conv=conv, owner=None)

    await orch._adopt_or_park_orphaned_subagents(
        [conv],
        "runner_dead",
        conversation_store=store,  # type: ignore[arg-type]
        runner_router=_FakeRunnerRouter(),  # type: ignore[arg-type]
        tunnel_registry=_FakeTunnelRegistry(  # type: ignore[arg-type]
            {"runner_live": None}, harnesses={"runner_live": advertised}
        ),
        initialize_session=None,
    )

    if adopted:
        assert store.rebinds == [("c_harness", "runner_live")]
        assert "c_harness" not in orch._orphaned_subagent_sessions
    else:
        assert store.rebinds == []
        assert "c_harness" in orch._orphaned_subagent_sessions


@pytest.mark.asyncio
async def test_live_ancestor_runner_is_preferred_over_other_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The parent's current runner wins over an arbitrary same-owner runner.

    Mirrors the reactive heal's target choice: the ancestor's runner shares
    the child's user and workspace context, so it is tried before generic
    registry order decides.
    """
    relays = _capture_relays(monkeypatch)
    child = _conv("c_child", parent_conversation_id="c_parent", live_status="running")
    parent = _conv("c_parent", kind="default", runner_id="runner_parent")
    store = _AdoptionStore(convs={"c_child": child, "c_parent": parent})

    await orch._adopt_or_park_orphaned_subagents(
        [child],
        "runner_dead",
        conversation_store=store,  # type: ignore[arg-type]
        runner_router=_FakeRunnerRouter(),  # type: ignore[arg-type]
        tunnel_registry=_FakeTunnelRegistry(  # type: ignore[arg-type]
            {"runner_other": None, "runner_parent": None}
        ),
        initialize_session=None,
    )

    assert store.rebinds == [("c_child", "runner_parent")]
    assert relays == [("c_child", "runner_parent")]


@pytest.mark.asyncio
async def test_connected_dedicated_runner_does_not_drain_parked_orphans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The parked-orphan drain applies the same target filters.

    A dedicated runner connecting (e.g. a fresh host-bound session's runner)
    must leave parked orphans parked rather than pull them into its sandbox.
    """
    _capture_relays(monkeypatch)
    conv = _conv("c_waiting", runner_id="runner_dead")
    store = _AdoptionStore(
        conv=conv,
        owner=None,
        runner_bound={"runner_dedicated": [_conv("c_host", host_id="host_1")]},
    )
    orch._orphaned_subagent_sessions["c_waiting"] = orch._OrphanedSubagent(
        dead_runner_id="runner_dead", owner=None
    )

    await orch._adopt_parked_orphans_onto_connected_runner(
        "runner_dedicated",
        conversation_store=store,  # type: ignore[arg-type]
        runner_router=_FakeRunnerRouter(),  # type: ignore[arg-type]
        tunnel_registry=_FakeTunnelRegistry({"runner_dedicated": None}),  # type: ignore[arg-type]
        initialize_session=None,
    )

    assert store.rebinds == []
    assert "c_waiting" in orch._orphaned_subagent_sessions


@pytest.mark.asyncio
async def test_archive_stop_settles_status_even_when_row_lookup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient row-lookup error must not leave the tombstone "running".

    The settle reads the status cache, so it works even when the store read
    fails and the host-runner teardown is skipped.
    """
    from omnigent.runtime import session_stream

    session_id = "a1b2c3d4e5f60718293a4b5c6d7e8f90"

    async def _noop_stop(*args: Any, **kwargs: Any) -> None:
        return None

    class _RaisingStore:
        def get_conversation(self, conversation_id: str) -> Any:
            raise RuntimeError("store unavailable")

    monkeypatch.setattr(sessions_module, "_best_effort_stop", _noop_stop)
    sessions_module._session_status_cache[session_id] = "running"

    try:
        await orch._archive_stop(session_id, _RaisingStore(), None, None)  # type: ignore[arg-type]
        assert sessions_module._session_status_cache.get(session_id) == "idle"
    finally:
        sessions_module._session_status_cache.pop(session_id, None)
        session_stream.close(session_id)
        await asyncio.sleep(0)
