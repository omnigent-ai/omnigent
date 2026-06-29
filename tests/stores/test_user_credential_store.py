"""Tests for the per-user credential vault store (#5): CRUD, upsert, and the
all-important per-user isolation (one user can never read another's secret).
"""

from __future__ import annotations

from omnigent.db.utils import generate_user_credential_id
from omnigent.stores.user_credential_store.sqlalchemy_store import (
    SqlAlchemyUserCredentialStore,
)


def test_crud_upsert_and_per_user_isolation(db_uri: str) -> None:
    store = SqlAlchemyUserCredentialStore(db_uri)
    store.upsert(generate_user_credential_id(), "alice", "github", "enc-A")
    store.upsert(generate_user_credential_id(), "bob", "github", "enc-B")

    # get_encrypted is scoped by user — each sees only their own ciphertext.
    assert store.get_encrypted("alice", "github") == "enc-A"
    assert store.get_encrypted("bob", "github") == "enc-B"
    # A user can't read a name they didn't set.
    assert store.get_encrypted("alice", "aws") is None

    # list_for_user is per-user and metadata-only (the entity carries no secret).
    alice = store.list_for_user("alice")
    assert [c.name for c in alice] == ["github"]
    assert not hasattr(alice[0], "secret_encrypted")

    # Upsert overwrites in place (same row, new ciphertext).
    store.upsert(generate_user_credential_id(), "alice", "github", "enc-A2")
    assert store.get_encrypted("alice", "github") == "enc-A2"
    assert len(store.list_for_user("alice")) == 1

    # Delete is idempotent and scoped — bob's row is untouched.
    assert store.delete("alice", "github") is True
    assert store.delete("alice", "github") is False
    assert store.get_encrypted("bob", "github") == "enc-B"
