"""Host rows for static sandboxes: external rows armed with a launch token."""

from __future__ import annotations

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlHost
from omnigent.db.utils import get_or_create_engine, now_epoch
from omnigent.stores.host_store import HostStore

_OWNER = "alice@example.com"
_HOST = "0f6c1d3e2a4b4c5d8e9f0a1b2c3d4e5f"


@pytest.fixture()
def store(db_uri: str) -> HostStore:
    return HostStore(db_uri)


def _arm(store: HostStore, token: str = "static-token-1") -> None:
    store.upsert_static_host(
        host_id=_HOST,
        name="static-sandbox-foo",
        user_id=_OWNER,
        token=token,
        token_expires_at=now_epoch() + 3600,
    )


def test_static_host_row_is_external_with_a_token(store: HostStore) -> None:
    _arm(store)
    host = store.get_host(_HOST)
    assert host is not None
    assert host.sandbox_provider is None
    assert host.sandbox_id is None
    assert store.resolve_launch_token(_HOST, "static-token-1") is not None
    assert host.status == "offline"


def test_static_host_connects_with_its_launch_token(store: HostStore) -> None:
    _arm(store)
    connected = store.upsert_on_connect(
        host_id=_HOST, name="static-sandbox-foo", user_id=_OWNER, managed_token="static-token-1"
    )
    assert connected.status == "online"
    assert connected.sandbox_provider is None


def test_static_host_rejects_a_bad_token(store: HostStore) -> None:
    _arm(store)
    assert store.resolve_launch_token(_HOST, "wrong") is None
    with pytest.raises(ValueError, match="no longer valid"):
        store.upsert_on_connect(
            host_id=_HOST, name="static-sandbox-foo", user_id=_OWNER, managed_token="wrong"
        )


def test_rearming_revokes_the_previous_token(store: HostStore) -> None:
    _arm(store, "first")
    _arm(store, "second")
    assert store.resolve_launch_token(_HOST, "first") is None
    assert store.resolve_launch_token(_HOST, "second") is not None


def test_a_revoked_static_host_cannot_connect(store: HostStore) -> None:
    _arm(store)
    store.revoke_launch_token(_HOST)
    assert store.resolve_launch_token(_HOST, "static-token-1") is None
    with pytest.raises(ValueError):
        store.upsert_on_connect(
            host_id=_HOST,
            name="static-sandbox-foo",
            user_id=_OWNER,
            managed_token="static-token-1",
        )


def test_an_external_host_without_a_token_is_not_token_authenticated(store: HostStore) -> None:
    store.upsert_on_connect(host_id=_HOST, name="laptop", user_id=_OWNER)
    assert store.resolve_launch_token(_HOST, "anything") is None
    with pytest.raises(ValueError):
        store.upsert_on_connect(
            host_id=_HOST, name="laptop", user_id=_OWNER, managed_token="anything"
        )


def test_managed_row_without_a_current_sandbox_still_cannot_connect(
    store: HostStore, db_uri: str
) -> None:
    """The relaxed check admits static rows only, never a detached managed row."""
    store.register_managed_host(
        host_id=_HOST,
        name="managed-x",
        user_id=_OWNER,
        token="managed-token",
        provider="modal",
        sandbox_id="sb-1",
        token_expires_at=now_epoch() + 3600,
    )
    with Session(get_or_create_engine(db_uri)) as session:
        session.execute(update(SqlHost).where(SqlHost.host_id == _HOST).values(sandbox_id=None))
        session.commit()
    with pytest.raises(ValueError):
        store.upsert_on_connect(
            host_id=_HOST, name="managed-x", user_id=_OWNER, managed_token="managed-token"
        )


def test_upsert_static_host_refuses_another_users_or_a_managed_host(store: HostStore) -> None:
    _arm(store)
    with pytest.raises(ValueError, match="different user"):
        store.upsert_static_host(
            host_id=_HOST,
            name="static-sandbox-foo",
            user_id="bob@example.com",
            token="t",
            token_expires_at=now_epoch() + 60,
        )
    store.register_managed_host(
        host_id="1" * 32,
        name="managed-y",
        user_id=_OWNER,
        token="m",
        provider="modal",
        sandbox_id="sb-2",
        token_expires_at=now_epoch() + 60,
    )
    with pytest.raises(ValueError, match="managed"):
        store.upsert_static_host(
            host_id="1" * 32,
            name="static-sandbox-bar",
            user_id=_OWNER,
            token="t",
            token_expires_at=now_epoch() + 60,
        )


def test_reaper_scan_never_lists_a_static_host(store: HostStore) -> None:
    _arm(store)
    assert store.list_current_managed_sandbox_hosts_page(after=None, limit=100) == []
    assert store.list_terminating_managed_sandbox_hosts_page(after=None, limit=100) == []
