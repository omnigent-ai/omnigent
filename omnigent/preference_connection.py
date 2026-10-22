"""Local opt-in profiles and authenticated transport for workspace preferences."""

from __future__ import annotations

import asyncio
import hashlib
import json
from urllib.parse import urlsplit

import httpx

from omnigent.cli_auth import load_databricks_org_id, open_server_client
from omnigent.config import global_config_path, load_global_config, save_global_config
from omnigent.preference_client import PreferenceClient, read_preference_snapshot
from omnigent.preference_sync import Name, PreferenceNamespace, ProtocolModel
from omnigent.preference_workspace import PreferenceWorkspace


class PreferenceConnection(ProtocolModel):
    server_url: str
    identity: PreferenceNamespace
    installation_id: Name

    def workspace(self) -> PreferenceWorkspace:
        digest = hashlib.sha256(self.identity.model_dump_json().encode()).hexdigest()
        return PreferenceWorkspace(
            PreferenceClient(
                global_config_path().parent / "preferences" / digest / "cache.db",
                identity=self.identity,
                installation_id=self.installation_id,
            )
        )


def connection_key(server_url: str) -> str:
    """The route selects a profile; the profile's authenticated namespace binds its data."""
    route = json.dumps([server_url.rstrip("/"), load_databricks_org_id(server_url)])
    return hashlib.sha256(route.encode()).hexdigest()


def load_connection(server_url: str) -> PreferenceConnection | None:
    profiles = load_global_config().get("shared_preferences", {})
    if not isinstance(profiles, dict):
        raise ValueError("Invalid shared preference profiles")
    raw = profiles.get(connection_key(server_url))
    return PreferenceConnection.model_validate(raw) if raw is not None else None


async def open_preference_http(server_url: str) -> httpx.AsyncClient:
    from omnigent.runner._entry import _make_auth_token_factory

    def token() -> str | None:
        factory = _make_auth_token_factory(
            server_url=server_url,
            _allow_initial_token=False,
            _allow_delegated_mint=False,
        )
        return factory() if factory is not None else None

    return open_server_client(server_url, bearer_token=await asyncio.to_thread(token), timeout=5)


async def connect_preferences(server_url: str) -> PreferenceConnection:
    """Explicitly bind to the authenticated server without importing any local values."""
    parsed = urlsplit(server_url)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.netloc
        or parsed.username
        or parsed.query
    ):
        raise ValueError("Use the server API URL without credentials or query parameters")
    server_url = server_url.rstrip("/")
    async with await open_preference_http(server_url) as http:
        snapshot = await read_preference_snapshot(http)
        from omnigent.host.identity import load_or_create_host_identity

        installation_id = load_or_create_host_identity().host_id
        connection = PreferenceConnection(
            server_url=server_url,
            identity=snapshot.identity,
            installation_id=installation_id,
        )
        workspace = connection.workspace()
        await asyncio.to_thread(workspace.client._accept_snapshot, snapshot)
    save_global_config(
        {"shared_preferences": {connection_key(server_url): connection.model_dump(mode="json")}},
        deep_merge_keys=("shared_preferences",),
    )
    return connection
