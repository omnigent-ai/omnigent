"""Tests for the GitHub connection store (encrypted at rest)."""

from __future__ import annotations

from omnigent.connections.github import GithubConnectionStore
from omnigent.server.github_app import GitHubTokenSet


class SecretBox:  # test double for the KMS SecretCipher: key- and context-bound
    def __init__(self, key: str) -> None:
        self._key = key

    def encrypt(self, plaintext: str, *, context) -> str:
        import base64
        import json

        return base64.b64encode(
            json.dumps({"k": self._key, "c": dict(context), "p": plaintext}).encode()
        ).decode("ascii")

    def decrypt(self, ciphertext: str, *, context):
        import base64
        import json

        try:
            d = json.loads(base64.b64decode(ciphertext.encode("ascii")))
        except ValueError:
            return None
        return d["p"] if d["k"] == self._key and d["c"] == dict(context) else None


def _store(db_uri: str) -> GithubConnectionStore:
    return GithubConnectionStore(db_uri, SecretBox("enc-key"))


def _tokens(access: str = "ghu_a", refresh: str | None = "ghr_a") -> GitHubTokenSet:
    return GitHubTokenSet(
        access_token=access,
        refresh_token=refresh,
        expires_at=1000,
        refresh_token_expires_at=2000,
        scopes="repo",
    )


def test_upsert_and_get_with_tokens(db_uri: str) -> None:
    store = _store(db_uri)
    store.upsert("alice@example.com", github_login="alice", github_user_id=7, tokens=_tokens())

    meta = store.get("alice@example.com")
    assert meta is not None
    assert meta.github_login == "alice"
    assert meta.github_user_id == 7
    # Default view hides tokens.
    assert meta.access_token is None
    assert meta.refresh_token is None

    full = store.get("alice@example.com", with_tokens=True)
    assert full is not None
    assert full.access_token == "ghu_a"
    assert full.refresh_token == "ghr_a"


def test_tokens_are_encrypted_at_rest(db_uri: str) -> None:
    """A store with a different key cannot read the tokens back."""
    _store(db_uri).upsert(
        "bob@example.com", github_login="bob", github_user_id=1, tokens=_tokens()
    )
    other = GithubConnectionStore(db_uri, SecretBox("different-key"))
    conn = other.get("bob@example.com", with_tokens=True)
    assert conn is not None
    # Wrong key → decrypt fails softly to None (needs reconnect), not a crash.
    assert conn.access_token is None


def test_upsert_overwrites_preserving_created_at(db_uri: str) -> None:
    store = _store(db_uri)
    store.upsert("alice", github_login="alice", github_user_id=7, tokens=_tokens("ghu_1"))
    first = store.get("alice", with_tokens=True)
    assert first is not None
    store.upsert("alice", github_login="alice2", github_user_id=9, tokens=_tokens("ghu_2"))
    second = store.get("alice", with_tokens=True)
    assert second is not None
    assert second.github_login == "alice2"
    assert second.github_user_id == 9
    assert second.access_token == "ghu_2"
    assert second.created_at == first.created_at


def test_update_tokens(db_uri: str) -> None:
    store = _store(db_uri)
    store.upsert("alice", github_login="alice", github_user_id=7, tokens=_tokens("ghu_old"))
    store.update_tokens("alice", _tokens("ghu_fresh", refresh="ghr_fresh"))
    conn = store.get("alice", with_tokens=True)
    assert conn is not None
    assert conn.access_token == "ghu_fresh"
    assert conn.refresh_token == "ghr_fresh"


def test_delete(db_uri: str) -> None:
    store = _store(db_uri)
    store.upsert("alice", github_login="alice", github_user_id=7, tokens=_tokens())
    assert store.delete("alice") is True
    assert store.get("alice") is None
    assert store.delete("alice") is False


def test_null_refresh_token(db_uri: str) -> None:
    store = _store(db_uri)
    store.upsert("alice", github_login="alice", github_user_id=7, tokens=_tokens(refresh=None))
    conn = store.get("alice", with_tokens=True)
    assert conn is not None
    assert conn.refresh_token is None
