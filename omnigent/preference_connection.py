"""Local opt-in profiles and authenticated transport for workspace preferences."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
from concurrent.futures import Future
from contextvars import copy_context
from urllib.parse import urlsplit

import httpx

from omnigent.cli_auth import load_databricks_org_id, open_server_client
from omnigent.config import global_config_path, load_global_config, save_global_config
from omnigent.preference_client import PreferenceClient, _disk_call, read_preference_snapshot
from omnigent.preference_sync import Name, PreferenceNamespace, ProtocolModel
from omnigent.preference_workspace import PreferenceWorkspace

_token_lock = threading.Lock()
_token_work: dict[tuple[str, str], Future[str | None]] = {}


class PreferenceConnection(ProtocolModel):
    server_url: str
    identity: PreferenceNamespace
    installation_id: Name
    account_context: str | None = None

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


def account_context(server_url: str) -> str:
    """Bind offline use to the selected login without storing credential material."""
    from omnigent.cli_auth import _load_entry

    entry = _load_entry(server_url) or {}
    context = {key: entry.get(key) for key in ("user_id", "auth_type", "workspace_host", "org_id")}
    # Unknown-principal credentials may rotate; re-verification is preferable to
    # borrowing another account's offline defaults.
    if not entry.get("user_id"):
        context["token"] = entry.get("token")
    context.update(
        {
            key: os.environ.get(key)
            for key in (
                "DATABRICKS_TOKEN",
                "DATABRICKS_HOST",
                "DATABRICKS_CONFIG_PROFILE",
                "OMNIGENT_API_KEY",
            )
        }
    )
    return hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()


def load_connection(server_url: str) -> PreferenceConnection | None:
    profiles = load_global_config().get("shared_preferences", {})
    if not isinstance(profiles, dict):
        raise ValueError("Invalid shared preference profiles")
    raw = profiles.get(connection_key(server_url))
    if raw is None:
        return None
    connection = PreferenceConnection.model_validate(raw)
    if connection.account_context != account_context(server_url):
        raise ValueError(
            "Preference login changed or needs verification. "
            "Run `omnigent preferences --server URL connect` before using cached defaults."
        )
    return connection


async def open_preference_http(server_url: str) -> httpx.AsyncClient:
    from omnigent.runner._entry import _make_auth_token_factory

    def token() -> str | None:
        factory = _make_auth_token_factory(
            server_url=server_url,
            _allow_initial_token=False,
            _allow_delegated_mint=False,
        )
        return factory() if factory is not None else None

    # A blocked SDK credential lookup must not hold asyncio.run's executor
    # shutdown past the startup deadline. Reconnects share unfinished work.
    context_key = (server_url, account_context(server_url))
    with _token_lock:
        pending = _token_work.get(context_key)
        if pending is None:
            pending = Future()
            _token_work[context_key] = pending
            context = copy_context()

            def resolve(result: Future[str | None]) -> None:
                try:
                    result.set_result(context.run(token))
                except BaseException as exc:  # noqa: BLE001 — forward worker failures to its caller
                    result.set_exception(exc)
                finally:
                    with _token_lock:
                        if _token_work.get(context_key) is result:
                            del _token_work[context_key]

            threading.Thread(target=resolve, args=(pending,), daemon=True).start()
    bearer = await asyncio.shield(asyncio.wrap_future(pending))
    return open_server_client(server_url, bearer_token=bearer, timeout=5)


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
    initial_context = account_context(server_url)
    async with await open_preference_http(server_url) as http:
        snapshot = await read_preference_snapshot(http)
        if account_context(server_url) != initial_context:
            raise ValueError("Login changed while connecting preferences; retry")
        from omnigent.host.identity import load_or_create_host_identity

        installation_id = load_or_create_host_identity().host_id
        connection = PreferenceConnection(
            server_url=server_url,
            identity=snapshot.identity,
            installation_id=installation_id,
            account_context=initial_context,
        )
        workspace = await asyncio.to_thread(connection.workspace)
        await _disk_call(workspace.client._accept_snapshot, snapshot)
    save_global_config(
        {"shared_preferences": {connection_key(server_url): connection.model_dump(mode="json")}},
        deep_merge_keys=("shared_preferences",),
    )
    return connection
