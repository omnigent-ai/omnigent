"""Client disk/transport recovery; server SQL behavior is covered by store tests."""

import asyncio
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from filelock import FileLock, Timeout

from omnigent.preference_client import PreferenceClient
from omnigent.preference_sync import PreferenceMutation, SharedPreferenceKey
from omnigent.stores.preference_store.sqlalchemy_store import SqlAlchemyPreferenceStore

MODEL = SharedPreferenceKey(kind="model", name="model")


@pytest.fixture
def peers(tmp_path, db_uri):
    store = SqlAlchemyPreferenceStore(db_uri, authority_id="client-test")
    clients = [
        PreferenceClient(
            tmp_path / name / "cache.db", identity=store.identity("alice"), installation_id=name
        )
        for name in ("laptop", "remote")
    ]
    return store, clients


def transport(store, *, user="alice", after_write=None, protocol_version=2):
    def handle(request):
        if request.method == "GET":
            result = store.read(
                user_id=user,
                limit=int(request.url.params.get("limit", 100)),
                cursor=request.url.params.get("cursor"),
                protocol_version=min(
                    protocol_version, int(request.url.params.get("protocol_version", 1))
                ),
            )
        else:
            operation = PreferenceMutation.model_validate_json(request.content)
            result = store.apply(operation, user_id=user)
            if after_write is not None:
                after_write(request, result)
        return httpx.Response(200, json=result.model_dump(mode="json"))

    return httpx.MockTransport(handle)


@pytest.mark.asyncio
async def test_offline_chain_survives_reopen_and_noops(peers):
    store, (local, remote) = peers
    with pytest.raises(ValueError, match="Synchronize once"):
        local.enqueue(MODEL, "a")
    async with httpx.AsyncClient(base_url="http://test", transport=transport(store)) as http:
        await local.sync(http)
        for value in ("a", "a", None, "b"):
            local.enqueue(MODEL, value)
        assert local.cached().entries == []
        reopened = PreferenceClient(
            local.path, identity=store.identity("alice"), installation_id="laptop"
        )
        result = await reopened.sync(http, max_operations=2)
        assert len(reopened.pending()) == 2
        assert result.entries[0].version == 1
        await reopened.sync(http)
        result = await remote.sync(http)
    assert result.entries[0].value == "b" and result.entries[0].version == 3
    assert result.revision == 4 and result.conflicts == []
    assert reopened.pending() == []


@pytest.mark.asyncio
async def test_lost_ack_and_newer_remote_edit_cannot_regress_cache(peers):
    store, (local, remote) = peers
    delivered = []

    def drop(request, _result):
        delivered.append(json.loads(request.content))
        raise httpx.ReadError("lost response", request=request)

    async with httpx.AsyncClient(base_url="http://test", transport=transport(store)) as http:
        await local.sync(http)
        local.enqueue(MODEL, "a")
        async with httpx.AsyncClient(
            base_url="http://test", transport=transport(store, after_write=drop)
        ) as lossy:
            with pytest.raises(httpx.ReadError):
                await local.sync(lossy)
        await remote.sync(http)
        remote.enqueue(MODEL, "b")
        await remote.sync(http)
        local.enqueue(MODEL, "c")
        assert local.pending()[0].model_dump(mode="json") == delivered[0]
        snapshot = await local.sync(http)
    assert snapshot.entries[0].value == "b"
    assert [conflict.value for conflict in snapshot.conflicts] == ["c"]
    assert local.pending() == []


@pytest.mark.asyncio
async def test_conflicting_offline_chain_requires_explicit_resolution(peers):
    store, (local, remote) = peers
    async with httpx.AsyncClient(base_url="http://test", transport=transport(store)) as http:
        await local.sync(http)
        await remote.sync(http)
        local.enqueue(MODEL, "a")
        remote.enqueue(MODEL, "b")
        remote.enqueue(MODEL, "c")
        await local.sync(http)
        snapshot = await remote.sync(http)
        assert snapshot.entries[0].value == "a"
        assert {item.value for item in snapshot.conflicts} == {"b", "c"}
        remote.enqueue(
            MODEL, "c", resolves=tuple(item.operation_id for item in snapshot.conflicts)
        )
        await remote.sync(http)
        snapshot = await local.sync(http)
    assert snapshot.entries[0].value == "c" and snapshot.conflicts == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["offline", "forbidden", "wrong-user", "bad-receipt", "cancelled"]
)
async def test_delivery_failure_preserves_outbox(peers, failure):
    store, (local, _) = peers
    async with httpx.AsyncClient(base_url="http://test", transport=transport(store)) as http:
        await local.sync(http)
    local.enqueue(MODEL, "a")
    queued = local.pending()
    posts = []

    def handle(request):
        if failure == "offline":
            raise httpx.ConnectError("offline", request=request)
        if request.method == "GET":
            snapshot = store.read(user_id="bob" if failure == "wrong-user" else "alice")
            return httpx.Response(200, json=snapshot.model_dump(mode="json"))
        posts.append(request)
        if failure == "forbidden":
            return httpx.Response(403)
        if failure == "cancelled":
            raise asyncio.CancelledError
        return httpx.Response(200, json={"operation_id": "invalid"})

    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(handle)
    ) as http:
        with pytest.raises((httpx.HTTPError, ValueError, asyncio.CancelledError)):
            await local.sync(http)
    assert local.pending() == queued
    assert len(posts) == (0 if failure in ("offline", "wrong-user") else 1)


@pytest.mark.asyncio
async def test_cache_identity_corruption_and_stale_snapshot_are_not_silent_resets(peers):
    store, (local, _) = peers
    with pytest.raises(ValueError, match="another identity"):
        PreferenceClient(local.path, identity=store.identity("bob"), installation_id="laptop")
    async with httpx.AsyncClient(base_url="http://test", transport=transport(store)) as http:
        old = await local.sync(http)
        local.enqueue(MODEL, "a")
        latest = await local.sync(http)
    with pytest.raises(ValueError, match="older than"):
        local._accept_snapshot(old)
    with pytest.raises(ValueError, match="lost an acknowledged"):
        local._accept_snapshot(latest.model_copy(update={"entries": []}))
    assert local.cached() == latest
    with local._transaction() as connection:
        connection.execute("UPDATE entries SET payload = ?", (b"corrupt",))
    with pytest.raises(ValueError):
        local.cached()


@pytest.mark.asyncio
async def test_concurrent_local_writers_and_sync_lock(peers):
    store, (local, _) = peers
    async with httpx.AsyncClient(base_url="http://test", transport=transport(store)) as http:
        await local.sync(http)
        with ThreadPoolExecutor(max_workers=4) as pool:
            ids = list(pool.map(lambda value: local.enqueue(MODEL, str(value)), range(12)))
        assert {item.operation_id for item in local.pending()} == set(ids)
        with FileLock(str(local.path) + ".sync.lock"):
            with pytest.raises(Timeout):
                await local.sync(http)
            with pytest.raises(Timeout):
                local.discard(local.pending()[0].operation_id)
        snapshot = await local.sync(http)
    assert snapshot.entries[0].version == 12 and snapshot.conflicts == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["operation_id", "key", "value"])
async def test_mismatched_receipt_does_not_acknowledge_an_edit(peers, change):
    from uuid import uuid4

    store, (local, _) = peers
    async with httpx.AsyncClient(base_url="http://test", transport=transport(store)) as http:
        await local.sync(http)
    local.enqueue(MODEL, "a")
    queued = local.pending()

    def handle(request):
        if request.method == "GET":
            result = store.read(user_id="alice")
        else:
            result = store.apply(
                PreferenceMutation.model_validate_json(request.content), user_id="alice"
            )
            if change == "operation_id":
                result = result.model_copy(update={"operation_id": uuid4()})
            else:
                entry = result.entry.model_copy(
                    update={
                        change: SharedPreferenceKey(kind="model", name="opencode_model")
                        if change == "key"
                        else "wrong"
                    }
                )
                result = result.model_copy(update={"entry": entry})
        return httpx.Response(200, json=result.model_dump(mode="json"))

    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(handle)
    ) as http:
        with pytest.raises(ValueError, match="receipt"):
            await local.sync(http)
    assert local.pending() == queued


@pytest.mark.asyncio
async def test_outbox_limit_and_invalid_edit_leave_existing_work(peers, monkeypatch):
    import omnigent.preference_client as module

    store, (local, _) = peers
    async with httpx.AsyncClient(base_url="http://test", transport=transport(store)) as http:
        with pytest.raises(ValueError, match="nonnegative"):
            await local.sync(http, max_operations=-1)
        for page_size in (0, 101, True):
            with pytest.raises(ValueError, match="page size"):
                await local.sync(http, page_size=page_size)
        await local.sync(http)
    with pytest.raises(ValueError):
        local.enqueue(MODEL, False)
    monkeypatch.setattr(module, "MAX_PENDING_OPERATIONS", 1)
    operation_id = local.enqueue(MODEL, "a")
    with pytest.raises(ValueError, match="outbox is full"):
        local.enqueue(MODEL, "b")
    assert [operation.operation_id for operation in local.pending()] == [operation_id]


@pytest.mark.asyncio
async def test_discard_uncertain_head_preserves_successor_conflicts(peers):
    store, (local, remote) = peers
    async with httpx.AsyncClient(base_url="http://test", transport=transport(store)) as http:
        await local.sync(http)
        first = local.enqueue(MODEL, "a")
        successor = local.enqueue(MODEL, "b")
        # The server committed, but the client has not received its receipt.
        store.apply(local.pending()[0], user_id="alice")
        before = local.pending()
        with pytest.raises(ValueError, match="first queued"):
            local.discard(successor)
        assert local.pending() == before
        assert local.discard(first) == before[0]
        assert local.pending() == [before[1]]
        snapshot = await local.sync(http)
        assert snapshot.entries[0].value == "a"
        assert [item.value for item in snapshot.conflicts] == ["b"]
        assert await remote.sync(http) == snapshot
        with pytest.raises(ValueError, match="first queued"):
            local.discard(first)


@pytest.mark.asyncio
async def test_paginated_refresh_is_atomic_and_retry_preserves_outbox(peers):
    store, (local, remote) = peers
    async with httpx.AsyncClient(base_url="http://test", transport=transport(store)) as http:
        await local.sync(http)
        await remote.sync(http)
        for index in range(5):
            remote.enqueue(
                SharedPreferenceKey(
                    kind="plugin",
                    name={"ecosystem": "claude", "source": "test", "name": str(index)},
                ),
                False,
            )
        expected = await remote.sync(http, page_size=2)
        assert await local.sync(http, page_size=2) == expected
    local.enqueue(MODEL, "offline")
    cached, pending = local.cached(), local.pending()
    requests = []

    def changed(request):
        requests.append(request)
        assert request.method == "GET"
        if request.url.params.get("cursor"):
            return httpx.Response(409)
        page = store.read(user_id="alice", limit=2)
        return httpx.Response(200, json=page.model_dump(mode="json"))

    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(changed)
    ) as http:
        with pytest.raises(httpx.HTTPStatusError):
            await local.sync(http, page_size=2)
    assert len(requests) == 2
    assert local.cached() == cached and local.pending() == pending
    async with httpx.AsyncClient(base_url="http://test", transport=transport(store)) as http:
        result = await local.sync(http, page_size=2)
    assert local.pending() == [] and any(entry.value == "offline" for entry in result.entries)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "identity",
        "revision",
        "protocol",
        "duplicate",
        "cursor",
        "empty",
        "oversize",
        "malformed",
        "disconnect",
    ],
)
async def test_bad_later_page_never_replaces_cache_or_sends_edits(peers, fault):
    store, (local, _) = peers
    async with httpx.AsyncClient(base_url="http://test", transport=transport(store)) as http:
        await local.sync(http)
        local.enqueue(MODEL, "a")
        await local.sync(http)
    local.enqueue(MODEL, "pending")
    cached, pending = local.cached(), local.pending()
    count = 0

    def handle(request):
        nonlocal count
        count += 1
        assert request.method == "GET"
        page = store.read(user_id="alice").model_dump(mode="json")
        if count == 1:
            page["next_cursor"] = "next"
            return httpx.Response(200, json=page)
        page["entries"] = [
            {**page["entries"][0], "key": {"kind": "model", "name": "opencode_model"}}
        ]
        if fault == "identity":
            page["identity"]["user_id"] = "bob"
        elif fault == "revision":
            page["revision"] += 1
        elif fault == "protocol":
            page["protocol_version"] = 2
        elif fault == "duplicate":
            page["entries"][0]["key"] = MODEL.model_dump(mode="json")
        elif fault == "cursor":
            page["next_cursor"] = "next"
        elif fault == "empty":
            page.update(entries=[], next_cursor="other")
        elif fault == "oversize":
            page["entries"] *= 2
        elif fault == "malformed":
            page.pop("next_cursor")
        else:
            raise httpx.ReadError("injected disconnect", request=request)
        return httpx.Response(200, json=page)

    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(handle)
    ) as http:
        with pytest.raises((ValueError, httpx.ReadError)):
            await local.sync(http, page_size=1)
    assert count == 2 and local.cached() == cached and local.pending() == pending


@pytest.mark.asyncio
async def test_enqueue_during_network_wait_and_cancelled_disk_worker(peers, monkeypatch):
    store, (local, _) = peers
    async with httpx.AsyncClient(base_url="http://test", transport=transport(store)) as http:
        await local.sync(http)
    local.enqueue(MODEL, "first")
    posted, release_post = asyncio.Event(), asyncio.Event()
    base = transport(store)

    async def delayed(request):
        if request.method == "POST":
            posted.set()
            await release_post.wait()
        return base.handle_request(request)

    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(delayed)
    ) as http:
        task = asyncio.create_task(local.sync(http))
        try:
            await asyncio.wait_for(posted.wait(), 5)
            successor = await asyncio.wait_for(
                asyncio.to_thread(local.enqueue, MODEL, "second"), 2
            )
            assert local.pending()[-1].operation_id == successor
        finally:
            release_post.set()
            await task
    assert local.cached().entries[0].value == "second"
    started, release_worker = threading.Event(), threading.Event()
    accept = local._accept_snapshot

    def blocked(snapshot):
        started.set()
        assert release_worker.wait(5)
        accept(snapshot)

    monkeypatch.setattr(local, "_accept_snapshot", blocked)
    async with httpx.AsyncClient(base_url="http://test", transport=transport(store)) as http:
        task = asyncio.create_task(local.sync(http))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()  # repeated cancellation must not release the lock either
            await asyncio.sleep(0)
            with pytest.raises(Timeout):
                await local.sync(http)
        finally:
            release_worker.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert await local.sync(http) == local.cached()


@pytest.mark.asyncio
async def test_cache_schema_permissions_bounds_and_incomplete_refresh(peers):
    store, (local, _) = peers
    assert local.path.stat().st_mode & 0o777 == 0o600
    assert local.path.parent.stat().st_mode & 0o777 == 0o700
    async with httpx.AsyncClient(base_url="http://test", transport=transport(store)) as http:
        before = await local.sync(http)
    local.enqueue(MODEL, "a")
    sequence, operation = local._next()
    receipt = store.apply(operation, user_id="alice")
    local._acknowledge(sequence, operation, receipt)
    # An ack isn't a complete snapshot; offline startup retains the last complete read.
    assert local.cached() == before
    with local._transaction() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE entries SET payload = ?", (b"x" * 16001,))
        connection.execute("PRAGMA user_version = 999")
    with pytest.raises(ValueError, match="Unsupported preference cache version"):
        PreferenceClient(local.path, identity=store.identity("alice"), installation_id="laptop")
    with local._transaction() as connection:
        connection.execute("PRAGMA user_version = 0")
    with pytest.raises(ValueError, match="Unversioned preference cache"):
        PreferenceClient(local.path, identity=store.identity("alice"), installation_id="laptop")


@pytest.mark.asyncio
@pytest.mark.parametrize("attempted", [False, True])
async def test_compacted_retry_requires_review_only_after_first_send(
    peers, monkeypatch, attempted
):
    import omnigent.stores.preference_store.sqlalchemy_store as module
    from omnigent.errors import OmnigentError

    monkeypatch.setattr(module, "MAX_OPERATIONS", 2)
    store, (local, remote) = peers
    async with httpx.AsyncClient(base_url="http://test", transport=transport(store)) as http:
        await local.sync(http)
        local.enqueue(MODEL, "offline")
        before = local.pending()[0]
        if attempted:

            def drop(request, _result):
                raise httpx.ReadError("lost response", request=request)

            async with httpx.AsyncClient(
                base_url="http://test", transport=transport(store, after_write=drop)
            ) as lossy:
                with pytest.raises(httpx.ReadError):
                    await local.sync(lossy)
        await remote.sync(http)
        for value in ("remote-a", "remote-b", "remote-c"):
            remote.enqueue(MODEL, value)
            await remote.sync(http)
        reopened = PreferenceClient(
            local.path, identity=store.identity("alice"), installation_id="laptop"
        )
        if attempted:
            with pytest.raises(OmnigentError, match="older than retained retry history"):
                await reopened.sync(http)
            assert reopened.pending() == [before]
            assert reopened.cached().entries[0].value == "remote-c"
            assert not reopened.cached().conflicts
            reopened.discard(before.operation_id)
            reopened.enqueue(MODEL, "reviewed")
            result = await reopened.sync(http)
            assert result.entries[0].value == "reviewed"
        else:
            result = await reopened.sync(http)
            assert not reopened.pending()
            assert result.entries[0].value == "remote-c"
            conflict = result.conflicts[0]
            assert conflict.observed_revision == 3
            assert conflict.model_copy(update={"observed_revision": 0}) == before


@pytest.mark.asyncio
async def test_legacy_server_keeps_v1_writes(peers):
    store, (local, _) = peers
    async with httpx.AsyncClient(
        base_url="http://test", transport=transport(store, protocol_version=1)
    ) as http:
        snapshot = await local.sync(http)
        assert snapshot.protocol_version == 1
        local.enqueue(MODEL, "legacy")
        assert "observed_revision" not in local.pending()[0].model_dump(mode="json")
        result = await local.sync(http)
        assert result.entries[0].value == "legacy" and result.protocol_version == 1
