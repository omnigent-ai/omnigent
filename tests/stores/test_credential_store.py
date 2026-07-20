"""Tests for the per-user credential store (encrypted at rest)."""

import pytest
from cryptography.fernet import Fernet

from omnigent.stores.credential_store import (
    CredentialStore,
    credential_encryption_enabled,
)

_USER = "alice@example.com"


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIGENT_CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    return CredentialStore(f"sqlite:///{tmp_path}/creds.db")


def test_roundtrip(store):
    store.upsert(_USER, "github", token="gho_secret", login="alice", scopes="repo")
    cred = store.get(_USER, "github")
    assert cred is not None
    assert (cred.login, cred.scopes) == ("alice", "repo")
    assert "gho_secret" not in cred.token_encrypted  # encrypted at rest
    assert store.decrypt_token(cred) == "gho_secret"


def test_upsert_replaces_existing(store):
    store.upsert(_USER, "github", token="old", login="alice", scopes="")
    store.upsert(_USER, "github", token="new", login="alice2", scopes="repo")
    cred = store.get(_USER, "github")
    assert cred.login == "alice2"
    assert store.decrypt_token(cred) == "new"


def test_get_missing_returns_none(store):
    assert store.get("nobody@example.com", "github") is None


def test_delete(store):
    store.upsert(_USER, "github", token="t", login="alice", scopes="")
    assert store.delete(_USER, "github") is True
    assert store.get(_USER, "github") is None
    assert store.delete(_USER, "github") is False


@pytest.mark.parametrize("key", [None, "not-a-key"])
def test_encryption_disabled_for_missing_or_invalid_key(monkeypatch, key):
    if key is None:
        monkeypatch.delenv("OMNIGENT_CREDENTIAL_ENCRYPTION_KEY", raising=False)
    else:
        monkeypatch.setenv("OMNIGENT_CREDENTIAL_ENCRYPTION_KEY", key)
    assert credential_encryption_enabled() is False


def test_decrypt_returns_none_after_key_rotation(store, monkeypatch):
    store.upsert(_USER, "github", token="t", login="alice", scopes="")
    cred = store.get(_USER, "github")
    monkeypatch.setenv("OMNIGENT_CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    assert store.decrypt_token(cred) is None
