"""Tests for the host launch authorization helpers.

Tests ``resolve_host_owner`` and ``resolve_host_launch`` directly
(pure function tests, no HTTP).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from fastapi import HTTPException

from omnigent import debug_logging
from omnigent.entities import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import LEVEL_OWNER
from omnigent.server.routes._host_launch import (
    host_absent_error,
    resolve_host_launch,
    resolve_host_owner,
)
from omnigent.stores.host_store import now_epoch


@dataclass
class _FakeHost:
    host_id: str = "host_1"
    name: str = "test-host"
    user_id: str = "alice"
    status: str = "online"
    updated_at: int = field(default_factory=now_epoch)


@dataclass
class _FakeHostStore:
    hosts: dict[str, _FakeHost] = field(default_factory=dict)

    def get_host(self, host_id: str) -> _FakeHost | None:
        return self.hosts.get(host_id)


@dataclass
class _FakeHostRegistry:
    conns: dict[str, object] = field(default_factory=dict)

    def get(self, host_id: str) -> object | None:
        return self.conns.get(host_id)


@dataclass
class _FakeConversationStore:
    convs: dict[str, Conversation] = field(default_factory=dict)
    reads: list[str] = field(default_factory=list)

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        self.reads.append(conversation_id)
        return self.convs.get(conversation_id)


@dataclass
class _FakePermissionStore:
    grants: set[tuple[str, str]] = field(default_factory=set)

    def is_admin(self, user_id: str) -> bool:
        return False

    def check_access(
        self,
        user_id: str | None,
        conversation_id: str,
        required_level: int,
    ) -> bool:
        assert required_level == LEVEL_OWNER
        return user_id is not None and (user_id, conversation_id) in self.grants


# ── resolve_host_owner ───────────────────────────────────────────────


class TestResolveHostOwner:
    def test_unknown_host_404(self) -> None:
        store = _FakeHostStore()
        with pytest.raises(HTTPException) as exc_info:
            resolve_host_owner(user_id="alice", host_id="host_x", host_store=store)
        assert exc_info.value.status_code == 404

    def test_wrong_owner_403(self) -> None:
        host = _FakeHost(host_id="host_1", user_id="bob")
        store = _FakeHostStore(hosts={"host_1": host})
        with pytest.raises(HTTPException) as exc_info:
            resolve_host_owner(user_id="alice", host_id="host_1", host_store=store)
        assert exc_info.value.status_code == 403

    def test_correct_owner(self) -> None:
        host = _FakeHost(host_id="host_1", user_id="alice")
        store = _FakeHostStore(hosts={"host_1": host})
        result = resolve_host_owner(user_id="alice", host_id="host_1", host_store=store)
        assert result.host_id == "host_1"

    def test_no_auth_skips_owner_check(self) -> None:
        host = _FakeHost(host_id="host_1", user_id="bob")
        store = _FakeHostStore(hosts={"host_1": host})
        result = resolve_host_owner(user_id=None, host_id="host_1", host_store=store)
        assert result.host_id == "host_1"


# ── resolve_host_launch ──────────────────────────────────────────────


class TestResolveHostLaunch:
    def test_host_offline_409(self) -> None:
        host = _FakeHost(host_id="host_1", user_id="alice", status="offline")
        store = _FakeHostStore(hosts={"host_1": host})
        registry = _FakeHostRegistry()  # empty = no connections
        conv_store = _FakeConversationStore()
        with pytest.raises(OmnigentError) as exc_info:
            resolve_host_launch(
                user_id="alice",
                host_id="host_1",
                session_id="s1",
                host_store=store,
                host_registry=registry,
                conversation_store=conv_store,
                permission_store=None,
            )
        assert exc_info.value.code == ErrorCode.CONFLICT

    def test_missing_session_404(self) -> None:
        host = _FakeHost(host_id="host_1", user_id="alice")
        conn = object()
        store = _FakeHostStore(hosts={"host_1": host})
        registry = _FakeHostRegistry(conns={"host_1": conn})
        conv_store = _FakeConversationStore()  # empty
        with pytest.raises(HTTPException) as exc_info:
            resolve_host_launch(
                user_id="alice",
                host_id="host_1",
                session_id="s1",
                host_store=store,
                host_registry=registry,
                conversation_store=conv_store,
                permission_store=None,
            )
        assert exc_info.value.status_code == 404

    def test_success_no_auth(self) -> None:
        host = _FakeHost(host_id="host_1", user_id="alice")
        conn = object()
        conv = Conversation(
            id="s1",
            created_at=1,
            updated_at=1,
            root_conversation_id="s1",
            agent_id="ag_1",
        )
        store = _FakeHostStore(hosts={"host_1": host})
        registry = _FakeHostRegistry(conns={"host_1": conn})
        conv_store = _FakeConversationStore(convs={"s1": conv})
        result = resolve_host_launch(
            user_id=None,
            host_id="host_1",
            session_id="s1",
            host_store=store,
            host_registry=registry,
            conversation_store=conv_store,
            permission_store=None,
        )
        assert result.host.host_id == "host_1"
        assert result.conv.id == "s1"

    def test_preloaded_conversation_skips_reads_but_keeps_acl(self) -> None:
        host = _FakeHost(host_id="host_1", user_id="alice")
        conn = object()
        conv = Conversation(
            id="s1",
            created_at=1,
            updated_at=1,
            root_conversation_id="s1",
            agent_id="ag_1",
        )
        store = _FakeHostStore(hosts={"host_1": host})
        registry = _FakeHostRegistry(conns={"host_1": conn})
        conv_store = _FakeConversationStore()
        permissions = _FakePermissionStore(grants={("alice", "s1")})

        result = resolve_host_launch(
            user_id="alice",
            host_id="host_1",
            session_id="s1",
            host_store=store,
            host_registry=registry,
            conversation_store=conv_store,
            permission_store=permissions,  # type: ignore[arg-type]
            conversation=conv,
        )

        assert result.conv is conv
        assert conv_store.reads == []

        permissions.grants.clear()
        with pytest.raises(HTTPException) as exc_info:
            resolve_host_launch(
                user_id="alice",
                host_id="host_1",
                session_id="s1",
                host_store=store,
                host_registry=registry,
                conversation_store=conv_store,
                permission_store=permissions,  # type: ignore[arg-type]
                conversation=conv,
            )
        assert exc_info.value.status_code == 404
        assert conv_store.reads == []

    def test_standalone_resolution_does_not_record_create_timing(self) -> None:
        """The shared host-launch route must not pollute create-only stages."""
        host = _FakeHost(host_id="host_1", user_id="alice")
        conv = Conversation(
            id="s1",
            created_at=1,
            updated_at=1,
            root_conversation_id="s1",
            agent_id="ag_1",
        )
        permissions = _FakePermissionStore(grants={("alice", "s1")})
        debug_logging.reset_request_audit_attrs()

        resolve_host_launch(
            user_id="alice",
            host_id="host_1",
            session_id="s1",
            host_store=_FakeHostStore(hosts={"host_1": host}),
            host_registry=_FakeHostRegistry(conns={"host_1": object()}),
            conversation_store=_FakeConversationStore(convs={"s1": conv}),
            permission_store=permissions,  # type: ignore[arg-type]
        )

        assert "create_acl_ms" not in debug_logging.current_request_audit_attrs()

    def test_mismatched_preloaded_conversation_is_denied_without_reads(self) -> None:
        host = _FakeHost(host_id="host_1", user_id="alice")
        wrong_conv = Conversation(
            id="s2",
            created_at=1,
            updated_at=1,
            root_conversation_id="s2",
            agent_id="ag_1",
        )
        store = _FakeHostStore(hosts={"host_1": host})
        registry = _FakeHostRegistry(conns={"host_1": object()})
        conv_store = _FakeConversationStore()
        permissions = _FakePermissionStore(grants={("alice", "s1"), ("alice", "s2")})

        with pytest.raises(HTTPException) as exc_info:
            resolve_host_launch(
                user_id="alice",
                host_id="host_1",
                session_id="s1",
                host_store=store,
                host_registry=registry,
                conversation_store=conv_store,
                permission_store=permissions,  # type: ignore[arg-type]
                conversation=wrong_conv,
            )

        assert exc_info.value.status_code == 404
        assert conv_store.reads == []


# ── host_absent_error (single-replica vs sharded) ─────────────────────


class TestHostAbsentError:
    def test_sharded_live_host_is_wrong_replica(self) -> None:
        """On a sharded deployment a live-but-absent host re-addresses (400)."""
        host = _FakeHost(host_id="host_1", status="online", updated_at=now_epoch())
        err = host_absent_error(host, sharded=True)
        assert err.code == ErrorCode.WRONG_REPLICA

    def test_single_replica_live_host_is_offline(self) -> None:
        """On a single-replica deployment there is no other replica to re-address
        to, so a live-but-absent host is reported offline (409), not the
        un-satisfiable WRONG_REPLICA that would drive an endless client poll."""
        host = _FakeHost(host_id="host_1", status="online", updated_at=now_epoch())
        err = host_absent_error(host, sharded=False)
        assert err.code == ErrorCode.CONFLICT

    def test_offline_host_is_conflict_regardless_of_sharding(self) -> None:
        """A stale/offline row is a genuine 409 whether or not we're sharded."""
        host = _FakeHost(host_id="host_1", status="offline", updated_at=0)
        assert host_absent_error(host, sharded=True).code == ErrorCode.CONFLICT
        assert host_absent_error(host, sharded=False).code == ErrorCode.CONFLICT
