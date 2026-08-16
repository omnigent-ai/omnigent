"""Tests for the host launch authorization helpers.

Tests ``resolve_host_owner`` and ``resolve_host_launch`` directly
(pure function tests, no HTTP).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from fastapi import HTTPException

from omnigent.entities import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.routes._host_launch import (
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

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        return self.convs.get(conversation_id)


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

    def test_admin_bypasses_ownership(self) -> None:
        # Bob owns the host; alice is an admin -> ownership waived.
        host = _FakeHost(host_id="host_1", user_id="bob")
        store = _FakeHostStore(hosts={"host_1": host})
        result = resolve_host_owner(
            user_id="alice", host_id="host_1", host_store=store, is_admin=True
        )
        assert result.host_id == "host_1"

    def test_non_admin_still_403_on_others_host(self) -> None:
        # is_admin default False: a plain user never bypasses ownership.
        host = _FakeHost(host_id="host_1", user_id="bob")
        store = _FakeHostStore(hosts={"host_1": host})
        with pytest.raises(HTTPException) as exc_info:
            resolve_host_owner(user_id="alice", host_id="host_1", host_store=store)
        assert exc_info.value.status_code == 403


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

    def test_admin_can_launch_on_other_users_host(self, monkeypatch) -> None:
        import omnigent.server.routes._host_launch as hl

        # Isolation: this test targets HOST authorization, so the session
        # owner check is neutralized.
        monkeypatch.setattr(hl, "check_session_access", lambda *a, **k: True)

        class _FakePermStore:
            def is_admin(self, user_id: str) -> bool:
                return user_id == "alice"

        host = _FakeHost(host_id="host_1", user_id="bob")  # bob's host
        conn = object()
        conv = Conversation(
            id="s1", created_at=1, updated_at=1, root_conversation_id="s1", agent_id="ag_1"
        )
        store = _FakeHostStore(hosts={"host_1": host})
        registry = _FakeHostRegistry(conns={"host_1": conn})
        conv_store = _FakeConversationStore(convs={"s1": conv})
        result = resolve_host_launch(
            user_id="alice",  # admin
            host_id="host_1",
            session_id="s1",
            host_store=store,
            host_registry=registry,
            conversation_store=conv_store,
            permission_store=_FakePermStore(),
        )
        assert result.host.host_id == "host_1"
        assert result.conv.id == "s1"

    def test_non_admin_cannot_launch_on_other_users_host(self, monkeypatch) -> None:
        import omnigent.server.routes._host_launch as hl

        monkeypatch.setattr(hl, "check_session_access", lambda *a, **k: True)

        class _FakePermStore:
            def is_admin(self, user_id: str) -> bool:
                return False

        host = _FakeHost(host_id="host_1", user_id="bob")
        conn = object()
        conv = Conversation(
            id="s1", created_at=1, updated_at=1, root_conversation_id="s1", agent_id="ag_1"
        )
        store = _FakeHostStore(hosts={"host_1": host})
        registry = _FakeHostRegistry(conns={"host_1": conn})
        conv_store = _FakeConversationStore(convs={"s1": conv})
        with pytest.raises(HTTPException) as exc_info:
            resolve_host_launch(
                user_id="alice",
                host_id="host_1",
                session_id="s1",
                host_store=store,
                host_registry=registry,
                conversation_store=conv_store,
                permission_store=_FakePermStore(),
            )
        assert exc_info.value.status_code == 403
