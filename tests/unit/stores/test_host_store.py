"""Tests for the host store (persistent host registration)."""

from __future__ import annotations

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlHost
from omnigent.db.utils import get_or_create_engine, now_epoch
from omnigent.stores.host_store import (
    HOST_LIVENESS_TTL_S,
    Host,
    HostStore,
    host_is_live,
)


@pytest.fixture()
def host_store(db_uri: str) -> HostStore:
    """
    Host store backed by the per-test SQLite database.

    :param db_uri: SQLite URI from the shared ``db_uri`` fixture.
    :returns: A :class:`HostStore` instance.
    """
    return HostStore(db_uri)


def _set_updated_at(db_uri: str, host_id: str, value: int) -> None:
    """Force a host row's ``updated_at`` to an exact epoch value.

    Lets a test stand a host's last-seen at a precise distance from
    ``now`` to probe the liveness freshness boundary, without sleeping.

    :param db_uri: SQLite URI shared with the store under test.
    :param host_id: Host whose timestamp to set.
    :param value: Unix epoch seconds to write into ``updated_at``.
    """
    engine = get_or_create_engine(db_uri)
    with Session(engine) as session:
        session.execute(update(SqlHost).where(SqlHost.host_id == host_id).values(updated_at=value))
        session.commit()


def test_upsert_creates_host_on_first_connect(
    host_store: HostStore,
) -> None:
    """
    Verify that upsert_on_connect inserts a new row when the host_id
    has never been seen before.

    If the host is missing from the DB after upsert, the INSERT path
    in upsert_on_connect is broken.
    """
    host = host_store.upsert_on_connect(
        host_id="host_aaa",
        name="test-laptop",
        owner="alice@example.com",
    )

    # Upsert returns the entity with all fields populated.
    assert host.host_id == "host_aaa"
    assert host.name == "test-laptop"
    assert host.owner == "alice@example.com"
    # New host is marked online immediately.
    assert host.status == "online"
    assert host.created_at > 0
    assert host.updated_at == host.created_at

    # Verify the row survives a fresh get.
    fetched = host_store.get_host("host_aaa")
    assert fetched is not None
    assert fetched.host_id == "host_aaa"
    assert fetched.name == "test-laptop"
    assert fetched.status == "online"


def test_upsert_updates_existing_host_on_reconnect(
    host_store: HostStore,
) -> None:
    """
    Verify that upsert_on_connect updates host_id, status, and
    updated_at when the same (owner, name) reconnects with a new
    host_id (user regenerated config.yaml).

    If the host_id is stale after the second upsert, the UPDATE
    path is broken.
    """
    host_store.upsert_on_connect(
        host_id="host_bbb_old",
        name="laptop",
        owner="bob@example.com",
    )
    host_store.set_offline("host_bbb_old")
    updated = host_store.upsert_on_connect(
        host_id="host_bbb_new",
        name="laptop",
        owner="bob@example.com",
    )

    assert updated.host_id == "host_bbb_new"
    assert updated.status == "online"


def test_upsert_persists_configured_harnesses(host_store: HostStore) -> None:
    """
    Verify configured_harnesses is written on insert and read back
    with exact values through get_host.

    If the map doesn't survive the round trip, GET /v1/hosts serves
    no readiness data and the web picker never warns.
    """
    host_store.upsert_on_connect(
        host_id="host_ch1",
        name="laptop",
        owner="alice@example.com",
        configured_harnesses={"claude-sdk": True, "codex": False},
    )

    fetched = host_store.get_host("host_ch1")
    assert fetched is not None
    # Exact equality: the False bit is the actionable "warn" value.
    assert fetched.configured_harnesses == {"claude-sdk": True, "codex": False}


def test_upsert_reconnect_overwrites_and_nulls_configured_harnesses(
    host_store: HostStore,
) -> None:
    """
    Verify a reconnect overwrites the stored map, and a reconnect
    without the map (older host build) resets it to None.

    If stale values survived a None reconnect, a host downgraded to a
    pre-readiness build would keep advertising obsolete bits forever.
    """
    host_store.upsert_on_connect(
        host_id="host_ch2",
        name="laptop2",
        owner="alice@example.com",
        configured_harnesses={"codex": False},
    )
    # Reconnect with fresh values — the user ran `omnigent setup`.
    host_store.upsert_on_connect(
        host_id="host_ch2",
        name="laptop2",
        owner="alice@example.com",
        configured_harnesses={"codex": True},
    )
    fetched = host_store.get_host("host_ch2")
    assert fetched is not None
    assert fetched.configured_harnesses == {"codex": True}

    # Reconnect without the map: back to unknown, not the stale value.
    host_store.upsert_on_connect(
        host_id="host_ch2",
        name="laptop2",
        owner="alice@example.com",
    )
    fetched = host_store.get_host("host_ch2")
    assert fetched is not None
    assert fetched.configured_harnesses is None


def test_malformed_configured_harnesses_column_reads_as_none(
    host_store: HostStore,
    db_uri: str,
) -> None:
    """
    Verify a corrupt configured_harnesses column value degrades to
    None instead of crashing host reads.

    Host listing must never 500 because of one bad advisory column —
    a JSONDecodeError here would take down GET /v1/hosts entirely.
    """
    host_store.upsert_on_connect(
        host_id="host_ch3",
        name="laptop3",
        owner="alice@example.com",
        configured_harnesses={"codex": True},
    )
    engine = get_or_create_engine(db_uri)
    with Session(engine) as session:
        session.execute(
            update(SqlHost)
            .where(SqlHost.host_id == "host_ch3")
            .values(configured_harnesses="{not json")
        )
        session.commit()

    fetched = host_store.get_host("host_ch3")
    assert fetched is not None
    assert fetched.configured_harnesses is None


def test_reconnect_with_rotated_host_id_repoints_bound_conversations(
    host_store: HostStore,
    db_uri: str,
) -> None:
    """A host_id rotation must not orphan or break conversations bound to it.

    Reproduces the production crash: the same machine ((owner, name) is
    the hosts PK) reconnects with a regenerated host_id while a
    conversation still references the OLD host_id via
    ``fk_conversations_host_id_hosts``. Renaming the unique ``host_id``
    column in place raises a ForeignKeyViolation (the FK has no
    ON UPDATE CASCADE), which crashed the host tunnel handler and sent
    the host into an endless reconnect loop with nothing in the UI.

    SQLite enforces FKs here (``PRAGMA foreign_keys=ON`` in
    ``_create_engine``), so this exercises the same constraint that
    fires on the hosted Postgres deploy. The fix must (a) not raise and
    (b) repoint the conversation to the new host_id so its binding
    survives the rotation.
    """
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore,
    )

    conversations = SqlAlchemyConversationStore(db_uri)

    host_store.upsert_on_connect(
        host_id="host_rot_old",
        name="dev-laptop",
        owner="dana@example.com",
    )
    # Bind a conversation to the old host_id (workspace is required by
    # the ck_conversations_workspace_required_for_host check constraint).
    conv = conversations.create_conversation(
        host_id="host_rot_old",
        workspace="/Users/dana/proj",
    )

    # Same (owner, name), rotated host_id — this used to raise.
    updated = host_store.upsert_on_connect(
        host_id="host_rot_new",
        name="dev-laptop",
        owner="dana@example.com",
    )

    assert updated.host_id == "host_rot_new"
    assert updated.status == "online"
    # The binding followed the rotation — the conversation now points at
    # the new host_id, not the old (dangling) one or NULL.
    rebound = conversations.get_conversation(conv.id)
    assert rebound is not None
    assert rebound.host_id == "host_rot_new", (
        "conversation must be repointed to the rotated host_id so the "
        "session stays bound to the reconnected host"
    )


def test_reown_host_id_across_owner_change_preserves_conversation_binding(
    host_store: HostStore,
    db_uri: str,
) -> None:
    """With reown opted in, the same host_id may move to a new owner.

    This is the auth-mode-flip case on the single-user local server: the
    server respawns under a different auth posture, so the same physical
    host (same host_id) re-registers under a different owner (accounts
    user → reserved ``local``). The ``(owner, name)`` lookup misses, but
    with ``allow_host_id_reown=True`` the existing row is re-owned in
    place — keeping the host_id and its conversation binding — instead of
    colliding on the host_id UNIQUE constraint.
    """
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore,
    )

    conversations = SqlAlchemyConversationStore(db_uri)
    host_store.upsert_on_connect(
        host_id="host_flip",
        name="laptop",
        owner="admin@example.com",
        allow_host_id_reown=True,
    )
    conv = conversations.create_conversation(host_id="host_flip", workspace="/home/me/proj")

    # Auth flipped to header mode: same machine, same host_id, new owner.
    reowned = host_store.upsert_on_connect(
        host_id="host_flip",
        name="laptop",
        owner="local",
        allow_host_id_reown=True,
    )

    assert reowned.host_id == "host_flip"
    assert reowned.owner == "local"
    assert reowned.status == "online"
    # The conversation binding survives the owner change (host_id unchanged).
    rebound = conversations.get_conversation(conv.id)
    assert rebound is not None
    assert rebound.host_id == "host_flip"
    # Exactly one row for this host_id — re-owned, not duplicated.
    online = host_store.list_hosts(owner="local")
    assert [h.host_id for h in online] == ["host_flip"]
    assert host_store.list_hosts(owner="admin@example.com") == []


def test_reown_disabled_rejects_foreign_owner_claiming_host_id(
    host_store: HostStore,
) -> None:
    """Without reown opt-in, a different owner cannot claim a host_id.

    The deployed multi-user boundary (W2-class host hijack): Alice owns
    ``host_x``; Bob connecting with the same host_id under the default
    ``allow_host_id_reown=False`` must NOT be able to re-own it. The
    host_id UNIQUE constraint fires (the same IntegrityError the host
    tunnel relies on to fail the handshake closed), so Bob never takes
    over Alice's host and Alice's row is untouched.
    """
    from sqlalchemy.exc import IntegrityError

    host_store.upsert_on_connect(host_id="host_x", name="alice-box", owner="alice@example.com")

    with pytest.raises(IntegrityError):
        host_store.upsert_on_connect(
            host_id="host_x",
            name="bob-box",
            owner="bob@example.com",
        )

    # Alice still owns host_x; Bob got nothing.
    assert [h.owner for h in host_store.list_hosts(owner="alice@example.com")] == [
        "alice@example.com"
    ]
    assert host_store.list_hosts(owner="bob@example.com") == []


def test_set_offline(host_store: HostStore) -> None:
    """
    Verify that set_offline transitions a host from online to offline.

    If status is still "online" after set_offline, the UPDATE
    statement is not executing or not committing.
    """
    host_store.upsert_on_connect(
        host_id="host_ccc",
        name="laptop",
        owner="carol@example.com",
    )
    host_store.set_offline("host_ccc")

    fetched = host_store.get_host("host_ccc")
    assert fetched is not None
    assert fetched.status == "offline"


def test_set_offline_noop_for_unknown_host(
    host_store: HostStore,
) -> None:
    """
    Verify that set_offline is a no-op for a nonexistent host_id.

    The disconnect callback may fire after a failed registration;
    it must not raise.
    """
    host_store.set_offline("host_nonexistent")


def test_heartbeat_advances_updated_at_without_changing_status(
    host_store: HostStore,
    db_uri: str,
) -> None:
    """
    Verify heartbeat refreshes last-seen but leaves status alone.

    The ping loop calls this every interval to keep a live host fresh.
    If it doesn't advance ``updated_at``, a long-lived host would age
    past the TTL and wrongly drop offline; if it flipped ``status``,
    it would fight the connect/disconnect writers.
    """
    host_store.upsert_on_connect("host_hb", "laptop", "alice@example.com")
    # Stand last-seen well in the past, as if the last touch was long ago.
    _set_updated_at(db_uri, "host_hb", now_epoch() - 10_000)

    host_store.heartbeat("host_hb")

    fetched = host_store.get_host("host_hb")
    assert fetched is not None
    # Last-seen jumped back to ~now (within a generous window for clock
    # granularity), proving the heartbeat wrote a fresh timestamp.
    assert fetched.updated_at >= now_epoch() - 5
    # Status is untouched — heartbeat only refreshes liveness.
    assert fetched.status == "online"


def test_heartbeat_noop_for_unknown_host(host_store: HostStore) -> None:
    """
    Verify heartbeat is a no-op for a host that does not exist.

    A heartbeat can race a just-deregistered host; it must not raise.
    """
    host_store.heartbeat("host_nonexistent")


def test_is_online_true_for_fresh_online_host(host_store: HostStore) -> None:
    """
    Verify is_online is True for an online host seen just now.

    This is the live-host happy path that keeps a connected session in
    the Connected group.
    """
    host_store.upsert_on_connect("host_fresh", "laptop", "alice@example.com")
    assert host_store.is_online("host_fresh") is True


def test_is_online_false_for_stale_online_host(
    host_store: HostStore,
    db_uri: str,
) -> None:
    """
    Verify is_online is False for an online row past the TTL.

    This is the crux of the fix: a host that crashed without a graceful
    disconnect keeps ``status="online"`` forever. Once its last-seen is
    older than the freshness window it must read as offline, even though
    ``set_offline`` never ran.
    """
    host_store.upsert_on_connect("host_stale", "laptop", "alice@example.com")
    # One second past the window — unambiguously stale.
    _set_updated_at(db_uri, "host_stale", now_epoch() - HOST_LIVENESS_TTL_S - 1)

    assert host_store.is_online("host_stale") is False


def test_is_online_false_for_offline_and_unknown(host_store: HostStore) -> None:
    """
    Verify is_online is False for an explicitly-offline or absent host.

    A clean disconnect (set_offline) and a never-seen host_id both must
    read as not-live regardless of timestamps.
    """
    host_store.upsert_on_connect("host_off", "laptop", "alice@example.com")
    host_store.set_offline("host_off")
    assert host_store.is_online("host_off") is False
    assert host_store.is_online("host_never_seen") is False


def test_online_host_ids_returns_only_live_hosts(
    host_store: HostStore,
    db_uri: str,
) -> None:
    """
    ``online_host_ids`` returns exactly the fresh-online subset.

    This is the bulk variant powering the sidebar online-dot batch
    path. It must apply the same status-plus-freshness gate as
    :meth:`is_online` across many ids in one query: a fresh host is
    included, a stale-online host (crashed without disconnect) and an
    explicitly-offline host are excluded, and an unknown id is simply
    absent. A regression that dropped the freshness check would wrongly
    include the stale host; one that dropped the status check would
    include the offline host.
    """
    # Distinct (host_id, name) per row — the hosts PK is (owner, name),
    # so reusing one name would collide instead of inserting three rows.
    host_store.upsert_on_connect("host_live", "laptop-live", "alice@example.com")
    host_store.upsert_on_connect("host_stale2", "laptop-stale", "alice@example.com")
    host_store.upsert_on_connect("host_offline", "laptop-off", "alice@example.com")
    host_store.set_offline("host_offline")
    _set_updated_at(db_uri, "host_stale2", now_epoch() - HOST_LIVENESS_TTL_S - 1)

    result = host_store.online_host_ids(
        ["host_live", "host_stale2", "host_offline", "host_unknown"]
    )
    assert result == {"host_live"}


def test_online_host_ids_empty_input_skips_query(host_store: HostStore) -> None:
    """
    ``online_host_ids([])`` returns an empty set without a DB round-trip.

    The sidebar batch frequently has no offline-with-host sessions to
    check, so the empty case is the common one; it must short-circuit
    rather than issue a degenerate ``IN ()`` query.
    """
    assert host_store.online_host_ids([]) == set()


def test_host_is_live_boundary_is_inclusive() -> None:
    """
    Verify the freshness boundary at exactly the TTL counts as live.

    A host seen exactly ``HOST_LIVENESS_TTL_S`` ago is still live (the
    comparison is ``>=`` last-seen vs ``now - TTL``); one second older
    is not. Pinning the boundary guards against an off-by-one that would
    either flap healthy hosts or keep dead ones a beat too long. Uses a
    fixed ``now`` so the two checks share one clock.
    """
    now = now_epoch()
    at_ttl = Host(
        host_id="host_edge",
        name="laptop",
        owner="a@example.com",
        status="online",
        created_at=now,
        updated_at=now - HOST_LIVENESS_TTL_S,
    )
    just_past = Host(
        host_id="host_edge",
        name="laptop",
        owner="a@example.com",
        status="online",
        created_at=now,
        updated_at=now - HOST_LIVENESS_TTL_S - 1,
    )
    assert host_is_live(at_ttl, now=now) is True
    assert host_is_live(just_past, now=now) is False


def test_list_hosts_filters_by_owner(
    host_store: HostStore,
) -> None:
    """
    Verify that list_hosts returns only hosts for the specified owner.

    If alice sees bob's host, the WHERE clause on ``owner`` is missing
    or broken.
    """
    host_store.upsert_on_connect("host_d1", "alice-laptop", "alice@example.com")
    host_store.upsert_on_connect("host_d2", "bob-laptop", "bob@example.com")
    host_store.upsert_on_connect("host_d3", "alice-arca", "alice@example.com")

    alice_hosts = host_store.list_hosts("alice@example.com")
    bob_hosts = host_store.list_hosts("bob@example.com")

    # Alice owns 2 hosts, bob owns 1.
    assert len(alice_hosts) == 2
    assert len(bob_hosts) == 1

    alice_ids = {h.host_id for h in alice_hosts}
    assert alice_ids == {"host_d1", "host_d3"}
    assert bob_hosts[0].host_id == "host_d2"


def test_list_hosts_empty_for_unknown_owner(
    host_store: HostStore,
) -> None:
    """
    Verify that list_hosts returns an empty list for an owner with
    no hosts.

    If a non-empty list is returned, the owner filter is not applied.
    """
    result = host_store.list_hosts("nobody@example.com")
    assert result == []


def test_get_host_returns_none_for_unknown(
    host_store: HostStore,
) -> None:
    """
    Verify that get_host returns None for a nonexistent host_id.

    If it raises or returns a default, the None-return contract is
    violated.
    """
    assert host_store.get_host("host_nonexistent") is None


def test_upsert_replaces_host_id_on_owner_name_conflict(
    host_store: HostStore,
) -> None:
    """
    When a host reconnects with a new host_id (user regenerated
    config.yaml) but the same (owner, name), the store must update
    the existing row's host_id rather than creating a duplicate.

    If two rows exist after the second upsert, the unique constraint
    on (owner, name) is missing or the IntegrityError fallback is
    broken.
    """
    host_store.upsert_on_connect("host_old", "laptop", "eve@example.com")
    host_store.upsert_on_connect("host_new", "laptop", "eve@example.com")

    hosts = host_store.list_hosts("eve@example.com")
    # Exactly one row — the old host_id was replaced, not duplicated.
    assert len(hosts) == 1
    assert hosts[0].host_id == "host_new"
    assert hosts[0].name == "laptop"
    assert hosts[0].status == "online"

    # Old host_id is gone from the DB.
    assert host_store.get_host("host_old") is None


def test_upsert_conflict_preserves_created_at(
    host_store: HostStore,
) -> None:
    """
    When the (owner, name) conflict path replaces a host_id, the
    original created_at timestamp must be preserved.

    If created_at changes, the IntegrityError branch is overwriting
    it with the current time instead of leaving it untouched.
    """
    original = host_store.upsert_on_connect(
        "host_first",
        "arca",
        "frank@example.com",
    )
    original_created = original.created_at

    host_store.upsert_on_connect(
        "host_second",
        "arca",
        "frank@example.com",
    )

    fetched = host_store.list_hosts("frank@example.com")
    assert len(fetched) == 1
    assert fetched[0].host_id == "host_second"
    # created_at from the original registration is preserved.
    assert fetched[0].created_at == original_created
    # updated_at should be >= the original (reconnect bumps it).
    assert fetched[0].updated_at >= original_created


# ── Managed-host credential methods ────────────────────────


def test_register_managed_host_and_resolve_token_roundtrip(db_uri: str) -> None:
    """
    The raw launch token resolves back to the full pre-registered host
    — owner, provider, sandbox id intact, status ``"offline"`` until
    the host actually connects. This is the credential path the host
    tunnel authenticates managed hosts with; a content mismatch here
    means the wrong user owns the host.
    """
    store = HostStore(db_uri)
    store.register_managed_host(
        host_id="host_managed_1",
        name="managed-m1",
        owner="alice@example.com",
        token="raw-launch-token-1",
        provider="modal",
        sandbox_id="sb-m1",
        token_expires_at=now_epoch() + 3600,
    )

    resolved = store.resolve_launch_token("raw-launch-token-1")
    assert resolved is not None
    assert resolved.host_id == "host_managed_1"
    assert resolved.name == "managed-m1"
    assert resolved.owner == "alice@example.com"
    assert resolved.sandbox_provider == "modal"
    assert resolved.sandbox_id == "sb-m1"
    # Pre-registered, not yet connected.
    assert resolved.status == "offline"


def test_resolve_launch_token_rejects_unknown_and_expired(db_uri: str) -> None:
    """
    Unknown tokens and expired tokens must NOT authenticate — the
    expiry is what keeps a token leaked from a long-dead sandbox from
    registering a host as its owner forever.
    """
    store = HostStore(db_uri)
    store.register_managed_host(
        host_id="host_managed_2",
        name="managed-m2",
        owner="alice@example.com",
        token="raw-launch-token-2",
        provider="modal",
        sandbox_id="sb-m2",
        # Already expired.
        token_expires_at=now_epoch() - 1,
    )

    assert store.resolve_launch_token("no-such-token") is None
    assert store.resolve_launch_token("raw-launch-token-2") is None


def test_register_managed_host_relaunch_rotates_credential(db_uri: str) -> None:
    """
    Relaunch: registering the SAME host_id again (a fresh sandbox
    generation after the previous one died) overwrites the credential
    and sandbox columns in place — the old token stops resolving the
    instant the new one lands, the host identity (and created_at)
    survives, and session bindings to the host_id stay valid.
    """
    store = HostStore(db_uri)
    first = store.register_managed_host(
        host_id="host_managed_3",
        name="managed-m3",
        owner="alice@example.com",
        token="generation-1-token",
        provider="modal",
        sandbox_id="sb-gen1",
        token_expires_at=now_epoch() + 3600,
    )

    second = store.register_managed_host(
        host_id="host_managed_3",
        name="managed-m3",
        owner="alice@example.com",
        token="generation-2-token",
        provider="modal",
        sandbox_id="sb-gen2",
        token_expires_at=now_epoch() + 3600,
    )

    # Same durable identity, fresh backing sandbox.
    assert second.host_id == first.host_id
    assert second.created_at == first.created_at
    assert second.sandbox_id == "sb-gen2"
    # Generation-1 token is revoked by the overwrite; generation-2
    # resolves to the same host now backed by the new sandbox.
    assert store.resolve_launch_token("generation-1-token") is None
    resolved = store.resolve_launch_token("generation-2-token")
    assert resolved is not None
    assert resolved.host_id == "host_managed_3"
    assert resolved.sandbox_id == "sb-gen2"


def test_managed_columns_survive_connect(db_uri: str) -> None:
    """
    The tunnel's ``upsert_on_connect`` (which fires when the sandbox
    host registers) must flip the pre-registered row online WITHOUT
    clobbering the managed columns — they are what session-delete
    later uses to terminate the right sandbox.
    """
    store = HostStore(db_uri)
    store.register_managed_host(
        host_id="host_managed_4",
        name="managed-m4",
        owner="alice@example.com",
        token="raw-launch-token-4",
        provider="modal",
        sandbox_id="sb-m4",
        token_expires_at=now_epoch() + 3600,
    )

    connected = store.upsert_on_connect(
        host_id="host_managed_4",
        name="managed-m4",
        owner="alice@example.com",
    )

    assert connected.status == "online"
    assert connected.sandbox_provider == "modal"
    assert connected.sandbox_id == "sb-m4"
    # The credential still resolves after connect.
    assert store.resolve_launch_token("raw-launch-token-4") is not None


def test_delete_host_removes_row_and_revokes_token(db_uri: str) -> None:
    """
    ``delete_host`` removes the host from the picker AND revokes its
    launch token in one operation (the row IS the credential); a
    second delete is a safe no-op for racing cleanup paths.
    """
    store = HostStore(db_uri)
    store.register_managed_host(
        host_id="host_managed_5",
        name="managed-m5",
        owner="alice@example.com",
        token="raw-launch-token-5",
        provider="modal",
        sandbox_id="sb-m5",
        token_expires_at=now_epoch() + 3600,
    )

    store.delete_host("host_managed_5")
    assert store.get_host("host_managed_5") is None
    assert store.resolve_launch_token("raw-launch-token-5") is None
    assert store.list_hosts("alice@example.com") == []
    # Second delete is a no-op, not an error.
    store.delete_host("host_managed_5")


def test_revoke_launch_token_keeps_row_but_stops_resolution(db_uri: str) -> None:
    """
    ``revoke_launch_token`` is the relaunch-failure cleanup: the
    credential stops authenticating but the durable host row stays, so
    the session binding survives for a retry. Contrast ``delete_host``
    (full teardown). Unknown hosts are a safe no-op for racing cleanup.
    """
    store = HostStore(db_uri)
    store.register_managed_host(
        host_id="host_managed_revoke",
        name="managed-revoke",
        owner="alice@example.com",
        token="raw-launch-token-revoke",
        provider="modal",
        sandbox_id="sb-revoke",
        token_expires_at=now_epoch() + 3600,
    )
    # Sanity: the token resolves before the revoke — without this, a
    # broken register would make the post-revoke assertion vacuous.
    assert store.resolve_launch_token("raw-launch-token-revoke") is not None

    store.revoke_launch_token("host_managed_revoke")

    # The credential is dead but the row (and its managed binding)
    # survives — a deleted row here would null the session's host_id.
    assert store.resolve_launch_token("raw-launch-token-revoke") is None
    host = store.get_host("host_managed_revoke")
    assert host is not None
    assert host.sandbox_provider == "modal"
    # Unknown host: no-op, not an error.
    store.revoke_launch_token("host_never_existed")


def test_managed_host_raw_token_never_stored(db_uri: str) -> None:
    """
    Only the SHA-256 digest is persisted: a database leak must not
    leak usable host credentials.
    """
    from sqlalchemy import select

    from omnigent.stores.host_store import hash_host_launch_token

    store = HostStore(db_uri)
    store.register_managed_host(
        host_id="host_managed_6",
        name="managed-m6",
        owner="alice@example.com",
        token="raw-launch-token-6",
        provider="modal",
        sandbox_id="sb-m6",
        token_expires_at=now_epoch() + 3600,
    )

    engine = get_or_create_engine(db_uri)
    with Session(engine) as session:
        row = session.execute(
            select(SqlHost).where(SqlHost.host_id == "host_managed_6")
        ).scalar_one()
        assert row.token_hash == hash_host_launch_token("raw-launch-token-6")
        assert row.token_hash != "raw-launch-token-6"


def test_register_managed_host_refuses_cross_owner_recredential(db_uri: str) -> None:
    """
    Fail-closed boundary: re-registering an existing host_id under a
    DIFFERENT owner must raise and leave the original credential
    intact. host_id is server-generated today, so a mismatch can only
    mean a bug or a forged id — silently re-owning would hand Bob's
    launch token Alice's host identity (cross-user host hijack).
    """
    store = HostStore(db_uri)
    store.register_managed_host(
        host_id="host_managed_7",
        name="managed-m7",
        owner="alice@example.com",
        token="alice-token-7",
        provider="modal",
        sandbox_id="sb-m7",
        token_expires_at=now_epoch() + 3600,
    )

    with pytest.raises(ValueError, match="different owner"):
        store.register_managed_host(
            host_id="host_managed_7",
            name="managed-m7-bob",
            owner="bob@example.com",
            token="bob-token-7",
            provider="modal",
            sandbox_id="sb-m7-bob",
            token_expires_at=now_epoch() + 3600,
        )

    # Alice's credential and binding are untouched; Bob's token never
    # became valid.
    resolved = store.resolve_launch_token("alice-token-7")
    assert resolved is not None
    assert resolved.owner == "alice@example.com"
    assert resolved.sandbox_id == "sb-m7"
    assert store.resolve_launch_token("bob-token-7") is None
