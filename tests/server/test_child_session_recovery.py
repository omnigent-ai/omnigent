"""Recovery restores active descendants without replaying finished work."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from omnigent.db.utils import generate_agent_id
from omnigent.entities import Conversation
from omnigent.server.child_session_recovery import restore_active_children
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
@pytest.mark.parametrize("exclusion", ["closed", "archived", "stopped", "hosted", "live_runner"])
async def test_do_not_restore_excluded_children(
    recovery_tree: Any, monkeypatch: pytest.MonkeyPatch, exclusion: str
) -> None:
    from omnigent.server.routes._sessions.common import _intentional_stop_sessions

    store, parent, child, relay, _, initializer = recovery_tree
    row = child()
    if exclusion == "closed":
        store.set_labels(row.id, {"omnigent.closed": "true"})
    elif exclusion == "archived":
        store.update_conversation(row.id, archived=True)
    elif exclusion == "stopped":
        _intentional_stop_sessions.add(row.id)
    elif exclusion == "hosted":
        store.set_host_id(row.id, "a" * 32, workspace="/tmp")
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
        _intentional_stop_sessions.discard(row.id)


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
        recovery, "asyncio", SimpleNamespace(to_thread=scheduled_store_call, Lock=asyncio.Lock)
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
    relay.side_effect = lambda *_args: restored.set()

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
@pytest.mark.parametrize("child_owner", ["owner", "other", None, "read", "edit", "manage"])
@pytest.mark.parametrize("same_binding", [False, True])
async def test_restoration_respects_runner_ownership(
    recovery_tree: Any,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    child_owner: str | None,
    same_binding: bool,
) -> None:
    """A child's direct owner must match the destination runner's owner."""
    from omnigent.server.auth import LEVEL_EDIT, LEVEL_MANAGE, LEVEL_OWNER, LEVEL_READ
    from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

    store, parent, child, relay, _, initializer = recovery_tree
    row = child()
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
        assert initialized == [row.id, nested.id]
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

    def fail_lookup(*_args: Any) -> None:
        recovered.assert_awaited_once_with(parent.id, store)
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
