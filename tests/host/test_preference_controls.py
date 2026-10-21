"""Host controls keep account binding and reviewed import bases intact."""

import httpx
import pytest

from omnigent.config import load_global_config, save_global_config
from omnigent.host.preferences import control_preferences
from omnigent.preference_controls import PREFERENCE_CONTROL
from omnigent.preference_sync import SharedPreferenceKey
from omnigent.stores.preference_store.sqlalchemy_store import SqlAlchemyPreferenceStore
from tests.test_preference_client import transport


async def test_host_checks_authenticated_identity_before_adopting(tmp_path, monkeypatch, db_uri):
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    store = SqlAlchemyPreferenceStore(db_uri, authority_id="controls")

    async def open_http(_):
        return httpx.AsyncClient(base_url="http://test", transport=transport(store))

    monkeypatch.setattr("omnigent.preference_connection.open_preference_http", open_http)
    monkeypatch.setattr("omnigent.host.preferences.open_preference_http", open_http)

    async def control(identity, **payload):
        return await control_preferences(
            "http://test", identity, PREFERENCE_CONTROL.validate_python(payload)
        )

    alice, bob = store.identity("alice"), store.identity("bob")
    save_global_config({"model": "portable", "server": "http://test"})
    assert not (await control(alice, action="status")).connected
    with pytest.raises(ValueError, match="login does not match"):
        await control(bob, action="connect")
    assert "shared_preferences" not in load_global_config()
    preview = await control(alice, action="preview_import")
    assert preview.preview.changes[0].operation.value == "portable"
    assert "shared_preferences" not in load_global_config()
    await control(alice, action="connect")
    before = load_global_config()
    with pytest.raises(ValueError, match="another account or workspace"):
        await control(
            bob, action="set_machine", key={"kind": "model", "name": "model"}, value="wrong"
        )
    assert load_global_config() == before
    assert not (await control(alice, action="status")).machine

    # An outage after queuing an import retains its reviewed mutation for later retry.
    async def unavailable(_):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr("omnigent.host.preferences.open_preference_http", unavailable)
    queued = await control(
        alice, action="apply_import", preview=preview.preview.model_dump(mode="json")
    )
    assert queued.warning and len(queued.pending) == 1
    assert queued.pending[0] == preview.preview.changes[0].operation
    monkeypatch.setattr("omnigent.host.preferences.open_preference_http", open_http)
    synced = await control(alice, action="sync")
    assert not synced.pending
    assert (
        next(
            entry.value
            for entry in synced.shared.entries
            if entry.key == SharedPreferenceKey(kind="model", name="model")
        )
        == "portable"
    )
    monkeypatch.setenv("OMNIGENT_API_KEY", "changed-test-login")
    with pytest.raises(ValueError, match="login changed"):
        await control(alice, action="status")
    assert (await control(alice, action="connect")).connected


async def test_prepare_uses_owner_snapshot_without_copying_login(tmp_path, monkeypatch, db_uri):
    from uuid import uuid4

    from omnigent.preference_connection import PreferenceConnection
    from omnigent.preference_sync import PreferenceMutation, PreferenceSnapshot
    from omnigent.stores.preference_store import read_store_snapshot

    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    store = SqlAlchemyPreferenceStore(db_uri, authority_id="prepare")
    identity = store.identity("alice")
    store.apply(
        PreferenceMutation(
            protocol_version=1,
            identity=identity,
            operation_id=uuid4(),
            installation_id="source",
            key=SharedPreferenceKey(kind="model", name="model"),
            expected_version=0,
            value="shared",
        ),
        user_id="alice",
    )
    shared = read_store_snapshot(store, "alice")
    project = tmp_path / "project"
    (project / ".omnigent").mkdir(parents=True)
    (project / ".omnigent/config.yaml").write_text(
        "model: project\nharness:\n  default: pi-native\n"
    )
    save_global_config({"model": "legacy", "api_key": "private"})
    from omnigent.host.identity import load_or_create_host_identity

    load_or_create_host_identity()
    original = load_global_config()

    async def no_credentials(*args, **kwargs):
        pytest.fail("Preparing through an owner tunnel must not request user credentials")

    monkeypatch.setattr("omnigent.host.preferences.open_preference_http", no_credentials)
    action = PREFERENCE_CONTROL.validate_python({"action": "prepare", "workspace": str(project)})
    prepared = await control_preferences("http://server", identity, action, shared=shared)
    assert prepared.launch.shared == shared
    assert prepared.defaults.model == "project"
    assert prepared.defaults.default_harness == "pi-native"
    assert load_global_config() == original
    assert "private" not in prepared.model_dump_json()
    workspace = PreferenceConnection(
        server_url="http://server",
        identity=identity,
        installation_id=prepared.launch.installation_id,
    ).workspace()
    workspace.set_machine(SharedPreferenceKey(kind="model", name="model"), "machine")
    plain = PREFERENCE_CONTROL.validate_python({"action": "prepare"})
    prepared = await control_preferences("http://server", identity, plain, shared=shared)
    assert prepared.defaults.model == "machine"
    assert prepared.launch.machine[0].value == "machine"
    with pytest.raises(ValueError, match="Choose shared defaults"):
        await control_preferences("http://server", store.identity("bob"), plain, shared=shared)
    with pytest.raises(ValueError, match="Choose shared defaults"):
        await control_preferences(
            "http://server", identity, plain, shared=PreferenceSnapshot(identity=identity)
        )
