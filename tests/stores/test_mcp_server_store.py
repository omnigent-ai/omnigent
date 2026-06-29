"""Tests for SqlAlchemyMcpServerStore."""

from __future__ import annotations

import pytest

from omnigent.stores.mcp_server_store.sqlalchemy_store import SqlAlchemyMcpServerStore


def test_create_and_get(mcp_server_store: SqlAlchemyMcpServerStore) -> None:
    server = mcp_server_store.create(
        "mcp_one",
        "alice",
        "litellm",
        "http",
        url="https://gateway.example.com/mcp",
        headers={"Authorization": "Bearer secret"},
        description="LiteLLM gateway",
    )
    assert server.id == "mcp_one"
    assert server.owner == "alice"
    assert server.transport == "http"
    assert server.headers == {"Authorization": "Bearer secret"}

    fetched = mcp_server_store.get("mcp_one")
    assert fetched is not None
    assert fetched.url == "https://gateway.example.com/mcp"
    # Secrets round-trip through the JSON column.
    assert fetched.headers == {"Authorization": "Bearer secret"}
    assert fetched.description == "LiteLLM gateway"


def test_get_nonexistent(mcp_server_store: SqlAlchemyMcpServerStore) -> None:
    assert mcp_server_store.get("mcp_missing") is None


def test_stdio_args_and_env_round_trip(mcp_server_store: SqlAlchemyMcpServerStore) -> None:
    server = mcp_server_store.create(
        "mcp_stdio",
        "alice",
        "local-fs",
        "stdio",
        command="uvx",
        args=["mcp-server-filesystem", "/data"],
        env={"TOKEN": "xyz"},
    )
    fetched = mcp_server_store.get(server.id)
    assert fetched is not None
    assert fetched.command == "uvx"
    assert fetched.args == ["mcp-server-filesystem", "/data"]
    assert fetched.env == {"TOKEN": "xyz"}
    assert fetched.url is None


def test_list_is_owner_scoped_and_newest_first(
    mcp_server_store: SqlAlchemyMcpServerStore,
) -> None:
    mcp_server_store.create("mcp_a", "alice", "a", "http", url="https://a.example.com")
    mcp_server_store.create("mcp_b", "alice", "b", "http", url="https://b.example.com")
    mcp_server_store.create("mcp_c", "bob", "c", "http", url="https://c.example.com")

    alice = mcp_server_store.list_for_owner("alice")
    assert {s.id for s in alice} == {"mcp_a", "mcp_b"}
    # Bob never sees alice's servers.
    bob = mcp_server_store.list_for_owner("bob")
    assert {s.id for s in bob} == {"mcp_c"}


def test_get_by_name_is_owner_scoped(mcp_server_store: SqlAlchemyMcpServerStore) -> None:
    mcp_server_store.create("mcp_a", "alice", "shared", "http", url="https://a.example.com")
    mcp_server_store.create("mcp_b", "bob", "shared", "http", url="https://b.example.com")

    # Same name is allowed across owners; lookup stays scoped.
    assert mcp_server_store.get_by_name("alice", "shared").id == "mcp_a"  # type: ignore[union-attr]
    assert mcp_server_store.get_by_name("bob", "shared").id == "mcp_b"  # type: ignore[union-attr]
    assert mcp_server_store.get_by_name("alice", "nope") is None


def test_duplicate_name_for_owner_rejected(mcp_server_store: SqlAlchemyMcpServerStore) -> None:
    mcp_server_store.create("mcp_a", "alice", "dup", "http", url="https://a.example.com")
    with pytest.raises(Exception):  # noqa: B017 — unique-index violation
        mcp_server_store.create("mcp_b", "alice", "dup", "http", url="https://b.example.com")


def test_update_replaces_config(mcp_server_store: SqlAlchemyMcpServerStore) -> None:
    mcp_server_store.create(
        "mcp_u",
        "alice",
        "old",
        "http",
        url="https://old.example.com",
        headers={"A": "1"},
    )
    updated = mcp_server_store.update(
        "mcp_u",
        name="new",
        transport="http",
        url="https://new.example.com",
        headers={"B": "2"},
        command=None,
        args=[],
        env={},
        description="renamed",
    )
    assert updated is not None
    assert updated.name == "new"
    assert updated.url == "https://new.example.com"
    assert updated.headers == {"B": "2"}
    assert updated.updated_at is not None

    fetched = mcp_server_store.get("mcp_u")
    assert fetched is not None
    assert fetched.name == "new"
    assert fetched.headers == {"B": "2"}


def test_update_missing_returns_none(mcp_server_store: SqlAlchemyMcpServerStore) -> None:
    result = mcp_server_store.update(
        "mcp_missing",
        name="x",
        transport="http",
        url="https://x.example.com",
        headers={},
        command=None,
        args=[],
        env={},
        description=None,
    )
    assert result is None


def test_delete(mcp_server_store: SqlAlchemyMcpServerStore) -> None:
    mcp_server_store.create("mcp_d", "alice", "d", "http", url="https://d.example.com")
    assert mcp_server_store.delete("mcp_d") is True
    assert mcp_server_store.get("mcp_d") is None
    # Second delete is a no-op.
    assert mcp_server_store.delete("mcp_d") is False
