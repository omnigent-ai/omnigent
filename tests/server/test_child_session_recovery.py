"""Recovery restores active descendants without replaying finished work."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from omnigent.db.utils import generate_agent_id
from omnigent.entities import Conversation
from omnigent.server.child_session_recovery import (
    RECOVERY_STORE_CONCURRENCY,
    restore_active_children,
)
from omnigent.server.runner_session_init import RunnerSessionInitializer
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


@pytest.fixture
def recovery_tree(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> tuple[
    SqlAlchemyConversationStore,
    Conversation,
    Callable[..., Conversation],
    Mock,
    AsyncMock,
    RunnerSessionInitializer,
]:
    from omnigent.server.routes import sessions

    store = SqlAlchemyConversationStore(db_uri)
    agent = SqlAlchemyAgentStore(db_uri).create(generate_agent_id(), "test", "bundle")
    parent = store.create_conversation(runner_id="new", agent_id=agent.id)
    relay, recovered = Mock(), AsyncMock()
    monkeypatch.setattr(sessions, "_ensure_runner_relay", relay)
    monkeypatch.setattr(sessions, "_ensure_runner_relay_ready", AsyncMock())
    monkeypatch.setattr(sessions, "_publish_runner_recovered_status", recovered)
    monkeypatch.setattr("omnigent.runtime.get_runner_router", lambda: None)

    def child(
        status: str = "running", *, owner: Conversation = parent, **kwargs: Any
    ) -> Conversation:
        row = store.create_conversation(
            kind="sub_agent",
            parent_conversation_id=owner.id,
            agent_id=agent.id,
            runner_id="old",
            **kwargs,
        )
        store.set_session_live_status(row.id, status)
        return store.get_conversation(row.id)  # type: ignore[return-value]

    initializer = RunnerSessionInitializer(Mock(get=lambda _: None), server_version="test")
    return store, parent, child, relay, recovered, initializer


@pytest.mark.asyncio
async def test_restore_active_descendants_and_idle_ancestor(recovery_tree: Any) -> None:
    store, parent, child, relay, recovered, initializer = recovery_tree
    active = child()
    waiting = child("waiting")
    disconnected = child("failed")
    store.set_labels(
        disconnected.id,
        {
            "omnigent.last_task_error_code": "runner_disconnected",
            "omnigent.last_task_error_message": "Disconnected",
        },
    )
    idle_ancestor = child("idle")
    nested = child(owner=idle_ancestor)
    untouched = [child("idle"), child("failed")]
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        assert store.get_conversation(body["session_id"]).runner_id == "new"
        return httpx.Response(201)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://runner"
    ) as client:
        await restore_active_children(parent, client, store, initializer)

    by_id = {call["session_id"]: call["session_init"] for call in calls}
    assert set(by_id) == {active.id, waiting.id, disconnected.id, idle_ancestor.id, nested.id}
    assert {sid for sid, envelope in by_id.items() if envelope["suppress_recovery_turn"]} == {
        idle_ancestor.id
    }
    assert {sid for sid, envelope in by_id.items() if envelope["resume_interrupted_turn"]} == {
        active.id,
        waiting.id,
        disconnected.id,
        nested.id,
    }
    ids = list(by_id)
    assert ids.index(idle_ancestor.id) < ids.index(nested.id)
    assert relay.call_count == 5
    recovered.assert_not_awaited()
    assert all(store.get_conversation(row.id).runner_id == "old" for row in untouched)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exclusion", ["closed", "archived", "stopped", "fenced", "hosted", "side_chat", "live_runner"]
)
async def test_do_not_restore_excluded_children(
    recovery_tree: Any, monkeypatch: pytest.MonkeyPatch, exclusion: str
) -> None:
    from omnigent.host.shutdown import ShutdownIntent
    from omnigent.server import shutdown_attribution
    from omnigent.server.routes._sessions.common import _interrupt_fenced_sessions

    store, parent, child, relay, _, initializer = recovery_tree
    row = child()
    if exclusion == "closed":
        store.set_labels(row.id, {"omnigent.closed": "true"})
    elif exclusion == "archived":
        store.update_conversation(row.id, archived=True)
    elif exclusion == "stopped":
        await shutdown_attribution.begin_connection(
            row.id, row.runner_id, "stopped_connection", store
        )
        await shutdown_attribution.record_session_shutdown(
            row,
            ShutdownIntent(
                reason="user_stopped_session",
                action="stop_session",
                initiator="authenticated_user",
            ),
            store,
        )
    elif exclusion == "fenced":
        _interrupt_fenced_sessions.add(row.id)
    elif exclusion == "hosted":
        store.set_host_id(row.id, "a" * 32, workspace="/tmp")
    elif exclusion == "side_chat":
        store.set_labels(row.id, {"omnigent.codex_native.agent_nickname": "Side chat"})
    else:
        monkeypatch.setattr(
            "omnigent.runtime.get_runner_router", lambda: Mock(runner_is_online=lambda _: True)
        )
    try:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: pytest.fail("unexpected init"))
        ) as client:
            await restore_active_children(parent, client, store, initializer)
        assert store.get_conversation(row.id).runner_id == "old"
        relay.assert_not_called()
    finally:
        shutdown_attribution.session_shutdowns.pop(row.id, None)
        shutdown_attribution.session_scopes.pop(row.id, None)
        _interrupt_fenced_sessions.discard(row.id)


@pytest.mark.asyncio
async def test_child_stop_for_an_older_runner_does_not_block_recovery(recovery_tree: Any) -> None:
    from omnigent.host.shutdown import ShutdownIntent
    from omnigent.server import shutdown_attribution

    store, parent, child, relay, _, initializer = recovery_tree
    row = child()
    store.replace_runner_id(row.id, "previously-stopped")
    await shutdown_attribution.begin_connection(
        row.id, "previously-stopped", "previous-connection", store
    )
    evidence = await shutdown_attribution.record_session_shutdown(
        store.get_conversation(row.id),
        ShutdownIntent(
            reason="user_stopped_session", action="stop_session", initiator="authenticated_user"
        ),
        store,
    )
    assert evidence is not None
    store.replace_runner_id(row.id, row.runner_id)
    try:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(201)),
            base_url="http://runner",
        ) as client:
            await restore_active_children(parent, client, store, initializer)
        assert store.get_conversation(row.id).runner_id == "new"
        relay.assert_called_once()
    finally:
        shutdown_attribution.session_shutdowns.pop(row.id, None)
        shutdown_attribution.session_scopes.pop(row.id, None)


@pytest.mark.asyncio
async def test_mirror_rebinds_without_independent_terminal_or_success_status(
    recovery_tree: Any,
) -> None:
    store, parent, child, relay, recovered, initializer = recovery_tree
    row = child()
    store.set_labels(row.id, {"omnigent.wrapper": "codex-native-ui-subagent"})
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("mirror initialized"))
    ) as client:
        await restore_active_children(parent, client, store, initializer)
    assert store.get_conversation(row.id).runner_id == "new"
    relay.assert_called_once()
    recovered.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_child_init_does_not_recover_its_descendants_or_block_siblings(
    recovery_tree: Any,
) -> None:
    store, parent, child, relay, recovered, initializer = recovery_tree
    failed = child()
    nested = child(owner=failed)
    sibling = child()
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        session_id = json.loads(request.content)["session_id"]
        calls.append(session_id)
        return httpx.Response(503 if session_id == failed.id else 201)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://runner"
    ) as client:
        await restore_active_children(parent, client, store, initializer)
    assert set(calls) == {failed.id, sibling.id}
    assert store.get_conversation(nested.id).runner_id == "old"
    assert relay.call_count == 1
    recovered.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_rebind_is_preserved(
    recovery_tree: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, parent, child, relay, _, initializer = recovery_tree
    row = child()
    replace = store.replace_runner_id

    def competing_rebind(session_id: str, runner_id: str, **kwargs: Any) -> Conversation:
        replace(session_id, "manual")
        return replace(session_id, runner_id, **kwargs)

    monkeypatch.setattr(store, "replace_runner_id", competing_rebind)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("stale init"))
    ) as client:
        await restore_active_children(parent, client, store, initializer)
    assert store.get_conversation(row.id).runner_id == "manual"
    relay.assert_not_called()


@pytest.mark.asyncio
async def test_child_finishing_after_scan_is_not_restored(
    recovery_tree: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.server.routes._sessions.common import _session_status_cache

    store, parent, child, relay, _, initializer = recovery_tree
    row = child()
    get = store.get_conversation

    def finish_before_recheck(session_id: str) -> Conversation | None:
        if session_id == row.id:
            store.set_session_live_status(row.id, "idle")
            _session_status_cache[row.id] = "idle"
        return get(session_id)

    monkeypatch.setattr(store, "get_conversation", finish_before_recheck)
    try:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: pytest.fail("finished child initialized"))
        ) as client:
            await restore_active_children(parent, client, store, initializer)
        assert get(row.id).runner_id == "old"
        relay.assert_not_called()
    finally:
        _session_status_cache.pop(row.id, None)


@pytest.mark.asyncio
async def test_old_runner_reconnecting_during_scan_is_not_rebound(
    recovery_tree: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, parent, child, relay, _, initializer = recovery_tree
    row = child()
    online = Mock(side_effect=[False, True])
    monkeypatch.setattr(
        "omnigent.runtime.get_runner_router", lambda: Mock(runner_is_online=online)
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("live child initialized"))
    ) as client:
        await restore_active_children(parent, client, store, initializer)
    assert store.get_conversation(row.id).runner_id == "old"
    relay.assert_not_called()


@pytest.mark.asyncio
async def test_repeated_disconnects_and_return_to_used_runner_keep_child_pending(
    recovery_tree: Any,
) -> None:
    from omnigent.server.routes import sessions
    from omnigent.server.schemas import ErrorDetail

    store, parent, child, _, recovered, initializer = recovery_tree
    row = child("failed")
    store.set_labels(
        row.id,
        {
            "omnigent.last_task_error_code": "runner_disconnected",
            "omnigent.last_task_error_message": "Disconnected",
        },
    )
    bodies = []

    def respond(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(201)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://runner"
    ) as client:
        for runner_id in ("A", "B", "A"):
            parent = store.replace_runner_id(parent.id, runner_id)
            await restore_active_children(parent, client, store, initializer)
            await restore_active_children(parent, client, store, initializer)
            fresh = store.get_conversation(row.id)
            assert fresh.live_status == "failed", "initialization must not imply task completion"
            await sessions._mark_runner_sessions_offline(
                [fresh], ErrorDetail(code="runner_disconnected", message="Disconnected"), store
            )
            assert store.get_conversation(row.id).labels["omnigent.last_task_error_code"]
    assert [body["session_id"] for body in bodies] == [row.id] * 3
    assert len({body["session_init"]["recovery_id"] for body in bodies}) == 3
    recovered.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_recovery_does_not_allocate_a_second_continuation(
    recovery_tree: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    from types import SimpleNamespace

    from omnigent.server import child_session_recovery as recovery

    store, parent, child, _, _, initializer = recovery_tree
    row = child()
    first_rebind = asyncio.Event()
    release_rebind = asyncio.Event()
    first_init_finished = asyncio.Event()
    replacements = 0
    requests = []

    async def scheduled_store_call(call: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal replacements
        if call == store.replace_runner_id:
            replacements += 1
            if replacements == 1:
                first_rebind.set()
                await release_rebind.wait()
            else:
                # Deliver the competing rebind only after the first continuation ended.
                await first_init_finished.wait()
        return call(*args, **kwargs)

    # Control this module's store scheduling without changing asyncio globally.
    monkeypatch.setattr(
        recovery,
        "asyncio",
        SimpleNamespace(**(vars(asyncio) | {"to_thread": scheduled_store_call})),
    )

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        first_init_finished.set()
        return httpx.Response(201)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://runner"
    ) as client:
        first = asyncio.create_task(restore_active_children(parent, client, store, initializer))
        await first_rebind.wait()
        second = asyncio.create_task(restore_active_children(parent, client, store, initializer))
        await asyncio.sleep(0)
        release_rebind.set()
        await asyncio.gather(first, second)
    assert store.get_conversation(row.id).runner_id == parent.runner_id
    assert len(requests) == 1, [r["session_init"]["recovery_id"] for r in requests]


@pytest.mark.asyncio
async def test_scheduled_restoration_is_shared_only_within_one_generation(
    recovery_tree: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.server import child_session_recovery as recovery

    store, parent, _, _, _, _ = recovery_tree
    connection = SimpleNamespace(generation=1)
    initializer = RunnerSessionInitializer(Mock(get=lambda _: connection), server_version="test")
    pending: dict[object, asyncio.Task[None]] = {}
    started: asyncio.Queue[int] = asyncio.Queue()
    release = asyncio.Event()
    completed: list[int] = []

    async def restore(*_: Any, generation: int) -> None:
        started.put_nowait(generation)
        await release.wait()
        completed.append(generation)

    monkeypatch.setattr(recovery, "_restoration_tasks", pending)
    monkeypatch.setattr(recovery, "restore_active_children", restore)
    async with httpx.AsyncClient() as client:
        try:
            recovery.schedule_child_restoration(parent, client, store, initializer)
            assert await asyncio.wait_for(started.get(), 5) == 1
            recovery.schedule_child_restoration(parent, client, store, initializer)
            connection.generation = 2
            recovery.schedule_child_restoration(parent, client, store, initializer)
            assert await asyncio.wait_for(started.get(), 5) == 2
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(*pending.values()), 5)

    assert sorted(completed) == [1, 2]
    assert not pending


@pytest.mark.asyncio
async def test_message_handshake_does_not_wait_for_child_initialization(
    recovery_tree: Any,
) -> None:
    """Parent messages can proceed while a slow child's restoration continues."""
    import asyncio

    from omnigent.server.routes import sessions

    store, parent, child, relay, _, initializer = recovery_tree
    row = child()
    entered, release, restored = asyncio.Event(), asyncio.Event(), asyncio.Event()
    requests = []
    relay.side_effect = lambda *_args, **_kwargs: restored.set()

    async def respond(request: httpx.Request) -> httpx.Response:
        session_id = json.loads(request.content)["session_id"]
        requests.append(session_id)
        if session_id == row.id:
            entered.set()
            await release.wait()
        return httpx.Response(201, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://runner"
    ) as client:
        handshake = asyncio.create_task(
            sessions._ensure_runner_session_initialized(
                parent.id, parent, client, store, initializer, suppress_recovery_turn=True
            )
        )
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            assert handshake.done(), "slow child initialization blocked the parent's message"
        finally:
            release.set()
            await asyncio.wait_for(handshake, timeout=5)
            await asyncio.wait_for(restored.wait(), timeout=5)
    assert requests == [parent.id, row.id]
    assert store.get_conversation(row.id).runner_id == parent.runner_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("child_owner", "mirrored"),
    [(owner, False) for owner in ("owner", "other", None, "read", "edit", "manage")]
    + [(owner, True) for owner in ("owner", "other", None, "read")],
)
@pytest.mark.parametrize("same_binding", [False, True])
async def test_restoration_respects_runner_ownership(
    recovery_tree: Any,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    child_owner: str | None,
    same_binding: bool,
    mirrored: bool,
) -> None:
    """A child's direct owner must match the destination runner's owner."""
    from omnigent.server.auth import LEVEL_EDIT, LEVEL_MANAGE, LEVEL_OWNER, LEVEL_READ
    from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

    store, parent, child, relay, _, initializer = recovery_tree
    row = child()
    if mirrored:
        store.set_labels(row.id, {"omnigent.wrapper": "codex-native-ui-subagent"})
    nested = child(owner=row)
    if same_binding:
        store.replace_runner_id(row.id, "new")
    permissions = SqlAlchemyPermissionStore(db_uri)
    for user in ("owner", "other"):
        permissions.ensure_user(user)
    permissions.grant("owner", parent.id, LEVEL_OWNER)
    permissions.grant("other", parent.id, LEVEL_READ)
    shared_levels = {"read": LEVEL_READ, "edit": LEVEL_EDIT, "manage": LEVEL_MANAGE}
    if child_owner in shared_levels:
        permissions.grant("other", row.id, shared_levels[child_owner])
        assert store.get_session_owner(row.id) == "other"
    elif child_owner is not None:
        permissions.grant(child_owner, row.id, LEVEL_OWNER)
    monkeypatch.setattr(
        "omnigent.runtime.get_runner_router",
        lambda: Mock(runner_is_online=lambda rid: rid == "new", runner_owner=lambda _: "owner"),
    )
    initialized = []

    def respond(request: httpx.Request) -> httpx.Response:
        initialized.append(json.loads(request.content)["session_id"])
        return httpx.Response(201)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://runner"
    ) as client:
        await restore_active_children(parent, client, store, initializer)
    if child_owner == "other":
        assert initialized == []
        assert store.get_conversation(row.id).runner_id == ("new" if same_binding else "old")
        assert store.get_conversation(nested.id).runner_id == "old"
        relay.assert_not_called()
    else:
        assert initialized == ([nested.id] if mirrored else [row.id, nested.id])
        assert store.get_conversation(nested.id).runner_id == "new"


@pytest.mark.asyncio
@pytest.mark.parametrize("transport_error", [False, True])
async def test_parent_recovery_published_before_descendant_store_failure(
    recovery_tree: Any, monkeypatch: pytest.MonkeyPatch, transport_error: bool
) -> None:
    """A failed descendant lookup must not obscure a successful parent handshake."""
    from sqlalchemy.exc import OperationalError

    from omnigent.server.routes import sessions

    store, parent, child, _, recovered, initializer = recovery_tree
    child()
    failure = (
        ConnectionError("descendant lookup failed")
        if transport_error
        else OperationalError("child lookup", {}, RuntimeError("database unavailable"))
    )

    ready = AsyncMock()
    monkeypatch.setattr(sessions, "_ensure_runner_relay_ready", ready)

    def fail_lookup(*_args: Any) -> None:
        recovered.assert_awaited_once_with(parent.id, store)
        ready.assert_awaited_once_with(
            parent.id, parent.runner_id, client, store, conversation=parent
        )
        raise failure

    monkeypatch.setattr(store, "list_child_conversation_ids_by_parent", fail_lookup)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(201, json={})),
        base_url="http://runner",
    ) as client:
        with pytest.raises(type(failure)) as caught:
            await sessions._ensure_runner_session_initialized(
                parent.id, parent, client, store, initializer, require_success=True
            )
    assert caught.value is failure


@pytest.mark.asyncio
async def test_hung_child_does_not_block_sibling_or_its_descendants(recovery_tree: Any) -> None:
    store, parent, child, relay, recovered, initializer = recovery_tree
    hung = child()
    blocked_descendant = child(owner=hung)
    slow = child()
    healthy_descendant = child(owner=slow)
    entered = {row.id: asyncio.Event() for row in (hung, slow)}
    release = asyncio.Event()
    restored = asyncio.Event()
    stopped: set[str] = set()
    requested = []
    relay.side_effect = lambda sid, *_, **__: (
        restored.set() if sid == healthy_descendant.id else None
    )

    async def respond(request: httpx.Request) -> httpx.Response:
        sid = json.loads(request.content)["session_id"]
        requested.append(sid)
        if sid in entered:
            entered[sid].set()
            try:
                await (asyncio.Event() if sid == hung.id else release).wait()
            finally:
                stopped.add(sid)
        return httpx.Response(201)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://runner"
    ) as client:
        task = asyncio.create_task(restore_active_children(parent, client, store, initializer))
        try:
            await asyncio.wait_for(asyncio.gather(*(e.wait() for e in entered.values())), 5)
            assert not restored.is_set()
            release.set()
            await asyncio.wait_for(restored.wait(), 5)
            assert not task.done(), "the hung child's work should still be pending"
            assert blocked_descendant.id not in requested
            assert store.get_conversation(blocked_descendant.id).runner_id == "old"
            recovered.assert_not_awaited()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.gather(*initializer.invalidate_runner("new"), return_exceptions=True)
    assert stopped == set(entered)
    assert not initializer.invalidate_runner("new")


@pytest.mark.asyncio
@pytest.mark.parametrize("child_owner", ["owner", "other", None])
async def test_same_runner_mirrors_check_ownership_without_individual_reads_or_initialization(
    recovery_tree: Any,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    child_owner: str | None,
) -> None:
    from omnigent.server.auth import LEVEL_OWNER
    from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

    store, parent, child, relay, recovered, initializer = recovery_tree
    mirror = child()
    nested = child(owner=mirror)
    for row in (mirror, nested):
        store.replace_runner_id(row.id, "new")
        store.set_labels(row.id, {"omnigent.wrapper": "codex-native-ui-subagent"})
    permissions = SqlAlchemyPermissionStore(db_uri)
    for user in ("owner", "other"):
        permissions.ensure_user(user)
    permissions.grant("owner", parent.id, LEVEL_OWNER)
    if child_owner is not None:
        permissions.grant(child_owner, mirror.id, LEVEL_OWNER)
    monkeypatch.setattr(
        "omnigent.runtime.get_runner_router",
        lambda: Mock(runner_owner=lambda _: "owner"),
    )
    monkeypatch.setattr(store, "get_conversation", lambda *_: pytest.fail("per-child read"))
    initialized: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        initialized.append(json.loads(request.content)["session_id"])
        return httpx.Response(201)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://runner"
    ) as client:
        await restore_active_children(parent, client, store, initializer)
    expected = [] if child_owner == "other" else [mirror.id, nested.id]
    assert [call.args[0] for call in relay.call_args_list] == expected
    assert initialized == []
    recovered.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["rebound", "closed", "archived", "finished"])
@pytest.mark.parametrize("phase", ["ancestor", "ownership"])
async def test_mirror_changed_while_recovery_waits_is_not_attached(
    recovery_tree: Any, monkeypatch: pytest.MonkeyPatch, change: str, phase: str
) -> None:
    store, parent, child, relay, _, initializer = recovery_tree
    ancestor = child()
    mirror, healthy = child(owner=ancestor), child(owner=ancestor)
    for row in (mirror, healthy):
        store.replace_runner_id(row.id, "new")
        store.set_labels(row.id, {"omnigent.wrapper": "codex-native-ui-subagent"})
    entered, release = asyncio.Event(), asyncio.Event()
    requested: list[str] = []
    bindings = {mirror.id: "new"}
    relay.side_effect = lambda sid, rid, *_, **__: bindings.update({sid: rid})

    def change_mirror() -> None:
        if change == "rebound":
            store.replace_runner_id(mirror.id, "elsewhere")
            bindings[mirror.id] = "elsewhere"
        elif change == "closed":
            store.set_labels(mirror.id, {"omnigent.closed": "true"})
        elif change == "archived":
            store.update_conversation(mirror.id, archived=True)
        else:
            store.set_session_live_status(mirror.id, "idle")

    if phase == "ownership":
        get_owner = store.get_session_owner

        def change_during_ownership_lookup(sid: str, **kwargs: Any) -> str | None:
            if sid == mirror.id:
                change_mirror()
            return get_owner(sid, **kwargs)

        monkeypatch.setattr(store, "get_session_owner", change_during_ownership_lookup)
        monkeypatch.setattr(
            "omnigent.runtime.get_runner_router",
            lambda: Mock(
                runner_owner=lambda _: "owner", runner_is_online=lambda rid: rid == "new"
            ),
        )

    async def respond(request: httpx.Request) -> httpx.Response:
        requested.append(json.loads(request.content)["session_id"])
        entered.set()
        await release.wait()
        return httpx.Response(201)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://runner"
    ) as client:
        task = asyncio.create_task(restore_active_children(parent, client, store, initializer))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            if phase == "ancestor":
                change_mirror()
            release.set()
            await asyncio.wait_for(task, 5)
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert requested == [ancestor.id]
    assert {call.args[0] for call in relay.call_args_list} == {ancestor.id, healthy.id}
    if change == "rebound":
        assert bindings[mirror.id] == "elsewhere"


@pytest.mark.asyncio
async def test_mirror_lookup_failure_does_not_cancel_executable_sibling(
    recovery_tree: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    store, parent, child, relay, _, initializer = recovery_tree
    mirrors, executable = [child(), child()], child()
    for mirror in mirrors:
        store.replace_runner_id(mirror.id, "new")
        store.set_labels(mirror.id, {"omnigent.wrapper": "codex-native-ui-subagent"})
    get_many = store.get_conversations
    failure = RuntimeError("mirror lookup failed")

    def fail_mirror_lookup(ids: list[str]) -> dict[str, Conversation]:
        if parent.id in ids:
            raise failure
        return get_many(ids)

    monkeypatch.setattr(store, "get_conversations", fail_mirror_lookup)
    requested: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requested.append(json.loads(request.content)["session_id"])
        return httpx.Response(201)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://runner"
    ) as client:
        await asyncio.wait_for(restore_active_children(parent, client, store, initializer), 5)
    assert requested == [executable.id]
    assert [call.args[0] for call in relay.call_args_list] == [executable.id]
    assert {
        row.id
        for row in mirrors
        if any(
            row.id in record.getMessage()
            and record.exc_info is not None
            and record.exc_info[1] is failure
            for record in caplog.records
        )
    } == {row.id for row in mirrors}


@pytest.mark.asyncio
async def test_cancellation_joins_pending_mirror_revalidation(
    recovery_tree: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.server import child_session_recovery as recovery

    store, parent, child, relay, _, initializer = recovery_tree
    for _ in range(2):
        mirror = child()
        store.replace_runner_id(mirror.id, "new")
        store.set_labels(mirror.id, {"omnigent.wrapper": "codex-native-ui-subagent"})
    entered, stopped = asyncio.Event(), asyncio.Event()

    async def scheduled_store_call(call: Any, *args: Any, **kwargs: Any) -> Any:
        if call == store.get_conversations and parent.id in args[0]:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        return await asyncio.to_thread(call, *args, **kwargs)

    monkeypatch.setattr(
        recovery,
        "asyncio",
        SimpleNamespace(**(vars(asyncio) | {"to_thread": scheduled_store_call})),
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("mirror initialized"))
    ) as client:
        task = asyncio.create_task(restore_active_children(parent, client, store, initializer))
        try:
            await asyncio.wait_for(entered.wait(), 5)
        finally:
            task.cancel()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
    assert stopped.is_set()
    relay.assert_not_called()


@pytest.mark.asyncio
async def test_store_fanout_is_bounded_without_blocking_on_hung_initializations(
    recovery_tree: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.server import child_session_recovery as recovery

    store, parent, child, relay, _, initializer = recovery_tree
    rows = [child() for _ in range(24)]
    healthy = rows[-1]
    restored = asyncio.Event()
    all_requested = asyncio.Event()
    requested: set[str] = set()
    pending_reads = peak_reads = 0

    async def scheduled_store_call(call: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal pending_reads, peak_reads
        pending_reads += 1
        peak_reads = max(peak_reads, pending_reads)
        try:
            await asyncio.sleep(0)
            return call(*args, **kwargs)
        finally:
            pending_reads -= 1

    monkeypatch.setattr(
        recovery,
        "asyncio",
        SimpleNamespace(**(vars(asyncio) | {"to_thread": scheduled_store_call})),
    )
    relay.side_effect = lambda sid, *_, **__: restored.set() if sid == healthy.id else None

    async def respond(request: httpx.Request) -> httpx.Response:
        sid = json.loads(request.content)["session_id"]
        requested.add(sid)
        if len(requested) == len(rows):
            all_requested.set()
        if sid != healthy.id:
            await asyncio.Event().wait()
        return httpx.Response(201)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://runner"
    ) as client:
        task = asyncio.create_task(restore_active_children(parent, client, store, initializer))
        try:
            await asyncio.wait_for(asyncio.gather(restored.wait(), all_requested.wait()), 5)
            assert requested == {row.id for row in rows}
            assert 1 < peak_reads <= RECOVERY_STORE_CONCURRENCY
            assert not task.done(), "hung initialization must not occupy a store slot"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.gather(*initializer.invalidate_runner("new"), return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["start", "scan", "read", "initialize"])
async def test_replaced_tunnel_stops_child_recovery_without_error_tracebacks(
    recovery_tree: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    phase: str,
) -> None:
    store, parent, child, relay, _, _ = recovery_tree
    rows = [child() for _ in range(12)]
    registry = SimpleNamespace(connection=SimpleNamespace(generation=1))
    initializer = RunnerSessionInitializer(
        Mock(get=lambda _: registry.connection), server_version="test"
    )
    method_name = (
        "get_conversation" if phase == "read" else "list_child_conversation_ids_by_parent"
    )
    real_read = getattr(store, method_name)

    def read_then_replace(*args: Any, **kwargs: Any) -> Any:
        result = real_read(*args, **kwargs)
        registry.connection = SimpleNamespace(generation=2)
        return result

    if phase == "start":
        registry.connection = SimpleNamespace(generation=2)
    elif phase != "initialize":
        monkeypatch.setattr(store, method_name, read_then_replace)

    def respond(_: httpx.Request) -> httpx.Response:
        registry.connection = SimpleNamespace(generation=2)
        return httpx.Response(201)

    caplog.set_level(logging.INFO, logger="omnigent.server.child_session_recovery")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://runner"
    ) as client:
        await restore_active_children(parent, client, store, initializer, generation=1)
    relay.assert_not_called()
    failures = [
        record
        for record in caplog.records
        if record.name == "omnigent.server.child_session_recovery"
        and record.levelno >= logging.WARNING
    ]
    assert not failures
    if phase != "initialize":
        fresh = store.get_conversations([row.id for row in rows])
        assert all(row.runner_id == "old" for row in fresh.values())


@pytest.mark.asyncio
async def test_current_tunnel_store_connection_failure_remains_visible(
    recovery_tree: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    store, parent, child, relay, _, initializer = recovery_tree
    row = child()
    failure = ConnectionError("store unavailable")
    monkeypatch.setattr(store, "get_conversation", Mock(side_effect=failure))
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("failed lookup initialized"))
    ) as client:
        await restore_active_children(parent, client, store, initializer)
    relay.assert_not_called()
    assert any(
        record.levelno == logging.ERROR
        and row.id in record.getMessage()
        and record.exc_info is not None
        and record.exc_info[1] is failure
        for record in caplog.records
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("destination_inflight", [False, True])
async def test_rebinding_joins_old_initialization_without_blocking_siblings(
    recovery_tree: Any,
    monkeypatch: pytest.MonkeyPatch,
    destination_inflight: bool,
) -> None:
    from omnigent.server import child_session_recovery as recovery_module

    store, parent, child, relay, _, initializer = recovery_tree
    row, sibling = child(), child()
    entered, retiring, release, retired = (asyncio.Event() for _ in range(4))
    sibling_restored = asyncio.Event()
    requested: set[str] = set()
    initialized_after_retirement: list[bool] = []
    destination_entered, destination_release = asyncio.Event(), asyncio.Event()
    destination: asyncio.Task[httpx.Response] | None = None
    relay.side_effect = lambda sid, *_, **__: sibling_restored.set() if sid == sibling.id else None

    async def start_destination_after_rebind(call: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal destination
        result = await asyncio.to_thread(call, *args, **kwargs)
        if destination_inflight and call == store.replace_runner_id and args[0] == row.id:
            destination = asyncio.create_task(
                initializer.initialize(
                    result, new_client, timeout=10, resume_interrupted_turn=True
                )
            )
            await destination_entered.wait()
        return result

    monkeypatch.setattr(
        recovery_module,
        "asyncio",
        SimpleNamespace(**(vars(asyncio) | {"to_thread": start_destination_after_rebind})),
    )

    async def old_response(_: httpx.Request) -> httpx.Response:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            retiring.set()
            await release.wait()
            retired.set()
        return httpx.Response(201)

    async def new_response(request: httpx.Request) -> httpx.Response:
        sid = json.loads(request.content)["session_id"]
        requested.add(sid)
        if sid == row.id:
            initialized_after_retirement.append(retired.is_set())
            if destination_inflight:
                destination_entered.set()
                await destination_release.wait()
        return httpx.Response(201)

    async with (
        httpx.AsyncClient(
            transport=httpx.MockTransport(old_response), base_url="http://old"
        ) as old_client,
        httpx.AsyncClient(
            transport=httpx.MockTransport(new_response), base_url="http://new"
        ) as new_client,
    ):
        old = asyncio.create_task(initializer.initialize(row, old_client, timeout=10))
        await asyncio.wait_for(entered.wait(), 5)
        recovery = asyncio.create_task(
            restore_active_children(parent, new_client, store, initializer)
        )
        try:
            await asyncio.wait_for(asyncio.gather(retiring.wait(), sibling_restored.wait()), 5)
            if destination_inflight:
                assert destination is not None
                assert not destination.done()
            else:
                assert row.id not in requested
            assert not recovery.done()
            release.set()
            destination_release.set()
            if destination is not None:
                assert (await asyncio.wait_for(asyncio.shield(destination), 5)).status_code == 201
            await asyncio.wait_for(recovery, 5)
            with pytest.raises(ConnectionError):
                await old
            assert requested == {row.id, sibling.id}
            assert initialized_after_retirement == [not destination_inflight]
            assert {call.args[0] for call in relay.call_args_list} == requested
        finally:
            release.set()
            destination_release.set()
            recovery.cancel()
            await asyncio.gather(recovery, return_exceptions=True)
            await asyncio.gather(
                *initializer.invalidate_runner("old"),
                *initializer.invalidate_runner("new"),
                return_exceptions=True,
            )
            await asyncio.gather(old, return_exceptions=True)
            if destination is not None:
                await asyncio.gather(destination, return_exceptions=True)
