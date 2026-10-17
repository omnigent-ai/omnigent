"""Server wiring for Databricks Sandboxes that run as their owner.

``create_app`` turns the Databricks Connect store into a blocking per-user
resolver on the sandbox deployment, so an owner-identity launcher acts with
the session owner's own (server-refreshed) token.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from fastapi import FastAPI

from omnigent.connections.databricks import DatabricksConnectionStore
from omnigent.db.utils import now_epoch
from omnigent.onboarding.sandboxes.databricks_sandbox import DatabricksSandboxLauncher
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.connections_registry import blocking_owner_resolver, connection_providers
from omnigent.server.databricks_app import DatabricksConfig, DatabricksTokenSet
from omnigent.server.managed_hosts import build_owner_launcher, parse_sandbox_config
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_store import HostStore

_WS = "https://ws.cloud.databricks.com"


class _MemoryCipher:
    def __init__(self) -> None:
        self.values: dict[str, tuple[str, dict[str, str]]] = {}

    def encrypt(self, plaintext: str, *, context: Mapping[str, str]) -> str:
        ciphertext = uuid.uuid4().hex
        self.values[ciphertext] = (plaintext, dict(context))
        return ciphertext

    def decrypt(self, ciphertext: str, *, context: Mapping[str, str]) -> str | None:
        plaintext, expected = self.values[ciphertext]
        return plaintext if expected == dict(context) else None


def _connect(store: DatabricksConnectionStore, user: str, token: str, *, ttl_s: int) -> None:
    store.upsert(
        user,
        workspace_host=_WS,
        databricks_user=user,
        databricks_user_id="1",
        tokens=DatabricksTokenSet(token, f"refresh-{user}", now_epoch() + ttl_s, None, "all-apis"),
    )


def _databricks_provider():
    (provider,) = [p for p in connection_providers() if p.name == "databricks"]
    return provider


async def test_owner_resolver_refreshes_a_token_that_would_lapse_before_the_next_keepalive(
    db_uri: str,
) -> None:
    store = DatabricksConnectionStore(db_uri, _MemoryCipher())
    # 10 minutes left: fine for the broker's 5-minute margin, too short for a
    # proxy bearer that the keepalive only rewrites every ~10 minutes.
    _connect(store, "alice", "stale", ttl_s=600)
    client = SimpleNamespace(
        refresh_token=AsyncMock(
            return_value=DatabricksTokenSet("fresh", "r2", now_epoch() + 3600, None, "all-apis")
        )
    )
    provider = _databricks_provider()
    assert provider.owner_identity_resolver is not None
    assert provider.credential_resolver is not None
    resolve = blocking_owner_resolver(provider.owner_identity_resolver, store=store, client=client)

    payload = await asyncio.to_thread(resolve, "alice")

    assert payload == {"token": "fresh", "workspace_host": _WS}
    client.refresh_token.assert_awaited_once_with(_WS, "refresh-alice")
    # The broker path keeps its own, shorter margin.
    broker = await provider.credential_resolver("alice", store=store, client=client)
    assert broker == {"token": "fresh", "workspace_host": _WS}
    assert await asyncio.to_thread(resolve, "bob") is None


def _app(db_uri: str, tmp_path: Path, **kwargs: Any) -> FastAPI:
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    sandbox = parse_sandbox_config(
        {
            "provider": "databricks",
            "server_url": "https://srv.example",
            "databricks": {"identity": "owner", "workspace_host": _WS},
        }
    )
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        comment_store=SqlAlchemyCommentStore(db_uri),
        host_store=HostStore(db_uri),
        sandbox_config=sandbox,
        **kwargs,
    )


async def test_create_app_binds_owner_launchers_to_databricks_connect(
    runtime_init: None, db_uri: str, tmp_path: Path
) -> None:
    store = DatabricksConnectionStore(db_uri, _MemoryCipher())
    _connect(store, "alice@example.com", "alice-token", ttl_s=3600)
    app = _app(
        db_uri,
        tmp_path,
        databricks_config=DatabricksConfig(
            client_id="cid", client_secret="secret", redirect_uri=f"{_WS}/cb", scopes="all-apis"
        ),
        databricks_store=store,
    )
    deployment = app.state.sandbox_config

    assert set(deployment.owner_credentials) == {"databricks"}
    launcher = build_owner_launcher(deployment.default, deployment, "alice@example.com")
    assert isinstance(launcher, DatabricksSandboxLauncher)
    token, host = await asyncio.to_thread(launcher._resolve_owner)
    assert (token, host) == ("alice-token", _WS)


async def test_create_app_without_databricks_connect_binds_nothing(
    runtime_init: None, db_uri: str, tmp_path: Path
) -> None:
    app = _app(db_uri, tmp_path)

    assert dict(app.state.sandbox_config.owner_credentials) == {}


async def test_owner_resolver_keeps_the_callers_workspace_scope() -> None:
    from omnigent.db.db_models import current_workspace_id, workspace_scope

    seen: list[int] = []

    async def resolver(user_id: str, *, store: object, client: object) -> dict[str, object]:
        seen.append(current_workspace_id())
        return {"token": user_id, "workspace_host": _WS}

    resolve = blocking_owner_resolver(resolver, store=None, client=None)

    def _in_scope() -> dict[str, object] | None:
        with workspace_scope(7):
            return resolve("alice")

    assert await asyncio.to_thread(_in_scope) == {"token": "alice", "workspace_host": _WS}
    assert seen == [7]
