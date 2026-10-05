"""All-or-nothing session import: write order, rollback on cancel, abandoned partials.

Every write after the create targets the id the store assigned, which a store
may choose itself instead of the requested deterministic id.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from typing import Any

import pytest

from omnigent.server.host_registry import HostRegistry
from omnigent.server.routes import imports as imports_module
from omnigent.stores.conversation_store import ConversationAlreadyExistsError
from tests.server.import_tunnel_harness import (
    FakeConversationStore,
    FakePermissionStore,
    TunnelPair,
    cli_import_body,
    client,
    host_record,
    imports_app,
    local_import_body,
    local_session,
    message_item,
    post_stream,
    serve_local_sessions,
    wait_until,
)

_EXT = "ext-1"
_CID = imports_module._import_conversation_id("claude", _EXT)


async def _post_cli(
    store: FakeConversationStore,
    *,
    permissions: FakePermissionStore | None = None,
    user_id: str | None = None,
) -> Any:
    app = imports_app(
        store,
        host_registry=HostRegistry(),
        host=host_record(),
        permission_store=permissions,
        user_id=user_id,
    )
    async with client(app) as http:
        return await http.post("/v1/imports", json=cli_import_body(_EXT))


def _leftover(
    store: FakeConversationStore,
    *,
    age_s: int,
    external: bool,
    items: int,
    conversation_id: str = _CID,
    external_id: str = _EXT,
) -> None:
    """Seed the conversation a dead import request left behind."""
    store.create_conversation(conversation_id=conversation_id, title="partial")
    store.conversations[conversation_id].created_at = int(time.time()) - age_s
    if items:
        store.items[conversation_id] = [message_item(f"old {i}") for i in range(items)]
    if external:
        store.set_external_session_id(conversation_id, external_id)


def _owned_leftover(
    owner: str | None, *, external: bool
) -> tuple[FakeConversationStore, FakePermissionStore]:
    """An abandoned partial owned by ``owner`` (or by nobody).

    No external id: the current write order died mid-write. External id and no
    items: an older server died after recording the dedupe key.
    """
    store = FakeConversationStore()
    _leftover(store, age_s=3600, external=external, items=0 if external else 5)
    permissions = FakePermissionStore()
    if owner is not None:
        permissions.grant(owner, _CID, imports_module.LEVEL_OWNER)
    return store, permissions


async def _stream_one(
    monkeypatch: pytest.MonkeyPatch,
    store: FakeConversationStore,
    session_id: str,
    **app_kwargs: Any,
) -> dict[str, Any]:
    """Stream-import one session and return the done event."""
    pair = TunnelPair()
    app = imports_app(store, host_registry=pair.registry, host=host_record(), **app_kwargs)
    serve_local_sessions(monkeypatch, {session_id: local_session(session_id)})
    async with pair:
        events = await post_stream(app)
    return events[-1]


async def test_external_id_is_written_after_items_and_labels() -> None:
    """The dedupe key lands last, so a half-written import is never findable."""
    store = FakeConversationStore()
    seen: list[str] = []

    def on_append(_conversation_id: str, _items: list[Any]) -> None:
        assert store.find_conversation_by_external_session_id(_EXT) is None
        seen.append("append")

    def on_set_external(conversation_id: str, _value: str) -> None:
        assert getattr(store.conversations[conversation_id], "labels", None)
        seen.append("external")

    store.on_append = on_append
    store.on_set_external = on_set_external
    response = await _post_cli(store)
    assert response.status_code == 201, response.text
    assert seen == ["append", "external"]


async def test_cancel_mid_persist_rolls_back_after_the_write() -> None:
    """A request cancelled mid-write is rolled back once the write finishes; a re-import works."""
    store = FakeConversationStore()
    entered = threading.Event()
    release = threading.Event()

    def slow_append(_conversation_id: str, _items: list[Any]) -> None:
        entered.set()
        release.wait(timeout=10)

    store.on_append = slow_append
    request = asyncio.create_task(_post_cli(store))
    await wait_until(entered.is_set)
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    # The append is still running in its thread; nothing is deleted under it.
    assert _CID in store.conversations
    release.set()
    await wait_until(lambda: _CID not in store.conversations)
    assert store.find_conversation_by_external_session_id(_EXT) is None
    assert store.items == {}

    store.on_append = None
    again = await _post_cli(store)
    assert again.status_code == 201, again.text
    assert store.find_conversation_by_external_session_id(_EXT) is not None


async def test_cancelled_stream_rolls_back_only_the_session_in_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling a stream keeps finished sessions and rolls back the one being written."""
    store = FakeConversationStore()
    entered = threading.Event()
    release = threading.Event()
    s1 = imports_module._import_conversation_id("claude", "s1")

    def on_append(conversation_id: str, _items: list[Any]) -> None:
        if conversation_id == s1:
            entered.set()
            release.wait(timeout=10)

    store.on_append = on_append
    pair = TunnelPair()
    app = imports_app(store, host_registry=pair.registry, host=host_record())
    # Serial, so s0 is still unread when s1 blocks.
    app.state.local_import_concurrency = lambda: 1
    # The host imports oldest first: s2, s1, s0.
    serve_local_sessions(monkeypatch, {f"s{i}": local_session(f"s{i}") for i in range(3)})
    async with pair:
        async with client(app) as http:
            request = asyncio.create_task(
                http.post("/v1/imports/local/stream", json=local_import_body())
            )
            await wait_until(entered.is_set)
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
        release.set()
        await wait_until(lambda: s1 not in store.conversations)
    assert store.find_conversation_by_external_session_id("s2") is not None
    assert store.find_conversation_by_external_session_id("s1") is None
    assert store.find_conversation_by_external_session_id("s0") is None


async def test_old_server_leftover_with_no_items_is_replaced() -> None:
    """An old findable conversation with no items is replaced, not reported as a duplicate."""
    store = FakeConversationStore()
    _leftover(store, age_s=3600, external=True, items=0)
    response = await _post_cli(store)
    assert response.status_code == 201, response.text
    assert len(store.items[_CID]) == 1


async def test_leftover_without_external_id_is_replaced() -> None:
    """An old import row that never got its external id is replaced."""
    store = FakeConversationStore()
    _leftover(store, age_s=3600, external=False, items=5)
    response = await _post_cli(store)
    assert response.status_code == 201, response.text
    assert len(store.items[_CID]) == 1
    assert store.find_conversation_by_external_session_id(_EXT) is not None


@pytest.mark.parametrize("external", [True, False])
async def test_recent_partial_may_still_be_writing_so_it_is_kept(external: bool) -> None:
    """A young partial may belong to a live import, so it stays a duplicate."""
    store = FakeConversationStore()
    _leftover(store, age_s=30, external=external, items=0)
    response = await _post_cli(store)
    assert response.status_code == 409, response.text
    assert _CID in store.conversations
    assert store.deleted == []


async def test_complete_import_is_still_a_duplicate() -> None:
    """An old import with its items and external id is a real duplicate."""
    store = FakeConversationStore()
    _leftover(store, age_s=3600, external=True, items=3)
    assert (await _post_cli(store)).status_code == 409
    assert store.deleted == []


async def test_native_session_with_the_same_external_id_is_never_replaced() -> None:
    """Only the deterministic import id can be abandoned; a native run never is."""
    store = FakeConversationStore()
    _leftover(store, age_s=3600, external=True, items=0, conversation_id="conv_native")
    assert (await _post_cli(store)).status_code == 409
    assert "conv_native" in store.conversations


async def test_local_import_replaces_an_abandoned_partial(monkeypatch: pytest.MonkeyPatch) -> None:
    """The host-mediated import also replaces an abandoned partial."""
    store = FakeConversationStore()
    cid = imports_module._import_conversation_id("claude", "s0")
    _leftover(store, age_s=3600, external=True, items=0, conversation_id=cid, external_id="s0")
    done = await _stream_one(monkeypatch, store, "s0")
    assert (done["imported"], done["already_imported"]) == (1, 0)
    assert len(store.items[cid]) == 1


async def test_another_users_partial_is_not_replaced() -> None:
    """A partial owned by someone else answers as an existing import and is kept."""
    store, permissions = _owned_leftover("bob", external=False)
    response = await _post_cli(store, permissions=permissions, user_id="alice")
    assert response.status_code == 409, response.text
    assert response.json()["error"]["import_code"] == "already_imported"
    assert store.deleted == []
    assert len(store.items[_CID]) == 5
    assert ("alice", _CID) not in permissions.grants


async def test_another_users_old_server_partial_is_not_replaced() -> None:
    """A findable partial owned by someone else gets the stranger's 404 and is kept."""
    store, permissions = _owned_leftover("bob", external=True)
    response = await _post_cli(store, permissions=permissions, user_id="alice")
    assert response.status_code == 404, response.text
    assert store.deleted == []
    assert store.find_conversation_by_external_session_id(_EXT) is store.conversations[_CID]


@pytest.mark.parametrize("external", [False, True])
async def test_own_partial_is_replaced(external: bool) -> None:
    """The importer's own abandoned partial is replaced and re-granted to them."""
    store, permissions = _owned_leftover("alice", external=external)
    response = await _post_cli(store, permissions=permissions, user_id="alice")
    assert response.status_code == 201, response.text
    assert store.deleted == [_CID]
    assert len(store.items[_CID]) == 1
    assert store.find_conversation_by_external_session_id(_EXT) is not None
    assert permissions.grants[("alice", _CID)] == imports_module.LEVEL_OWNER


async def test_partial_that_never_got_an_owner_is_replaced() -> None:
    """A partial that never got an owner grant belongs to nobody, so it is replaced."""
    store, permissions = _owned_leftover(None, external=False)
    response = await _post_cli(store, permissions=permissions, user_id="alice")
    assert response.status_code == 201, response.text
    assert len(store.items[_CID]) == 1
    assert permissions.grants == {("alice", _CID): imports_module.LEVEL_OWNER}


async def test_ownership_unknowable_without_grant_rows_keeps_the_partial() -> None:
    """A store with no grant rows to inspect keeps a partial the importer can't prove is theirs."""

    class _CreatorOwnedPermissions(FakePermissionStore):
        def has_any_grants(self, conversation_id: str) -> bool:
            raise NotImplementedError

    store, _ = _owned_leftover(None, external=False)
    response = await _post_cli(store, permissions=_CreatorOwnedPermissions(), user_id="alice")
    assert response.status_code == 409, response.text
    assert store.deleted == []


async def test_local_import_keeps_another_users_partial(monkeypatch: pytest.MonkeyPatch) -> None:
    """The host-mediated import counts another user's partial as already imported."""
    store = FakeConversationStore()
    cid = imports_module._import_conversation_id("claude", "s0")
    _leftover(store, age_s=3600, external=True, items=0, conversation_id=cid, external_id="s0")
    permissions = FakePermissionStore()
    permissions.grant("bob", cid, imports_module.LEVEL_OWNER)
    # The host belongs to "local", who is importing.
    done = await _stream_one(
        monkeypatch, store, "s0", permission_store=permissions, user_id="local"
    )
    assert (done["imported"], done["already_imported"], done["failed"]) == (0, 1, 0)
    assert store.deleted == []
    assert cid not in store.items


async def test_duplicate_409_names_the_existing_session() -> None:
    """A duplicate keeps the old code and message and adds import_code and session_id."""
    store = FakeConversationStore()
    _leftover(store, age_s=3600, external=True, items=3)
    response = await _post_cli(store)
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "conflict"
    assert error["message"] == f"This claude session already exists as {_CID}"
    assert (error["import_code"], error["session_id"], error["retryable"]) == (
        "already_imported",
        _CID,
        False,
    )


async def test_create_race_is_already_imported_not_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Losing the create race to a concurrent import of the same session is already-imported."""
    store = FakeConversationStore()
    cid = imports_module._import_conversation_id("claude", "s0")
    # Young and not yet findable: a concurrent import still writing.
    store.create_conversation(conversation_id=cid, title="in flight")
    done = await _stream_one(monkeypatch, store, "s0")
    assert (done["imported"], done["already_imported"], done["failed"]) == (0, 1, 0)
    assert cid in store.conversations
    assert store.deleted == []


class _ReplacedDuringJudgmentStore(FakeConversationStore):
    """A store where a concurrent import replaces the partial while it is being judged.

    The swap happens on the judgment's read (``list_items`` for a findable
    partial, else the first ``get_conversation``), before any delete.
    """

    def __init__(self, conversation_id: str, external_id: str, *, on: str) -> None:
        super().__init__()
        self._conversation_id = conversation_id
        self._external_id = external_id
        self._on = on
        self.swapped = False

    def _swap(self) -> None:
        if self.swapped:
            return
        self.swapped = True
        # The concurrent import deleted the partial and wrote a complete row.
        del self.conversations[self._conversation_id]
        self.create_conversation(conversation_id=self._conversation_id, title="fresh")
        self.conversations[self._conversation_id].created_at += 1
        self.items[self._conversation_id] = [message_item("fresh")]
        self.set_external_session_id(self._conversation_id, self._external_id)

    def list_items(self, conversation_id: str, limit: int = 100, **kwargs: Any) -> Any:
        page = super().list_items(conversation_id, limit, **kwargs)
        if self._on == "list_items":
            self._swap()
        return page

    def get_conversation(self, conversation_id: str) -> Any:
        conversation = super().get_conversation(conversation_id)
        if self._on == "get_conversation":
            self._swap()
        return conversation


def _assert_fresh_row_kept(store: _ReplacedDuringJudgmentStore, conversation_id: str) -> None:
    assert store.swapped
    assert store.deleted == []
    assert store.conversations[conversation_id].title == "fresh"
    assert store.items[conversation_id] == [message_item("fresh")]


async def test_cli_import_keeps_a_partial_replaced_while_judged() -> None:
    """``/v1/imports`` never deletes a row a concurrent import completed; it answers 409."""
    store = _ReplacedDuringJudgmentStore(_CID, _EXT, on="list_items")
    _leftover(store, age_s=3600, external=True, items=0)
    response = await _post_cli(store)
    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert (error["import_code"], error["session_id"]) == ("already_imported", _CID)
    _assert_fresh_row_kept(store, _CID)


async def test_create_collision_keeps_a_partial_replaced_while_judged() -> None:
    """A create that hits a partial replaced meanwhile reports already imported, keeping it."""
    store = _ReplacedDuringJudgmentStore(_CID, _EXT, on="get_conversation")
    # Not findable by external id, so only the create collides with it.
    _leftover(store, age_s=3600, external=False, items=5)
    response = await _post_cli(store)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["import_code"] == "already_imported"
    _assert_fresh_row_kept(store, _CID)


async def test_local_import_keeps_a_partial_replaced_while_judged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The batch counts a partial replaced during its judgment as already imported."""
    cid = imports_module._import_conversation_id("claude", "s0")
    store = _ReplacedDuringJudgmentStore(cid, "s0", on="list_items")
    _leftover(store, age_s=3600, external=True, items=0, conversation_id=cid, external_id="s0")
    done = await _stream_one(monkeypatch, store, "s0")
    assert (done["imported"], done["already_imported"], done["failed"]) == (0, 1, 0)
    _assert_fresh_row_kept(store, cid)


class _StoreAssignedIdStore(FakeConversationStore):
    """A store whose create ignores the requested id and assigns its own.

    Later writes reject any id the store never issued, so a write that targets
    the requested deterministic id fails loudly.
    """

    def __init__(self) -> None:
        super().__init__()
        self.requested: list[str | None] = []
        self.writes: list[tuple[str, str]] = []
        self._next_id = 4_100_000_000_000_001

    def create_conversation(self, **kwargs: Any) -> Any:
        self.requested.append(kwargs.get("conversation_id"))
        assigned = str(self._next_id)
        self._next_id += 1
        return super().create_conversation(**{**kwargs, "conversation_id": assigned})

    def _write(self, method: str, conversation_id: str) -> None:
        if conversation_id not in self.conversations:
            raise KeyError(f"{method}: no conversation {conversation_id}")
        self.writes.append((method, conversation_id))

    def append(self, conversation_id: str, items: list[Any]) -> list[Any]:
        self._write("append", conversation_id)
        return super().append(conversation_id, items)

    def set_labels(self, conversation_id: str, labels: dict[str, str]) -> None:
        self._write("set_labels", conversation_id)
        super().set_labels(conversation_id, labels)

    def set_external_session_id(self, conversation_id: str, external_session_id: str) -> None:
        self._write("set_external_session_id", conversation_id)
        super().set_external_session_id(conversation_id, external_session_id)


class _IntegerIdPermissions(FakePermissionStore):
    """A permission store that binds conversation ids as int64 keys."""

    def grant(self, user_id: str, conversation_id: str, level: int) -> None:
        node_id = int(conversation_id)  # a hex import id fails here
        if not 0 < node_id < 2**63:
            raise ValueError("Value out of range")
        super().grant(user_id, conversation_id, level)


def _assert_written_under(
    store: _StoreAssignedIdStore,
    permissions: FakePermissionStore,
    user_id: str,
    external_id: str,
    session_id: str,
) -> None:
    assert store.writes == [
        ("append", session_id),
        ("set_labels", session_id),
        ("set_external_session_id", session_id),
    ]
    assert permissions.grants == {(user_id, session_id): imports_module.LEVEL_OWNER}
    assert store.find_conversation_by_external_session_id(external_id).id == session_id
    assert len(store.items[session_id]) == 1


async def _stream_events(
    monkeypatch: pytest.MonkeyPatch,
    store: FakeConversationStore,
    permissions: FakePermissionStore,
    session_id: str,
) -> list[dict[str, Any]]:
    pair = TunnelPair()
    # The host belongs to "local", who is importing.
    app = imports_app(
        store,
        host_registry=pair.registry,
        host=host_record(),
        permission_store=permissions,
        user_id="local",
    )
    serve_local_sessions(monkeypatch, {session_id: local_session(session_id)})
    async with pair:
        return await post_stream(app)


async def test_cli_import_writes_under_the_store_assigned_id() -> None:
    """Every write after the create, and the returned id, use the id the store assigned."""
    store, permissions = _StoreAssignedIdStore(), _IntegerIdPermissions()
    response = await _post_cli(store, permissions=permissions, user_id="alice")
    assert response.status_code == 201, response.text
    assert store.requested == [_CID]
    (session_id,) = store.conversations
    assert session_id != _CID
    assert response.json()["session_id"] == session_id
    _assert_written_under(store, permissions, "alice", _EXT, session_id)


async def test_stream_import_writes_under_the_store_assigned_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, permissions = _StoreAssignedIdStore(), _IntegerIdPermissions()
    events = await _stream_events(monkeypatch, store, permissions, "s0")
    (session_id,) = store.conversations
    assert [e["session_id"] for e in events if e["event"] == "session"] == [session_id]
    done = events[-1]
    assert (done["imported"], done["already_imported"], done["failed"]) == (1, 0, 0)
    _assert_written_under(store, permissions, "local", "s0", session_id)


async def test_reimport_names_the_store_assigned_id() -> None:
    store, permissions = _StoreAssignedIdStore(), _IntegerIdPermissions()
    first = await _post_cli(store, permissions=permissions, user_id="alice")
    session_id = first.json()["session_id"]
    again = await _post_cli(store, permissions=permissions, user_id="alice")
    assert again.status_code == 409, again.text
    error = again.json()["error"]
    assert (error["import_code"], error["session_id"]) == ("already_imported", session_id)
    assert error["message"] == f"This claude session already exists as {session_id}"
    assert list(store.conversations) == [session_id]


async def test_stream_reimport_with_store_assigned_ids_is_already_imported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, permissions = _StoreAssignedIdStore(), _IntegerIdPermissions()
    await _stream_events(monkeypatch, store, permissions, "s0")
    events = await _stream_events(monkeypatch, store, permissions, "s0")
    done = events[-1]
    assert (done["imported"], done["already_imported"], done["failed"]) == (0, 1, 0)
    assert [e for e in events if e["event"] == "session"] == []
    assert len(store.requested) == 1


async def test_failure_after_create_rolls_back_the_store_assigned_id() -> None:
    store, permissions = _StoreAssignedIdStore(), _IntegerIdPermissions()

    def fail(_conversation_id: str, _value: str) -> None:
        raise RuntimeError("storage unavailable")

    store.on_set_external = fail
    response = await _post_cli(store, permissions=permissions, user_id="alice")
    assert response.status_code == 500, response.text
    assert response.json()["error"]["import_code"] == "internal"
    (granted,) = {cid for _user, cid in permissions.grants}
    assert store.deleted == [granted]
    assert store.conversations == {}

    store.on_set_external = None
    again = await _post_cli(store, permissions=permissions, user_id="alice")
    assert again.status_code == 201, again.text


async def test_cancelled_import_rolls_back_the_store_assigned_id() -> None:
    store = _StoreAssignedIdStore()
    entered = threading.Event()
    release = threading.Event()

    def slow_append(_conversation_id: str, _items: list[Any]) -> None:
        entered.set()
        release.wait(timeout=10)

    store.on_append = slow_append
    request = asyncio.create_task(_post_cli(store))
    try:
        await wait_until(entered.is_set)
        request.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await request
        (session_id,) = store.conversations
    finally:
        release.set()
    await wait_until(lambda: store.deleted == [session_id])
    assert store.conversations == {}


class _AliasingStore(FakeConversationStore):
    """Keeps the requested id unique but stores the row under an id of its own."""

    def __init__(self) -> None:
        super().__init__()
        self.aliases: dict[str, str] = {}

    def create_conversation(self, **kwargs: Any) -> Any:
        requested = kwargs["conversation_id"]
        if requested in self.aliases:
            raise ConversationAlreadyExistsError(requested)
        assigned = f"node-{len(self.aliases) + 1}"
        self.aliases[requested] = assigned
        return super().create_conversation(**{**kwargs, "conversation_id": assigned})

    def get_conversation(self, conversation_id: str) -> Any:
        return super().get_conversation(self.aliases.get(conversation_id, conversation_id))


async def test_conflict_on_the_requested_id_names_the_existing_row() -> None:
    """The 409 for a create conflict points at the row the store holds, not the requested id."""
    # An import still writing (young, no external id yet) holds the requested id.
    store = _AliasingStore()
    store.create_conversation(conversation_id=_CID, title="in flight")
    response = await _post_cli(store)
    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert (error["import_code"], error["session_id"]) == ("already_imported", "node-1")
