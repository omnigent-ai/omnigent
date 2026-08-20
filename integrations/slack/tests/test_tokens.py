from __future__ import annotations

from pathlib import Path

from cryptography.fernet import Fernet
from omnigent_slack.tokens import EncryptedTokenStore, TokenStore


async def _store(tmp_path: Path) -> TokenStore:
    store = EncryptedTokenStore(tmp_path / "t.sqlite3", Fernet.generate_key().decode())
    await store.initialize()
    return store


async def test_put_get_round_trip(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    await store.put("T1", "U1", "http://s", access_token="at", refresh_token="rt")
    rec = await store.get("T1", "U1", "http://s")
    assert rec is not None
    assert rec.access_token == "at"
    assert rec.refresh_token == "rt"


async def test_tokens_are_encrypted_at_rest(tmp_path: Path) -> None:
    """The raw SQLite bytes must not contain the plaintext token."""
    import aiosqlite

    path = tmp_path / "t.sqlite3"
    store = EncryptedTokenStore(path, Fernet.generate_key().decode())
    await store.initialize()
    await store.put("T1", "U1", "http://s", access_token="SECRET-AT", refresh_token="SECRET-RT")

    async with aiosqlite.connect(path) as db:
        cursor = await db.execute("SELECT access_token_enc, refresh_token_enc FROM oauth_tokens")
        row = await cursor.fetchone()
        await cursor.close()
    assert b"SECRET-AT" not in row[0]
    assert b"SECRET-RT" not in row[1]


async def test_scoped_by_server(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    await store.put("T1", "U1", "http://a", access_token="a", refresh_token="ra")
    await store.put("T1", "U1", "http://b", access_token="b", refresh_token="rb")
    assert (await store.get("T1", "U1", "http://a")).access_token == "a"
    assert (await store.get("T1", "U1", "http://b")).access_token == "b"


async def test_delete(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    await store.put("T1", "U1", "http://s", access_token="at", refresh_token="rt")
    await store.delete("T1", "U1", "http://s")
    assert await store.get("T1", "U1", "http://s") is None


async def test_list_for_user(tmp_path: Path) -> None:
    """list_for_user returns every (server, record) for a user, only theirs."""
    store = await _store(tmp_path)
    await store.put("T1", "U1", "http://a", access_token="a", refresh_token="ra")
    await store.put("T1", "U1", "http://b", access_token="b", refresh_token="rb")
    await store.put("T1", "U2", "http://a", access_token="c", refresh_token="rc")

    got = {server: rec.access_token for server, rec in await store.list_for_user("T1", "U1")}
    assert got == {"http://a": "a", "http://b": "b"}


async def test_wrong_key_yields_none(tmp_path: Path) -> None:
    """A rotated/incorrect key returns None rather than crashing."""
    path = tmp_path / "t.sqlite3"
    store = EncryptedTokenStore(path, Fernet.generate_key().decode())
    await store.initialize()
    await store.put("T1", "U1", "http://s", access_token="at", refresh_token="rt")

    other = EncryptedTokenStore(path, Fernet.generate_key().decode())
    assert await other.get("T1", "U1", "http://s") is None


async def test_in_memory_store_round_trip_and_scoping() -> None:
    """The no-key fallback stores tokens in memory with the same interface."""
    from omnigent_slack.tokens import InMemoryTokenStore

    store = InMemoryTokenStore()
    await store.initialize()
    await store.put("T1", "U1", "http://s/", access_token="at", refresh_token="rt")
    # Trailing slash is normalized like the encrypted store.
    rec = await store.get("T1", "U1", "http://s")
    assert rec is not None and rec.access_token == "at"
    # Scoped per server, and delete works.
    assert await store.get("T1", "U1", "http://other") is None
    await store.delete("T1", "U1", "http://s")
    assert await store.get("T1", "U1", "http://s") is None
