"""Tests for the provider-agnostic CredentialStore."""

from __future__ import annotations

import base64
import json
from typing import Any

import sqlalchemy as sa

from omnigent.db.db_models import workspace_scope
from omnigent.db.utils import get_or_create_engine
from omnigent.stores.credential_store.sqlalchemy_store import CredentialStore


class _FakeCipher:
    """In-memory SecretCipher standing in for the KMS cipher.

    Binds ciphertext to ``(key, context)`` so a wrong key or a mismatched
    identity decrypts to ``None`` — the same observable contract as
    :class:`~omnigent.stores.credential_store.secret_cipher.KmsSecretCipher`, without AWS. The
    plaintext is base64-wrapped (not stored in the clear), so the at-rest test
    is still meaningful.
    """

    def __init__(self, key: str = "test-key") -> None:
        self._key = key

    def encrypt(self, plaintext: str, *, context: Any) -> str:
        payload = {"key": self._key, "ctx": dict(context), "pt": plaintext}
        return base64.b64encode(json.dumps(payload).encode()).decode("ascii")

    def decrypt(self, ciphertext: str, *, context: Any) -> str | None:
        try:
            payload = json.loads(base64.b64decode(ciphertext.encode("ascii")))
        except ValueError:
            return None
        if payload["key"] != self._key or payload["ctx"] != dict(context):
            return None
        return payload["pt"]


def _store(db_uri: str) -> CredentialStore:
    return CredentialStore(db_uri, _FakeCipher())


def test_upsert_get_roundtrip(db_uri: str) -> None:
    store = _store(db_uri)
    store.upsert(
        "alice@example.com",
        "github",
        secret={"access_token": "ghu_1", "refresh_token": "ghr_1"},
        metadata={"github_login": "alice"},
    )
    # Metadata-only view hides the secret.
    meta_only = store.get("alice@example.com", "github")
    assert meta_only is not None
    assert meta_only.secret is None
    assert meta_only.metadata["github_login"] == "alice"
    # With-secret view decrypts.
    full = store.get("alice@example.com", "github", with_secret=True)
    assert full is not None and full.secret == {"access_token": "ghu_1", "refresh_token": "ghr_1"}


def test_provider_isolation(db_uri: str) -> None:
    store = _store(db_uri)
    store.upsert("alice", "github", secret={"k": "gh"}, metadata={})
    store.upsert("alice", "datadog", secret={"k": "dd"}, metadata={})
    gh = store.get("alice", "github", with_secret=True)
    dd = store.get("alice", "datadog", with_secret=True)
    assert gh is not None and gh.secret == {"k": "gh"}
    assert dd is not None and dd.secret == {"k": "dd"}
    assert store.get("alice", "slack") is None
    assert {c.provider for c in store.list_for_user("alice")} == {"github", "datadog"}


def test_update_secret_and_delete(db_uri: str) -> None:
    store = _store(db_uri)
    store.upsert("bob", "github", secret={"access_token": "old"}, metadata={"scopes": "repo"})
    assert store.update_secret("bob", "github", secret={"access_token": "new"}) is True
    conn = store.get("bob", "github", with_secret=True)
    assert conn is not None and conn.secret == {"access_token": "new"}
    assert conn.metadata["scopes"] == "repo"  # metadata preserved when not patched
    assert store.delete("bob", "github") is True
    assert store.get("bob", "github") is None


def test_update_secret_missing_row_returns_false(db_uri: str) -> None:
    # A refresh racing a disconnect must report the drop, not silently discard
    # the freshly-minted (and now-spent) token.
    store = _store(db_uri)
    assert store.update_secret("ghost", "github", secret={"access_token": "x"}) is False


def test_secret_never_stored_in_plaintext(db_uri: str) -> None:
    # The actual at-rest guarantee: the token bytes must not appear in the
    # column, only the cipher's ciphertext.
    store = _store(db_uri)
    store.upsert("dave", "github", secret={"access_token": "ghu_plaintext_leak"}, metadata={})
    engine = get_or_create_engine(db_uri)
    with engine.connect() as conn:
        stored = conn.execute(sa.text("SELECT secret_enc FROM connections")).scalar_one()
    assert "ghu_plaintext_leak" not in stored
    # And it still round-trips back to the plaintext through the cipher.
    full = store.get("dave", "github", with_secret=True)
    assert full is not None and full.secret == {"access_token": "ghu_plaintext_leak"}


def test_wrong_key_decrypts_to_none(db_uri: str) -> None:
    _store(db_uri).upsert("carol", "github", secret={"access_token": "x"}, metadata={})
    other = CredentialStore(db_uri, _FakeCipher("different-key"))
    conn = other.get("carol", "github", with_secret=True)
    assert conn is not None and conn.secret is None  # soft-fail, not a crash


def test_list_all_filters_by_provider(db_uri: str) -> None:
    store = _store(db_uri)
    store.upsert("a", "github", secret={"k": "1"}, metadata={})
    store.upsert("b", "github", secret={"k": "2"}, metadata={})
    store.upsert("a", "datadog", secret={"k": "3"}, metadata={})
    assert len(store.list_all(provider="github")) == 2
    assert len(store.list_all()) == 3


def test_workspace_isolation(db_uri: str) -> None:
    # The core multi-tenant guarantee: the same (user, provider) in two
    # workspaces are distinct rows and never read across the boundary.
    store = _store(db_uri)
    with workspace_scope(1):
        store.upsert("alice", "github", secret={"access_token": "ws1"}, metadata={})
    with workspace_scope(2):
        store.upsert("alice", "github", secret={"access_token": "ws2"}, metadata={})
        w2 = store.get("alice", "github", with_secret=True)
        assert w2 is not None and w2.secret == {"access_token": "ws2"}
        assert [c.user_id for c in store.list_all(provider="github")] == [
            "alice"
        ]  # only ws2's row
    with workspace_scope(1):
        w1 = store.get("alice", "github", with_secret=True)
        assert w1 is not None and w1.secret == {"access_token": "ws1"}
        assert len(store.list_all(provider="github")) == 1


def test_non_empty_account_id(db_uri: str) -> None:
    # account_id is in the PK, so two accounts of one provider coexist per user.
    store = _store(db_uri)
    store.upsert("alice", "github", secret={"k": "org1"}, metadata={}, account_id="org1")
    store.upsert("alice", "github", secret={"k": "org2"}, metadata={}, account_id="org2")
    a1 = store.get("alice", "github", account_id="org1", with_secret=True)
    a2 = store.get("alice", "github", account_id="org2", with_secret=True)
    assert a1 is not None and a1.secret == {"k": "org1"}
    assert a2 is not None and a2.secret == {"k": "org2"}
    # The default-account view ("") is a distinct, absent row.
    assert store.get("alice", "github") is None
    assert len(store.list_for_user("alice")) == 2
