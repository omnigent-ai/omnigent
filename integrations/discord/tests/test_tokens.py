from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from omnigent_discord.tokens import EncryptedTokenStore, InMemoryTokenStore, TokenStore

SERVER = "https://omnigent.example.com"


@pytest.fixture(params=["encrypted", "memory"])
async def store(request: pytest.FixtureRequest, tmp_path: Path) -> TokenStore:
    """Both backends satisfy the same protocol, so the contract is shared."""
    if request.param == "encrypted":
        backend: TokenStore = EncryptedTokenStore(
            tmp_path / "tokens.sqlite3", Fernet.generate_key().decode()
        )
    else:
        backend = InMemoryTokenStore()
    await backend.initialize()
    return backend


async def test_missing_token_is_none(store: TokenStore) -> None:
    assert await store.get("u1", SERVER) is None


async def test_token_round_trips(store: TokenStore) -> None:
    await store.put("u1", SERVER, access_token="acc", refresh_token="ref")
    record = await store.get("u1", SERVER)
    assert record is not None
    assert (record.access_token, record.refresh_token) == ("acc", "ref")


async def test_tokens_are_scoped_per_user(store: TokenStore) -> None:
    await store.put("u1", SERVER, access_token="acc", refresh_token="ref")
    assert await store.get("u2", SERVER) is None


async def test_tokens_are_scoped_per_server(store: TokenStore) -> None:
    await store.put("u1", SERVER, access_token="acc", refresh_token="ref")
    assert await store.get("u1", "https://other.example.com") is None


async def test_trailing_slash_is_the_same_server(store: TokenStore) -> None:
    await store.put("u1", SERVER + "/", access_token="acc", refresh_token="ref")
    assert await store.get("u1", SERVER) is not None


async def test_put_replaces_the_pair(store: TokenStore) -> None:
    await store.put("u1", SERVER, access_token="a1", refresh_token="r1")
    await store.put("u1", SERVER, access_token="a2", refresh_token="r2")
    record = await store.get("u1", SERVER)
    assert record is not None and record.access_token == "a2"


async def test_list_for_user_covers_every_server(store: TokenStore) -> None:
    await store.put("u1", SERVER, access_token="a", refresh_token="r")
    await store.put("u1", "https://other.example.com", access_token="a2", refresh_token="r2")
    await store.put("u2", SERVER, access_token="x", refresh_token="y")
    assert {server for server, _ in await store.list_for_user("u1")} == {
        SERVER,
        "https://other.example.com",
    }


async def test_delete_removes_only_that_pair(store: TokenStore) -> None:
    await store.put("u1", SERVER, access_token="a", refresh_token="r")
    await store.put("u2", SERVER, access_token="x", refresh_token="y")
    await store.delete("u1", SERVER)
    assert await store.get("u1", SERVER) is None
    assert await store.get("u2", SERVER) is not None


async def test_encrypted_store_writes_no_plaintext_token(tmp_path: Path) -> None:
    # A stolen database file must not be usable to impersonate anyone.
    path = tmp_path / "tokens.sqlite3"
    store = EncryptedTokenStore(path, Fernet.generate_key().decode())
    await store.initialize()
    await store.put("u1", SERVER, access_token="SECRET-ACCESS", refresh_token="SECRET-REFRESH")
    blob = path.read_bytes()
    assert b"SECRET-ACCESS" not in blob
    assert b"SECRET-REFRESH" not in blob


async def test_rotated_key_reads_as_no_token_rather_than_crashing(tmp_path: Path) -> None:
    path = tmp_path / "tokens.sqlite3"
    original = EncryptedTokenStore(path, Fernet.generate_key().decode())
    await original.initialize()
    await original.put("u1", SERVER, access_token="a", refresh_token="r")

    rotated = EncryptedTokenStore(path, Fernet.generate_key().decode())
    await rotated.initialize()
    # Undecryptable → the user is prompted to re-authenticate.
    assert await rotated.get("u1", SERVER) is None
    assert await rotated.list_for_user("u1") == []


async def test_in_memory_store_never_touches_disk(tmp_path: Path) -> None:
    store = InMemoryTokenStore()
    await store.initialize()
    await store.put("u1", SERVER, access_token="a", refresh_token="r")
    assert list(tmp_path.iterdir()) == []
