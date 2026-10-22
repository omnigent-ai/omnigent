"""Bounded preference refresh at new-session boundaries, with frozen runner handoff."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Literal

import httpx
from filelock import FileLock, Timeout
from pydantic import Field

from omnigent.config import ResolvedConfig, resolve_effective_config
from omnigent.install_ledger import atomic_write_json
from omnigent.preference_connection import PreferenceConnection, open_preference_http
from omnigent.preference_sync import (
    MAX_KEYS,
    Name,
    PreferenceSnapshot,
    ProtocolModel,
    SharedPreferenceEntry,
)
from omnigent.preferences import PreferenceContext, PreferenceLayer, PreferenceScope

PREFERENCE_SNAPSHOT_ENV = "OMNIGENT_PREFERENCE_SNAPSHOT"
MAX_SESSION_SNAPSHOTS = 2048


class PreferenceLaunch(ProtocolModel):
    """An immutable choice for a session; later sync never rewrites this handoff."""

    protocol_version: Literal[1] = 1
    shared: PreferenceSnapshot
    installation_id: Name
    machine: list[SharedPreferenceEntry] = Field(default_factory=list, max_length=MAX_KEYS)
    state: Literal["current", "cached"]
    warning: str | None = None

    def resolve(
        self, global_config: dict[str, object], project_config: dict[str, object]
    ) -> ResolvedConfig:
        identity = self.shared.identity.as_identity()
        machine = PreferenceSnapshot(
            identity=self.shared.identity, entries=self.machine
        ).as_layer()
        return resolve_effective_config(
            global_config,
            project_config,
            preference_context=PreferenceContext(identity, self.installation_id),
            preference_layers=(
                self.shared.as_layer(),
                PreferenceLayer(
                    PreferenceScope.MACHINE,
                    identity,
                    machine.values,
                    installation_id=self.installation_id,
                ),
            ),
        )


async def prepare_preferences(
    connection: PreferenceConnection,
    *,
    timeout: float = 5,
    refresh: bool = True,
) -> PreferenceLaunch:
    """Refresh once; only transient outages can use an existing verified cache."""
    workspace = await asyncio.to_thread(connection.workspace)
    state: Literal["current", "cached"] = "current"
    warning = None
    try:
        if refresh:
            async with asyncio.timeout(timeout):
                async with await open_preference_http(connection.server_url) as http:
                    snapshot = await workspace.client.sync(http)
        else:
            snapshot = await asyncio.to_thread(workspace.client.cached)
            if snapshot is None:
                raise ValueError("Preferences have not been synchronized")
    except (httpx.TransportError, TimeoutError, Timeout) as exc:
        snapshot = await asyncio.to_thread(workspace.client.cached)
        state = "cached"
        warning = "Preferences could not synchronize; using this workspace's last complete cache."
        if snapshot is None:
            raise ValueError(
                "Preferences unavailable. Retry or explicitly use temporary defaults."
            ) from exc
    except httpx.HTTPStatusError as exc:
        # Authentication failures and wrong identities must never borrow an old account's cache.
        if exc.response.status_code not in (409, 429) and exc.response.status_code < 500:
            raise
        snapshot = await asyncio.to_thread(workspace.client.cached)
        if snapshot is None:
            raise ValueError(
                "Preferences unavailable. Retry or explicitly use temporary defaults."
            ) from exc
        state = "cached"
        warning = (
            f"Preference synchronization returned HTTP {exc.response.status_code}; "
            "inspect pending edits."
        )
    if snapshot.conflicts:
        warning = (
            "Shared preferences have competing edits. "
            "Review them with `omnigent preferences show`."
        )
    from omnigent.preference_connection import account_context

    if connection.account_context is not None and connection.account_context != account_context(
        connection.server_url
    ):
        raise ValueError(
            "Login changed while loading preferences; reconnect before using the cache"
        )
    if snapshot.revision == 0:
        raise ValueError(
            "Choose your shared defaults once: preview/import an existing config, or run "
            "`omnigent preferences initialize-empty`. Temporary defaults never publish."
        )
    from omnigent.preference_workspace import shared_key

    machine = await asyncio.to_thread(workspace.machine_layer)
    return PreferenceLaunch(
        shared=snapshot,
        installation_id=workspace.installation_id,
        state=state,
        warning=warning,
        machine=[
            SharedPreferenceEntry(
                key=shared_key(key),
                version=0,
                value=list(value) if isinstance(value, tuple) else value,
            )
            for key, value in machine.values.items()
        ],
    )


async def prepare_server_preferences(
    server_url: str, *, timeout: float = 5
) -> PreferenceLaunch | None:
    """Bound discovery and the first refresh together; a new connection already refreshed."""
    from omnigent.preference_connection import connect_preferences, load_connection

    connection = await asyncio.to_thread(load_connection, server_url)
    if connection is not None:
        return await prepare_preferences(connection, timeout=timeout)
    if os.environ.get("OMNIGENT_SHARED_PREFERENCES") != "1":
        return None
    try:
        async with asyncio.timeout(timeout):
            connection = await connect_preferences(server_url)
    except TimeoutError as exc:
        raise ValueError(
            "Preferences unavailable. Retry or explicitly use temporary defaults."
        ) from exc
    return await prepare_preferences(connection, refresh=False)


def write_launch_snapshot(path: Path, launch: PreferenceLaunch) -> None:
    """Create a private per-session handoff, without copying credentials or commands."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with FileLock(str(path.parent / ".lock"), timeout=5):
        if path.exists():
            existing = PreferenceLaunch.model_validate_json(path.read_bytes())
            if (existing.shared.identity, existing.installation_id) != (
                launch.shared.identity,
                launch.installation_id,
            ):
                raise ValueError("Session preference snapshot belongs to another identity")
            return
        # Never evict a running/resumable session's frozen choice to admit another.
        from itertools import islice

        if (
            len(list(islice(path.parent.glob("*.json"), MAX_SESSION_SNAPSHOTS)))
            >= MAX_SESSION_SNAPSHOTS
        ):
            raise ValueError(
                "Session preference cache is full; remove snapshots for discarded sessions "
                "or explicitly use temporary defaults."
            )
        atomic_write_json(path, launch.model_dump(mode="json"))


def inherited_launch() -> PreferenceLaunch | None:
    path = os.environ.get(PREFERENCE_SNAPSHOT_ENV)
    return PreferenceLaunch.model_validate_json(Path(path).read_bytes()) if path else None


def temporary_preference_labels() -> dict[str, str]:
    """Carry an explicit temporary choice through the existing session metadata path."""
    import click

    context = click.get_current_context(silent=True)
    return (
        {"omnigent.preferences.temporary": "true"}
        if context and context.meta.get("temporary_preferences")
        else {}
    )


async def host_preference_environment(
    server_url: str,
    session_id: str,
    *,
    temporary: bool = False,
) -> dict[str, str]:
    """Freeze one session's defaults; registration never waits for this operation."""
    import hashlib

    from omnigent.preference_connection import load_connection
    from omnigent.process_logging import data_dir

    if temporary:
        return {}
    connection = await asyncio.to_thread(load_connection, server_url)
    digest = hashlib.sha256(session_id.encode()).hexdigest()
    path = data_dir() / "preferences" / "sessions" / f"{digest}.json"
    if path.exists() and connection is not None:
        existing = PreferenceLaunch.model_validate_json(path.read_bytes())
        if (existing.shared.identity, existing.installation_id) != (
            connection.identity,
            connection.installation_id,
        ):
            raise ValueError("Session preference snapshot belongs to another identity")
    else:
        launch = await prepare_server_preferences(server_url)
        if launch is None:
            return {}
        await asyncio.to_thread(write_launch_snapshot, path, launch)
    return {PREFERENCE_SNAPSHOT_ENV: str(path)}


async def retry_pending_preferences(server_url: str) -> None:
    """Coalesce reconnect work into bounded jittered retries; never poll an idle outbox."""
    import random

    from omnigent.preference_connection import load_connection

    connection = await asyncio.to_thread(load_connection, server_url)
    if connection is None:
        return
    workspace = await asyncio.to_thread(connection.workspace)
    for attempt in range(4):
        if not await asyncio.to_thread(workspace.client.pending):
            return
        await asyncio.sleep(random.uniform(0, min(30, 2**attempt)))
        try:
            async with asyncio.timeout(5):
                async with await open_preference_http(server_url) as http:
                    await workspace.client.sync(http)
        except (httpx.TransportError, TimeoutError, Timeout):
            continue
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429 or exc.response.status_code >= 500:
                continue
            raise
