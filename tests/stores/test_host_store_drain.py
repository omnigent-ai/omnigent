"""The durable launch fence on the hosts row (``HostStore`` drain methods)."""

from __future__ import annotations

import uuid

import pytest

from omnigent.db.utils import clear_engine_cache
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.host_store import HostStore


@pytest.fixture()
def host_store(db_uri: str) -> HostStore:
    return HostStore(db_uri)


def _connected_host(host_store: HostStore, *, supported: bool = True) -> str:
    host_id = uuid.uuid4().hex
    host_store.upsert_on_connect(host_id, "laptop", "alice")
    host_store.note_drain_connect(host_id, supported=supported)
    return host_id


def test_start_drain_is_idempotent_and_bumps_generation_once(host_store: HostStore) -> None:
    host_id = _connected_host(host_store)

    first = host_store.start_drain(host_id, "alice")
    again = host_store.start_drain(host_id, "bob")

    assert first is not None and again is not None
    assert first.draining and first.drain_generation == 1
    assert again.drain_generation == 1, "re-draining must not invalidate an existing ack"
    assert again.drain_requested_by == "alice"


def test_admit_launch_rejects_draining_host_and_undrain_reopens(
    host_store: HostStore, db_uri: str
) -> None:
    """The admission transaction is the fence every launch path funnels through."""
    host_id = _connected_host(host_store)
    conversations = SqlAlchemyConversationStore(db_uri)
    conv = conversations.create_conversation()

    host_store.start_drain(host_id, "alice")
    with pytest.raises(OmnigentError) as error:
        host_store.admit_launch(host_id, conv.id, "alice", None, allow_unbound=True)
    assert error.value.code == ErrorCode.HOST_DRAINING
    assert error.value.http_status == 503

    undrained = host_store.stop_drain(host_id)
    assert undrained is not None and not undrained.draining
    assert undrained.drain_generation == 2
    host_store.admit_launch(host_id, conv.id, "alice", None, allow_unbound=True)


def test_drain_survives_server_restart(db_uri: str) -> None:
    """A new store over the same database (a restarted server) still fences."""
    store = HostStore(db_uri)
    host_id = _connected_host(store)
    store.start_drain(host_id, "alice")
    store.record_drain_ack(host_id, 1)
    clear_engine_cache()

    restarted = HostStore(db_uri)
    host = restarted.get_host(host_id)
    assert host is not None
    assert host.draining and host.drain_generation == 1
    assert host.drain_complete, "the recorded close-ack is durable too"
    conv = SqlAlchemyConversationStore(db_uri).create_conversation()
    with pytest.raises(OmnigentError) as error:
        restarted.admit_launch(host_id, conv.id, "alice", None, allow_unbound=True)
    assert error.value.code == ErrorCode.HOST_DRAINING


def test_ack_is_generation_bound(host_store: HostStore) -> None:
    host_id = _connected_host(host_store)
    host_store.start_drain(host_id, "alice")  # generation 1

    assert host_store.record_drain_ack(host_id, 0) is False
    assert host_store.record_drain_ack(host_id, 2) is False
    assert host_store.record_drain_ack(host_id, 1) is True
    host = host_store.get_host(host_id)
    assert host is not None and host.drain_complete

    host_store.stop_drain(host_id)  # generation 2
    host_store.start_drain(host_id, "alice")  # generation 3
    host = host_store.get_host(host_id)
    assert host is not None and not host.drain_complete
    assert host_store.record_drain_ack(host_id, 1) is False, "a stale ack must not carry over"


def test_ack_requires_an_active_drain(host_store: HostStore) -> None:
    host_id = _connected_host(host_store)
    assert host_store.record_drain_ack(host_id, 0) is False
    host = host_store.get_host(host_id)
    assert host is not None and not host.drain_complete


def test_unsupported_host_can_never_be_acked(host_store: HostStore) -> None:
    """An old host build cannot acknowledge, so it is never safe to restart."""
    host_id = _connected_host(host_store, supported=False)
    host_store.start_drain(host_id, "alice")

    assert host_store.record_drain_ack(host_id, 1) is False
    host = host_store.get_host(host_id)
    assert host is not None
    assert host.draining and host.drain_supported is False
    assert not host.drain_complete


def test_unknown_support_is_never_complete(host_store: HostStore) -> None:
    """A host that has not reconnected since the upgrade reads unknown, not safe."""
    host_id = uuid.uuid4().hex
    host_store.upsert_on_connect(host_id, "laptop", "alice")
    host_store.start_drain(host_id, "alice")

    assert host_store.record_drain_ack(host_id, 1) is False
    host = host_store.get_host(host_id)
    assert host is not None and host.drain_supported is None and not host.drain_complete


def test_reconnect_resets_the_ack_but_keeps_the_fence(host_store: HostStore) -> None:
    host_id = _connected_host(host_store)
    host_store.start_drain(host_id, "alice")
    assert host_store.record_drain_ack(host_id, 1)

    host_store.upsert_on_connect(host_id, "laptop", "alice")
    reconnected = host_store.note_drain_connect(host_id, supported=True)

    assert reconnected is not None
    assert reconnected.draining and reconnected.drain_generation == 1
    assert reconnected.drain_acked_generation is None
    assert not reconnected.drain_complete


def test_downgraded_reconnect_blocks_a_late_ack(host_store: HostStore) -> None:
    """A late ack from the old process cannot vouch for an older build that replaced it."""
    host_id = _connected_host(host_store)
    host_store.start_drain(host_id, "alice")
    host_store.note_drain_connect(host_id, supported=False)

    assert host_store.record_drain_ack(host_id, 1) is False


def test_identity_rotation_keeps_the_fence(host_store: HostStore) -> None:
    """``host reset-id`` on a draining machine must not silently lift its fence."""
    old_id = _connected_host(host_store)
    host_store.start_drain(old_id, "alice")

    new_id = uuid.uuid4().hex
    rotated = host_store.upsert_on_connect(new_id, "laptop", "alice")

    assert rotated.host_id == new_id
    assert rotated.draining and rotated.drain_generation == 1


def test_drain_unknown_host_returns_none(host_store: HostStore) -> None:
    missing = uuid.uuid4().hex
    assert host_store.start_drain(missing, "alice") is None
    assert host_store.stop_drain(missing) is None
    assert host_store.note_drain_connect(missing, supported=True) is None
    assert host_store.record_drain_ack(missing, 1) is False
