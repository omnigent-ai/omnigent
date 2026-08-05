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
        host_id="bdda8ba7e34130318b54dd872eb160af",
        name="test-laptop",
        user_id="alice@example.com",
    )

    # Upsert returns the entity with all fields populated.
    assert host.host_id == "bdda8ba7e34130318b54dd872eb160af"
    assert host.name == "test-laptop"
    assert host.user_id == "alice@example.com"
    # New host is marked online immediately.
    assert host.status == "online"
    assert host.created_at > 0
    assert host.updated_at == host.created_at

    # Verify the row survives a fresh get.
    fetched = host_store.get_host("bdda8ba7e34130318b54dd872eb160af")
    assert fetched is not None
    assert fetched.host_id == "bdda8ba7e34130318b54dd872eb160af"
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
        host_id="74d106ce261c29485a5dfb880a2cb15f",
        name="laptop",
        user_id="bob@example.com",
    )
    host_store.set_offline("74d106ce261c29485a5dfb880a2cb15f")
    updated = host_store.upsert_on_connect(
        host_id="ebfb0eac338c147444dc6dbf3f0503fc",
        name="laptop",
        user_id="bob@example.com",
    )

    assert updated.host_id == "ebfb0eac338c147444dc6dbf3f0503fc"
    assert updated.status == "online"


def test_upsert_persists_configured_harnesses(host_store: HostStore) -> None:
    """
    Verify configured_harnesses is written on insert and read back
    with exact values through get_host.

    If the map doesn't survive the round trip, GET /v1/hosts serves
    no readiness data and the web picker never warns.
    """
    host_store.upsert_on_connect(
        host_id="e8d515c60f315ca35b4109564e238669",
        name="laptop",
        user_id="alice@example.com",
        configured_harnesses={"claude-sdk": True, "codex": "needs-auth"},
    )

    fetched = host_store.get_host("e8d515c60f315ca35b4109564e238669")
    assert fetched is not None
    # Exact equality: the False bit is the actionable "warn" value.
    assert fetched.configured_harnesses == {"claude-sdk": True, "codex": "needs-auth"}


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
        host_id="54e092213a38acc19cfd13ffb160a2b7",
        name="laptop2",
        user_id="alice@example.com",
        configured_harnesses={"codex": False},
    )
    # Reconnect with fresh values — the user ran `omnigent setup`.
    host_store.upsert_on_connect(
        host_id="54e092213a38acc19cfd13ffb160a2b7",
        name="laptop2",
        user_id="alice@example.com",
        configured_harnesses={"codex": True},
    )
    fetched = host_store.get_host("54e092213a38acc19cfd13ffb160a2b7")
    assert fetched is not None
    assert fetched.configured_harnesses == {"codex": True}

    # Reconnect without the map: back to unknown, not the stale value.
    host_store.upsert_on_connect(
        host_id="54e092213a38acc19cfd13ffb160a2b7",
        name="laptop2",
        user_id="alice@example.com",
    )
    fetched = host_store.get_host("54e092213a38acc19cfd13ffb160a2b7")
    assert fetched is not None
    assert fetched.configured_harnesses is None


def test_update_harness_readiness_replaces_live_map(host_store: HostStore) -> None:
    """A live tunnel refresh replaces readiness without reconnecting."""
    host_id = "6d86ee544f1d5b7068ac56f5927a5b5c"
    host_store.upsert_on_connect(
        host_id=host_id,
        name="laptop-live",
        user_id="alice@example.com",
        configured_harnesses={"pi": False},
    )

    host_store.update_harness_readiness(host_id, {"pi": True})

    fetched = host_store.get_host(host_id)
    assert fetched is not None
    assert fetched.configured_harnesses == {"pi": True}
    assert fetched.status == "online"


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
        host_id="2da3abf4db79c0504dbda7b88dbf521d",
        name="laptop3",
        user_id="alice@example.com",
        configured_harnesses={"codex": True},
    )
    engine = get_or_create_engine(db_uri)
    with Session(engine) as session:
        session.execute(
            update(SqlHost)
            .where(SqlHost.host_id == "2da3abf4db79c0504dbda7b88dbf521d")
            .values(configured_harnesses="{not json")
        )
        session.commit()

    fetched = host_store.get_host("2da3abf4db79c0504dbda7b88dbf521d")
    assert fetched is not None
    assert fetched.configured_harnesses is None


def test_host_store_drops_unknown_harness_availability(
    host_store: HostStore,
    db_uri: str,
) -> None:
    """Persisted readiness maps retain only states supported by the wire contract."""
    host_id = "7c1e25b5bfa49cb0cfb4fb1ed86333fb"
    host_store.upsert_on_connect(
        host_id=host_id,
        name="laptop-readiness",
        user_id="alice@example.com",
        configured_harnesses={"codex": "needs-auth"},
    )
    engine = get_or_create_engine(db_uri)
    with Session(engine) as session:
        session.execute(
            update(SqlHost)
            .where(SqlHost.host_id == host_id)
            .values(configured_harnesses='{"codex":"needs-auth","pi":"future-state"}')
        )
        session.commit()

    fetched = host_store.get_host(host_id)

    assert fetched is not None
    assert fetched.configured_harnesses == {"codex": "needs-auth"}


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
        host_id="a1ed1ab71de2311e20488a989e61701c",
        name="dev-laptop",
        user_id="dana@example.com",
    )
    # Bind a conversation to the old host_id (workspace is required by
    # the ck_conversations_workspace_required_for_host check constraint).
    conv = conversations.create_conversation(
        host_id="a1ed1ab71de2311e20488a989e61701c",
        workspace="/Users/dana/proj",
    )

    # Same (owner, name), rotated host_id — this used to raise.
    updated = host_store.upsert_on_connect(
        host_id="b1b5efd7dfc33b5a6241f1866ffb00e6",
        name="dev-laptop",
        user_id="dana@example.com",
    )

    assert updated.host_id == "b1b5efd7dfc33b5a6241f1866ffb00e6"
    assert updated.status == "online"
    # The binding followed the rotation — the conversation now points at
    # the new host_id, not the old (dangling) one or NULL.
    rebound = conversations.get_conversation(conv.id)
    assert rebound is not None
    assert rebound.host_id == "b1b5efd7dfc33b5a6241f1866ffb00e6", (
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
        host_id="a0c8ab2431b35377abb4232febeded94",
        name="laptop",
        user_id="admin@example.com",
        allow_host_id_reown=True,
    )
    conv = conversations.create_conversation(
        host_id="a0c8ab2431b35377abb4232febeded94", workspace="/home/me/proj"
    )

    # Auth flipped to header mode: same machine, same host_id, new owner.
    reowned = host_store.upsert_on_connect(
        host_id="a0c8ab2431b35377abb4232febeded94",
        name="laptop",
        user_id="local",
        allow_host_id_reown=True,
    )

    assert reowned.host_id == "a0c8ab2431b35377abb4232febeded94"
    assert reowned.user_id == "local"
    assert reowned.status == "online"
    # The conversation binding survives the owner change (host_id unchanged).
    rebound = conversations.get_conversation(conv.id)
    assert rebound is not None
    assert rebound.host_id == "a0c8ab2431b35377abb4232febeded94"
    # Exactly one row for this host_id — re-owned, not duplicated.
    online = host_store.list_hosts(user_id="local")
    assert [h.host_id for h in online] == ["a0c8ab2431b35377abb4232febeded94"]
    assert host_store.list_hosts(user_id="admin@example.com") == []


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

    host_store.upsert_on_connect(
        host_id="5d23e459b50e20479abf5d3fa8e2f936", name="alice-box", user_id="alice@example.com"
    )

    with pytest.raises(IntegrityError):
        host_store.upsert_on_connect(
            host_id="5d23e459b50e20479abf5d3fa8e2f936",
            name="bob-box",
            user_id="bob@example.com",
        )

    # Alice still owns host_x; Bob got nothing.
    assert [h.user_id for h in host_store.list_hosts(user_id="alice@example.com")] == [
        "alice@example.com"
    ]
    assert host_store.list_hosts(user_id="bob@example.com") == []


def test_set_offline(host_store: HostStore) -> None:
    """
    Verify that set_offline transitions a host from online to offline.

    If status is still "online" after set_offline, the UPDATE
    statement is not executing or not committing.
    """
    host_store.upsert_on_connect(
        host_id="7b463227e479b3a677307588a5d9e44f",
        name="laptop",
        user_id="carol@example.com",
    )
    host_store.set_offline("7b463227e479b3a677307588a5d9e44f")

    fetched = host_store.get_host("7b463227e479b3a677307588a5d9e44f")
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
    host_store.set_offline("aababcc3941edb738172734a9ab7bb8c")


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
    host_store.upsert_on_connect("12b05166b1e4ccd4dce299285b12442f", "laptop", "alice@example.com")
    # Stand last-seen well in the past, as if the last touch was long ago.
    _set_updated_at(db_uri, "12b05166b1e4ccd4dce299285b12442f", now_epoch() - 10_000)

    host_store.heartbeat("12b05166b1e4ccd4dce299285b12442f")

    fetched = host_store.get_host("12b05166b1e4ccd4dce299285b12442f")
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
    host_store.heartbeat("aababcc3941edb738172734a9ab7bb8c")


def test_is_online_true_for_fresh_online_host(host_store: HostStore) -> None:
    """
    Verify is_online is True for an online host seen just now.

    This is the live-host happy path that keeps a connected session in
    the Connected group.
    """
    host_store.upsert_on_connect("c077de8b7f1cdaa48fa9edde307f5105", "laptop", "alice@example.com")
    assert host_store.is_online("c077de8b7f1cdaa48fa9edde307f5105") is True


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
    host_store.upsert_on_connect("f5422856c5f2a0d1189fdd49de0c469c", "laptop", "alice@example.com")
    # One second past the window — unambiguously stale.
    _set_updated_at(
        db_uri, "f5422856c5f2a0d1189fdd49de0c469c", now_epoch() - HOST_LIVENESS_TTL_S - 1
    )

    assert host_store.is_online("f5422856c5f2a0d1189fdd49de0c469c") is False


def test_is_online_false_for_offline_and_unknown(host_store: HostStore) -> None:
    """
    Verify is_online is False for an explicitly-offline or absent host.

    A clean disconnect (set_offline) and a never-seen host_id both must
    read as not-live regardless of timestamps.
    """
    host_store.upsert_on_connect("ba0a675ffb72378982f8ef434478adc8", "laptop", "alice@example.com")
    host_store.set_offline("ba0a675ffb72378982f8ef434478adc8")
    assert host_store.is_online("ba0a675ffb72378982f8ef434478adc8") is False
    assert host_store.is_online("d75a658a60c6bbad9c213a6e5cfbabf3") is False


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
    # Distinct (host_id, name) per row — the hosts unique constraint is
    # (workspace_id, user_id, name), so reusing one name would collide.
    host_store.upsert_on_connect(
        "2fd786c75c03cfbbec099a6820c08b62", "laptop-live", "alice@example.com"
    )
    host_store.upsert_on_connect(
        "1b82e794a2b66c37d902e663a89e0d91", "laptop-stale", "alice@example.com"
    )
    host_store.upsert_on_connect(
        "3d9665477127e41f42de3f4109418173", "laptop-off", "alice@example.com"
    )
    host_store.set_offline("3d9665477127e41f42de3f4109418173")
    _set_updated_at(
        db_uri, "1b82e794a2b66c37d902e663a89e0d91", now_epoch() - HOST_LIVENESS_TTL_S - 1
    )

    result = host_store.online_host_ids(
        [
            "2fd786c75c03cfbbec099a6820c08b62",
            "1b82e794a2b66c37d902e663a89e0d91",
            "3d9665477127e41f42de3f4109418173",
            "5342cd866f93bf4bad2f138f44f3609b",
        ]
    )
    assert result == {"2fd786c75c03cfbbec099a6820c08b62"}


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
        host_id="85816a8fc5fccd5874bf61da46a4c0ef",
        name="laptop",
        user_id="a@example.com",
        status="online",
        created_at=now,
        updated_at=now - HOST_LIVENESS_TTL_S,
    )
    just_past = Host(
        host_id="85816a8fc5fccd5874bf61da46a4c0ef",
        name="laptop",
        user_id="a@example.com",
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
    host_store.upsert_on_connect(
        "dd631c2a902eeead6632bb7a15d2a36d", "alice-laptop", "alice@example.com"
    )
    host_store.upsert_on_connect(
        "af2aefbe4a416df7641bf8ca520f838f", "bob-laptop", "bob@example.com"
    )
    host_store.upsert_on_connect(
        "58d8ac102d1d1b3db3f5056a7c8e196b", "alice-arca", "alice@example.com"
    )

    alice_hosts = host_store.list_hosts("alice@example.com")
    bob_hosts = host_store.list_hosts("bob@example.com")

    # Alice owns 2 hosts, bob owns 1.
    assert len(alice_hosts) == 2
    assert len(bob_hosts) == 1

    alice_ids = {h.host_id for h in alice_hosts}
    assert alice_ids == {"dd631c2a902eeead6632bb7a15d2a36d", "58d8ac102d1d1b3db3f5056a7c8e196b"}
    assert bob_hosts[0].host_id == "af2aefbe4a416df7641bf8ca520f838f"


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
    assert host_store.get_host("aababcc3941edb738172734a9ab7bb8c") is None


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
    host_store.upsert_on_connect("660d14a93e762f508ff74112a7a81eea", "laptop", "eve@example.com")
    host_store.upsert_on_connect("9b4beb1282718551dd43288ed246fdae", "laptop", "eve@example.com")

    hosts = host_store.list_hosts("eve@example.com")
    # Exactly one row — the old host_id was replaced, not duplicated.
    assert len(hosts) == 1
    assert hosts[0].host_id == "9b4beb1282718551dd43288ed246fdae"
    assert hosts[0].name == "laptop"
    assert hosts[0].status == "online"

    # Old host_id is gone from the DB.
    assert host_store.get_host("660d14a93e762f508ff74112a7a81eea") is None


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
        "4a7dab2e281a9dc8db539e9f59919109",
        "arca",
        "frank@example.com",
    )
    original_created = original.created_at

    host_store.upsert_on_connect(
        "a9dac0e66482a6e9ab8a4aae5135f59b",
        "arca",
        "frank@example.com",
    )

    fetched = host_store.list_hosts("frank@example.com")
    assert len(fetched) == 1
    assert fetched[0].host_id == "a9dac0e66482a6e9ab8a4aae5135f59b"
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
        host_id="e932ccae9eeb8f2a86f7ebfc5089c28d",
        name="managed-m1",
        user_id="alice@example.com",
        token="raw-launch-token-1",
        provider="modal",
        sandbox_id="sb-m1",
        token_expires_at=now_epoch() + 3600,
    )

    resolved = store.resolve_launch_token("e932ccae9eeb8f2a86f7ebfc5089c28d", "raw-launch-token-1")
    assert resolved is not None
    assert resolved.host_id == "e932ccae9eeb8f2a86f7ebfc5089c28d"
    assert resolved.name == "managed-m1"
    assert resolved.user_id == "alice@example.com"
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
        host_id="e5e05ec590da46a0e27bb138d343ffe7",
        name="managed-m2",
        user_id="alice@example.com",
        token="raw-launch-token-2",
        provider="modal",
        sandbox_id="sb-m2",
        # Already expired.
        token_expires_at=now_epoch() - 1,
    )

    # Unknown host id: nothing to resolve.
    assert store.resolve_launch_token("00000000000000000000000000000000", "no-such-token") is None
    # Known host, but its token is already expired.
    assert (
        store.resolve_launch_token("e5e05ec590da46a0e27bb138d343ffe7", "raw-launch-token-2")
        is None
    )


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
        host_id="a687a760841c785578a03f4677f8db3c",
        name="managed-m3",
        user_id="alice@example.com",
        token="generation-1-token",
        provider="modal",
        sandbox_id="sb-gen1",
        token_expires_at=now_epoch() + 3600,
    )

    second = store.register_managed_host(
        host_id="a687a760841c785578a03f4677f8db3c",
        name="managed-m3",
        user_id="alice@example.com",
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
    assert (
        store.resolve_launch_token("a687a760841c785578a03f4677f8db3c", "generation-1-token")
        is None
    )
    resolved = store.resolve_launch_token("a687a760841c785578a03f4677f8db3c", "generation-2-token")
    assert resolved is not None
    assert resolved.host_id == "a687a760841c785578a03f4677f8db3c"
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
        host_id="d55a61010459cea88ed2af0fe916139b",
        name="managed-m4",
        user_id="alice@example.com",
        token="raw-launch-token-4",
        provider="modal",
        sandbox_id="sb-m4",
        token_expires_at=now_epoch() + 3600,
    )

    connected = store.upsert_on_connect(
        host_id="d55a61010459cea88ed2af0fe916139b",
        name="managed-m4",
        user_id="alice@example.com",
    )

    assert connected.status == "online"
    assert connected.sandbox_provider == "modal"
    assert connected.sandbox_id == "sb-m4"
    # The credential still resolves after connect.
    assert (
        store.resolve_launch_token("d55a61010459cea88ed2af0fe916139b", "raw-launch-token-4")
        is not None
    )


def test_delete_host_removes_row_and_revokes_token(db_uri: str) -> None:
    """
    ``delete_host`` removes the host from the picker AND revokes its
    launch token in one operation (the row IS the credential); a
    second delete is a safe no-op for racing cleanup paths.
    """
    store = HostStore(db_uri)
    store.register_managed_host(
        host_id="dcf4eb5fc0b04985ec45f79cfda95566",
        name="managed-m5",
        user_id="alice@example.com",
        token="raw-launch-token-5",
        provider="modal",
        sandbox_id="sb-m5",
        token_expires_at=now_epoch() + 3600,
    )

    store.delete_host("dcf4eb5fc0b04985ec45f79cfda95566")
    assert store.get_host("dcf4eb5fc0b04985ec45f79cfda95566") is None
    assert (
        store.resolve_launch_token("dcf4eb5fc0b04985ec45f79cfda95566", "raw-launch-token-5")
        is None
    )
    assert store.list_hosts("alice@example.com") == []
    # Second delete is a no-op, not an error.
    store.delete_host("dcf4eb5fc0b04985ec45f79cfda95566")


def test_revoke_launch_token_keeps_row_but_stops_resolution(db_uri: str) -> None:
    """
    ``revoke_launch_token`` is the relaunch-failure cleanup: the
    credential stops authenticating but the durable host row stays, so
    the session binding survives for a retry. Contrast ``delete_host``
    (full teardown). Unknown hosts are a safe no-op for racing cleanup.
    """
    store = HostStore(db_uri)
    store.register_managed_host(
        host_id="f59827fa9468170e62cf28104d2a5251",
        name="managed-revoke",
        user_id="alice@example.com",
        token="raw-launch-token-revoke",
        provider="modal",
        sandbox_id="sb-revoke",
        token_expires_at=now_epoch() + 3600,
    )
    # Sanity: the token resolves before the revoke — without this, a
    # broken register would make the post-revoke assertion vacuous.
    assert (
        store.resolve_launch_token("f59827fa9468170e62cf28104d2a5251", "raw-launch-token-revoke")
        is not None
    )

    store.revoke_launch_token("f59827fa9468170e62cf28104d2a5251")

    # The credential is dead but the row (and its managed binding)
    # survives — a deleted row here would null the session's host_id.
    assert (
        store.resolve_launch_token("f59827fa9468170e62cf28104d2a5251", "raw-launch-token-revoke")
        is None
    )
    host = store.get_host("f59827fa9468170e62cf28104d2a5251")
    assert host is not None
    assert host.sandbox_provider == "modal"
    # Unknown host: no-op, not an error.
    store.revoke_launch_token("b4cfac8495d21e1a300c27de5203e52d")


def test_managed_host_raw_token_never_stored(db_uri: str) -> None:
    """
    Only the SHA-256 digest is persisted: a database leak must not
    leak usable host credentials.
    """
    from sqlalchemy import select

    from omnigent.stores.host_store import hash_host_launch_token

    store = HostStore(db_uri)
    store.register_managed_host(
        host_id="64c92d9b75006275f995c5041380a170",
        name="managed-m6",
        user_id="alice@example.com",
        token="raw-launch-token-6",
        provider="modal",
        sandbox_id="sb-m6",
        token_expires_at=now_epoch() + 3600,
    )

    engine = get_or_create_engine(db_uri)
    with Session(engine) as session:
        row = session.execute(
            select(SqlHost).where(SqlHost.host_id == "64c92d9b75006275f995c5041380a170")
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
        host_id="58f80f7592c6a72ba121eb5aedde8a82",
        name="managed-m7",
        user_id="alice@example.com",
        token="alice-token-7",
        provider="modal",
        sandbox_id="sb-m7",
        token_expires_at=now_epoch() + 3600,
    )

    with pytest.raises(ValueError, match="different user"):
        store.register_managed_host(
            host_id="58f80f7592c6a72ba121eb5aedde8a82",
            name="managed-m7-bob",
            user_id="bob@example.com",
            token="bob-token-7",
            provider="modal",
            sandbox_id="sb-m7-bob",
            token_expires_at=now_epoch() + 3600,
        )

    # Alice's credential and binding are untouched; Bob's token never
    # became valid.
    resolved = store.resolve_launch_token("58f80f7592c6a72ba121eb5aedde8a82", "alice-token-7")
    assert resolved is not None
    assert resolved.user_id == "alice@example.com"
    assert resolved.sandbox_id == "sb-m7"
    # Bob's token never armed Alice's host: it does not match the stored digest.
    assert store.resolve_launch_token("58f80f7592c6a72ba121eb5aedde8a82", "bob-token-7") is None
