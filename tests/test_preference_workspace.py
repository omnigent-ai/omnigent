"""Scope/import journeys through a durable cache and the authoritative SQL store."""

import httpx
import pytest

from omnigent.preference_client import PreferenceClient
from omnigent.preference_sync import SharedPreferenceKey
from omnigent.preference_workspace import ImportPreview, PreferenceWorkspace
from omnigent.preferences import (
    PreferenceKey,
    PreferenceKind,
    PreferenceScope,
    resolve_preferences,
)
from tests.test_preference_client import MODEL, transport
from tests.test_preference_client import peers as peers


@pytest.mark.asyncio
async def test_machine_scopes_import_reopen_and_restore_conflict(peers):
    store, (local, remote) = peers
    workspace = PreferenceWorkspace(local)
    async with httpx.AsyncClient(base_url="http://test", transport=transport(store)) as http:
        await local.sync(http)
        await remote.sync(http)
        local.enqueue(MODEL, "original")
        await local.sync(http)
        preview = workspace.preview_config(
            {
                "model": "imported",
                "harness": {
                    "default": "pi-native",
                    "pi-native": {
                        "command": "private-executable",
                        "args": [],
                    },
                },
                "credentials": "private-token",
            }
        )
        assert "private" not in preview.model_dump_json()
        assert local.pending() == []
        workspace.set_machine(MODEL, "experiment")
        queued = workspace.apply_import(
            ImportPreview.model_validate_json(preview.model_dump_json())
        )
        reopened = PreferenceClient(
            local.path, identity=store.identity("alice"), installation_id="laptop"
        )
        assert [item.operation_id for item in reopened.pending()] == queued
        snapshot = await reopened.sync(http)
        workspace = PreferenceWorkspace(reopened)
        resolved = resolve_preferences(
            workspace.context, [snapshot.as_layer(), workspace.machine_layer()]
        )
        key = PreferenceKey(PreferenceKind.MODEL, "model")
        assert (
            resolved[key].value == "experiment" and resolved[key].source == PreferenceScope.MACHINE
        )
        assert not PreferenceWorkspace(remote).machine_layer().values
        workspace.set_machine(MODEL, None)
        assert (
            resolve_preferences(
                workspace.context, [snapshot.as_layer(), workspace.machine_layer()]
            )[key].value
            == "imported"
        )
        restore = workspace.preview_restore()
        await remote.sync(http)
        remote.enqueue(MODEL, "later")
        await remote.sync(http)
        workspace.apply_import(restore)
        result = await reopened.sync(http)
        assert next(entry.value for entry in result.entries if entry.key == MODEL) == "later"
        assert [conflict.value for conflict in result.conflicts] == ["original"]
        assert next(entry.value for entry in result.entries if entry.key.kind == "harness") is None


@pytest.mark.asyncio
async def test_import_preserves_absent_entries_and_refuses_pending_or_foreign_plan(peers):
    store, (local, _) = peers
    workspace = PreferenceWorkspace(local)
    async with httpx.AsyncClient(base_url="http://test", transport=transport(store)) as http:
        await local.sync(http)
        local.enqueue(SharedPreferenceKey(kind="model", name="opencode_model"), "keep")
        await local.sync(http)
        plan = workspace.preview_config({"model": "imported"})
        with pytest.raises(ValueError, match="another identity"):
            workspace.apply_import(plan.model_copy(update={"identity": store.identity("bob")}))
        local.enqueue(MODEL, "pending")
        with pytest.raises(ValueError, match="pending edits"):
            workspace.apply_import(plan)
        await local.sync(http)
        workspace.apply_import(plan)
        with pytest.raises(ValueError, match="unacknowledged"):
            workspace.preview_restore()
        result = await local.sync(http)
        assert {entry.value for entry in result.entries} == {"keep", "pending"}
        assert [conflict.value for conflict in result.conflicts] == ["imported"]
        assert workspace.preview_restore().changes == []


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [1, 2])
async def test_local_schema_upgrade_preserves_pending_identity_and_cache(peers, version):
    store, (local, _) = peers
    async with httpx.AsyncClient(base_url="http://test", transport=transport(store)) as http:
        snapshot = await local.sync(http)
    operation_id = local.enqueue(MODEL, "offline")
    with local._transaction() as connection:
        if version == 1:
            connection.execute("DROP TABLE machine")
            connection.execute("DROP TABLE import_entries")
        connection.execute("ALTER TABLE state DROP COLUMN host_adopted")
        connection.execute(f"PRAGMA user_version = {version}")
    reopened = PreferenceClient(
        local.path, identity=store.identity("alice"), installation_id="laptop"
    )
    assert reopened.cached() == snapshot
    assert [item.operation_id for item in reopened.pending()] == [operation_id]
    workspace = PreferenceWorkspace(reopened)
    workspace.set_machine(SharedPreferenceKey(kind="harness_args", name="pi-native"), [])
    workspace.set_machine(
        SharedPreferenceKey(
            kind="plugin", name={"ecosystem": "claude", "source": "test", "name": "p"}
        ),
        False,
    )
    assert set(workspace.machine_layer().values.values()) == {(), False}
