"""Tests for the Databricks connection store façade."""

from __future__ import annotations

from omnigent.server.databricks_app import DatabricksTokenSet
from omnigent.connections.databricks import DatabricksConnectionStore
class SecretBox:  # test double for the KMS SecretCipher: key- and context-bound
    def __init__(self, key: str) -> None:
        self._key = key

    def encrypt(self, plaintext: str, *, context) -> str:
        import base64, json
        return base64.b64encode(
            json.dumps({"k": self._key, "c": dict(context), "p": plaintext}).encode()
        ).decode("ascii")

    def decrypt(self, ciphertext: str, *, context):
        import base64, json
        try:
            d = json.loads(base64.b64decode(ciphertext.encode("ascii")))
        except ValueError:
            return None
        return d["p"] if d["k"] == self._key and d["c"] == dict(context) else None


def _store(db_uri: str) -> DatabricksConnectionStore:
    return DatabricksConnectionStore(db_uri, SecretBox("enc-secret"))


def _tokens(access: str = "at", refresh: str = "rt") -> DatabricksTokenSet:
    return DatabricksTokenSet(
        access_token=access,
        refresh_token=refresh,
        expires_at=1000,
        refresh_token_expires_at=2000,
        scopes="all-apis offline_access",
    )


def test_upsert_get_roundtrip(db_uri: str) -> None:
    store = _store(db_uri)
    store.upsert(
        "alice",
        workspace_host="https://dbc-abc.cloud.databricks.com",
        databricks_user="alice@corp.com",
        databricks_user_id="42",
        tokens=_tokens(),
    )
    # Metadata-only view hides tokens but keeps workspace/user.
    meta_only = store.get("alice")
    assert meta_only is not None
    assert meta_only.access_token is None and meta_only.refresh_token is None
    assert meta_only.workspace_host == "https://dbc-abc.cloud.databricks.com"
    assert meta_only.databricks_user == "alice@corp.com"
    # With-tokens view decrypts.
    full = store.get("alice", with_tokens=True)
    assert full is not None and full.access_token == "at" and full.refresh_token == "rt"
    assert full.token_expires_at == 1000


def test_update_tokens_preserves_workspace(db_uri: str) -> None:
    store = _store(db_uri)
    store.upsert(
        "bob",
        workspace_host="https://ws.example",
        databricks_user="bob@corp.com",
        databricks_user_id="7",
        tokens=_tokens("old", "oldr"),
    )
    assert store.update_tokens("bob", _tokens("new", "newr")) is True
    conn = store.get("bob", with_tokens=True)
    assert conn is not None and conn.access_token == "new"
    assert conn.workspace_host == "https://ws.example"  # preserved
    assert conn.databricks_user == "bob@corp.com"  # preserved
    # Missing row → False (removed between read and refresh).
    assert store.update_tokens("ghost", _tokens()) is False


def test_delete_and_isolation_from_github(db_uri: str) -> None:
    store = _store(db_uri)
    store.upsert(
        "carol",
        workspace_host="https://ws.example",
        databricks_user="carol",
        databricks_user_id="9",
        tokens=_tokens(),
    )
    assert len(store.list_all()) == 1
    assert store.delete("carol") is True
    assert store.get("carol") is None
