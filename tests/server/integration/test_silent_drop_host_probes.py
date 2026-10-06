"""The host lookups ``create_app`` wires for the silent-drop hold, against a real host store."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi import FastAPI

from omnigent.host.frames import HostHelloFrame
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server import runner_drop_state
from omnigent.server.app import create_app
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_store import HostStore

pytestmark = pytest.mark.asyncio

_LAPTOP_ID = "c2d81b1a6812ae1cf32221c5a2a70ba0"
_SANDBOX_ID = "b8a8862c405a01143b4373e2b155b02a"


class _FakeHostSocket:
    async def send_text(self, data: str) -> None:
        del data

    async def receive_text(self) -> str:
        raise AssertionError("the registry does not read from the socket")


@pytest.fixture
def app(
    runtime_init: None, db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> FastAPI:
    """An app wired with a host store, so the probes read the ``hosts`` table."""
    del runtime_init
    monkeypatch.setattr(runner_drop_state, "_PROBE_TTL_S", 0.0)
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        host_store=HostStore(db_uri),
    )


async def test_host_liveness_follows_the_host_store_and_the_local_registry(app: FastAPI) -> None:
    host_store: HostStore = app.state.host_store
    assert await runner_drop_state.host_is_online(_LAPTOP_ID) is False, (
        "an unknown host is offline"
    )

    host_store.upsert_on_connect(_LAPTOP_ID, "laptop", RESERVED_USER_LOCAL)
    assert await runner_drop_state.host_is_online(_LAPTOP_ID) is True
    host_store.set_offline(_LAPTOP_ID)
    assert await runner_drop_state.host_is_online(_LAPTOP_ID) is False

    # A host connected to this replica is online before its row says so.
    app.state.host_registry.register(
        _LAPTOP_ID,
        _FakeHostSocket(),
        HostHelloFrame(version="0.1.0-test", frame_protocol_version=1, name="laptop"),
        owner=RESERVED_USER_LOCAL,
    )
    assert await runner_drop_state.host_is_online(_LAPTOP_ID) is True


async def test_a_managed_sandbox_host_is_recognized_from_its_host_row(app: FastAPI) -> None:
    host_store: HostStore = app.state.host_store
    host_store.upsert_on_connect(_LAPTOP_ID, "laptop", RESERVED_USER_LOCAL)
    host_store.register_managed_host(
        host_id=_SANDBOX_ID,
        name="managed-sandbox",
        user_id=RESERVED_USER_LOCAL,
        token="launch-token-secret",
        provider="modal",
        sandbox_id="sb-12345",
        token_expires_at=int(time.time()) + 3600,
    )

    assert await runner_drop_state.host_is_managed(_SANDBOX_ID) is True
    assert await runner_drop_state.host_is_managed(_LAPTOP_ID) is False
    assert await runner_drop_state.host_is_managed("0123456789abcdef0123456789abcdef") is False


async def test_without_a_host_store_no_host_is_managed(
    runtime_init: None, db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del runtime_init
    monkeypatch.setattr(runner_drop_state, "_PROBE_TTL_S", 0.0)
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    bare = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
    )
    assert bare.state.host_store is None
    assert await runner_drop_state.host_is_managed(_SANDBOX_ID) is False
    assert await runner_drop_state.host_is_online(_LAPTOP_ID) is False
