"""Tests for the per-user credential store (encrypted at rest)."""

import pytest
from cryptography.fernet import Fernet

from omnigent.stores.credential_store import (
    CredentialStore,
    credential_encryption_enabled,
)


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIGENT_CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    return CredentialStore(f"sqlite:///{tmp_path}/creds.db")


def test_roundtrip(store):
    store.upsert("alice@example.com", "github", token="gho_secret", login="alice", scopes="repo")
    cred = store.get("alice@example.com", "github")
    assert cred is not None
    assert cred.login == "alice"
    assert cred.scopes == "repo"
    # Encrypted at rest — raw token never stored verbatim.
    assert "gho_secret" not in cred.token_encrypted
    assert store.decrypt_token(cred) == "gho_secret"


def test_upsert_replaces_existing(store):
    store.upsert("alice@example.com", "github", token="old", login="alice", scopes="")
    store.upsert("alice@example.com", "github", token="new", login="alice2", scopes="repo")
    cred = store.get("alice@example.com", "github")
    assert cred.login == "alice2"
    assert store.decrypt_token(cred) == "new"


def test_get_missing_returns_none(store):
    assert store.get("nobody@example.com", "github") is None


def test_delete(store):
    store.upsert("alice@example.com", "github", token="t", login="alice", scopes="")
    assert store.delete("alice@example.com", "github") is True
    assert store.get("alice@example.com", "github") is None
    assert store.delete("alice@example.com", "github") is False


def test_encryption_disabled_without_key(monkeypatch):
    monkeypatch.delenv("OMNIGENT_CREDENTIAL_ENCRYPTION_KEY", raising=False)
    assert credential_encryption_enabled() is False


def test_encryption_disabled_with_garbage_key(monkeypatch):
    monkeypatch.setenv("OMNIGENT_CREDENTIAL_ENCRYPTION_KEY", "not-a-key")
    assert credential_encryption_enabled() is False


def test_decrypt_returns_none_after_key_rotation(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIGENT_CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    store = CredentialStore(f"sqlite:///{tmp_path}/creds.db")
    store.upsert("alice@example.com", "github", token="t", login="alice", scopes="")
    cred = store.get("alice@example.com", "github")
    monkeypatch.setenv("OMNIGENT_CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    assert store.decrypt_token(cred) is None
