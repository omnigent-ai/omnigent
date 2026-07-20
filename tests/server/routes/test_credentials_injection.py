"""Owner-credential injection into managed sandbox launches."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from cryptography.fernet import Fernet

from omnigent.server.routes import sessions as sessions_routes
from omnigent.stores.credential_store import CredentialStore

_OWNER = "alice@example.com"


@pytest.fixture()
def credential_store(tmp_path, monkeypatch) -> CredentialStore:
    monkeypatch.setenv("OMNIGENT_CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    return CredentialStore(f"sqlite:///{tmp_path}/creds.db")


async def _provision(monkeypatch, credential_store: CredentialStore | None) -> dict:
    """Drive _provision_managed_sandbox with launch_managed_host captured."""
    captured: dict = {}

    async def fake_launch(**kwargs):
        captured.update(kwargs)
        return MagicMock()

    monkeypatch.setattr(
        "omnigent.server.managed_hosts.launch_managed_host",
        fake_launch,
    )
    await sessions_routes._provision_managed_sandbox(
        session_id="conv_test",
        owner=_OWNER,
        sandbox_config=MagicMock(),
        repo=None,
        tracker=MagicMock(),
        host_store=MagicMock(),
        relaunch_host=None,
        credential_store=credential_store,
    )
    return captured


async def test_connected_credential_injected(monkeypatch, credential_store) -> None:
    credential_store.upsert(_OWNER, "github", token="gho_x", login="alice", scopes="repo")
    captured = await _provision(monkeypatch, credential_store)
    assert captured["extra_env"] == {"GIT_TOKEN": "gho_x"}


async def test_no_credential_launches_without(monkeypatch, credential_store) -> None:
    captured = await _provision(monkeypatch, credential_store)
    assert captured["extra_env"] is None


async def test_no_store_launches_without(monkeypatch) -> None:
    captured = await _provision(monkeypatch, None)
    assert captured["extra_env"] is None


async def test_undecryptable_credential_launches_without(monkeypatch, credential_store) -> None:
    credential_store.upsert(_OWNER, "github", token="gho_x", login="alice", scopes="repo")
    monkeypatch.setenv("OMNIGENT_CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    captured = await _provision(monkeypatch, credential_store)
    assert captured["extra_env"] is None
