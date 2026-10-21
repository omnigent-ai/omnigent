"""Execute explicit preference controls using this host's verified local profile."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from omnigent.config import load_global_config, load_local_config
from omnigent.preference_client import _disk_call
from omnigent.preference_connection import (
    PreferenceConnection,
    connect_preferences,
    load_connection,
    open_preference_http,
)
from omnigent.preference_controls import (
    PreferenceApply,
    PreferenceControl,
    PreferenceControlResult,
    PreferenceDefaults,
    PreferenceDiscard,
    PreferenceImport,
    PreferenceMachine,
    PreferencePrepare,
)
from omnigent.preference_startup import PreferenceLaunch
from omnigent.preference_sync import PreferenceNamespace, PreferenceSnapshot, SharedPreferenceEntry
from omnigent.preference_workspace import preview_import, shared_key


async def control_preferences(
    server_url: str,
    identity: PreferenceNamespace,
    control: PreferenceControl,
    *,
    shared: PreferenceSnapshot | None = None,
) -> PreferenceControlResult:
    """Inspection never adopts; preview may warm a cache without saving a profile."""
    if isinstance(control, PreferenceApply) and control.preview.identity != identity:
        raise ValueError("Import belongs to another account or workspace")
    if isinstance(control, PreferencePrepare):
        if shared is None or shared.identity != identity or shared.revision == 0:
            raise ValueError("Choose shared defaults in Settings before starting a session")
        from omnigent.host.identity import load_or_create_host_identity

        # The authenticated owner tunnel authorizes this request, not future offline CLI use.
        installation_id = (await asyncio.to_thread(load_or_create_host_identity)).host_id
        workspace = await asyncio.to_thread(
            PreferenceConnection(
                server_url=server_url, identity=identity, installation_id=installation_id
            ).workspace
        )
        await _disk_call(workspace.client._accept_snapshot, shared)
        machine = await asyncio.to_thread(workspace.machine_layer)
        pending = await asyncio.to_thread(workspace.client.pending)
        launch = PreferenceLaunch(
            shared=shared,
            installation_id=installation_id,
            state="current",
            machine=[
                SharedPreferenceEntry(
                    key=shared_key(key),
                    version=0,
                    value=list(value) if isinstance(value, tuple) else value,
                )
                for key, value in machine.values.items()
            ],
            warning="Pending or competing preference edits need review in Settings."
            if pending or shared.conflicts
            else None,
        )

        def defaults() -> PreferenceDefaults:
            project = (
                load_local_config(Path(control.workspace) / ".omnigent/config.yaml")
                if control.workspace
                else {}
            )
            config = launch.resolve(load_global_config(), project).config
            harness = config.get("harness")
            return PreferenceDefaults.model_validate(
                {
                    "model": config.get("model"),
                    "opencode_model": config.get("opencode_model"),
                    "default_harness": harness.get("default")
                    if isinstance(harness, dict)
                    else harness,
                }
            )

        return PreferenceControlResult(
            connected=True,
            shared=shared,
            machine=launch.machine,
            pending=pending,
            launch=launch,
            warning=launch.warning,
            defaults=await asyncio.to_thread(defaults),
        )
    connection = (
        None
        if control.action == "connect"
        else await asyncio.to_thread(load_connection, server_url)
    )
    if connection is not None and connection.identity != identity:
        raise ValueError(
            "Host preferences belong to another account or workspace; reconnect locally"
        )
    connected = connection is not None
    if control.action == "connect" or (
        connection is None and control.action in ("preview_import", "apply_import")
    ):
        connection = await connect_preferences(
            server_url,
            expected_identity=identity,
            persist=control.action != "preview_import",
        )
        connected = control.action != "preview_import"
    if connection is None:
        if control.action == "status":
            return PreferenceControlResult(connected=False)
        raise ValueError("Connect this machine to workspace preferences first")
    workspace = await asyncio.to_thread(connection.workspace)
    preview = None
    warning = None
    synchronize = control.action == "sync"
    if isinstance(control, PreferenceMachine):
        await _disk_call(workspace.set_machine, control.key, control.value)
    elif isinstance(control, PreferenceDiscard):
        await _disk_call(workspace.client.discard, control.operation_id)
    elif isinstance(control, PreferenceImport):
        from omnigent.preference_plugins import import_plugin_values
        from omnigent.preferences import preferences_from_config

        def preview_local():
            snapshot = workspace.client.cached()
            if snapshot is None:
                raise ValueError("Synchronize before previewing an import")
            values = {
                **preferences_from_config(load_global_config()),
                **import_plugin_values(tuple(control.plugins)),
            }
            return preview_import(
                snapshot, installation_id=workspace.installation_id, values=values
            )

        preview = await asyncio.to_thread(preview_local)
    elif isinstance(control, PreferenceApply):
        await _disk_call(workspace.apply_import, control.preview)
        synchronize = True
    elif control.action == "preview_restore":
        preview = await asyncio.to_thread(workspace.preview_restore)
    if synchronize:
        try:
            async with asyncio.timeout(5):
                async with await open_preference_http(server_url) as http:
                    await workspace.client.sync(http)
        except (httpx.TransportError, TimeoutError):
            warning = (
                "Synchronization unavailable. Queued edits remain on this machine; retry sync."
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in (409, 429) and exc.response.status_code < 500:
                raise
            warning = "Synchronization failed. Inspect pending edits and conflicts, then retry."
    machine = await asyncio.to_thread(workspace.machine_layer)
    return PreferenceControlResult(
        connected=connected,
        shared=await asyncio.to_thread(workspace.client.cached),
        machine=[
            SharedPreferenceEntry(
                key=shared_key(key),
                version=0,
                value=list(value) if isinstance(value, tuple) else value,
            )
            for key, value in machine.values.items()
        ],
        pending=await asyncio.to_thread(workspace.client.pending),
        preview=preview,
        warning=warning,
    )
