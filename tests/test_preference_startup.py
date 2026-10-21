import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import click
import httpx
import pytest

from omnigent.config import load_effective_config
from omnigent.preference_connection import PreferenceConnection
from omnigent.preference_startup import (
    PREFERENCE_SNAPSHOT_ENV,
    host_preference_environment,
    prepare_preferences,
    retry_pending_preferences,
    temporary_preference_labels,
)
from omnigent.preference_sync import PreferenceSnapshot
from omnigent.stores.preference_store.sqlalchemy_store import SqlAlchemyPreferenceStore
from tests.test_preference_client import MODEL, transport


@pytest.fixture
def startup(tmp_path, db_uri, monkeypatch):
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    store = SqlAlchemyPreferenceStore(db_uri, authority_id="startup")
    connection = PreferenceConnection(
        server_url="http://test", identity=store.identity("alice"), installation_id="machine"
    )

    async def open_http(_url):
        return httpx.AsyncClient(base_url="http://test", transport=transport(store))

    monkeypatch.setattr("omnigent.preference_startup.open_preference_http", open_http)
    monkeypatch.setattr("omnigent.preference_connection.load_connection", lambda _url: connection)
    return store, connection


@pytest.mark.asyncio
async def test_startup_requires_initial_choice_then_pins_session_across_reconnect(
    startup, monkeypatch, tmp_path
):
    store, connection = startup
    local = connection.workspace().client
    async with httpx.AsyncClient(base_url="http://test", transport=transport(store)) as http:
        await local.sync(http)
        with pytest.raises(ValueError, match="Choose your shared defaults"):
            await prepare_preferences(connection)
        local.enqueue(MODEL, "initial")
        await local.sync(http)
    connection.workspace().set_machine(MODEL, "experiment")
    env = await host_preference_environment("http://test", "session-a")
    monkeypatch.setenv(PREFERENCE_SNAPSHOT_ENV, env[PREFERENCE_SNAPSHOT_ENV])
    assert load_effective_config(workspace=tmp_path)["model"] == "experiment"
    local.enqueue(MODEL, "next-session")
    connection.workspace().set_machine(MODEL, None)
    monkeypatch.setattr("random.uniform", lambda *_: 0)
    await retry_pending_preferences("http://test")
    assert local.pending() == []
    assert load_effective_config(workspace=tmp_path)["model"] == "experiment"
    assert await host_preference_environment("http://test", "session-a") == env
    newer = await host_preference_environment("http://test", "session-b")
    monkeypatch.setenv(PREFERENCE_SNAPSHOT_ENV, newer[PREFERENCE_SNAPSHOT_ENV])
    assert load_effective_config(workspace=tmp_path)["model"] == "next-session"
    assert await host_preference_environment("http://test", "temporary", temporary=True) == {}
    before = Path(newer[PREFERENCE_SNAPSHOT_ENV]).read_bytes()
    assert json.loads(before)["shared"]["identity"] == connection.identity.model_dump()


@pytest.mark.asyncio
async def test_cached_outage_wrong_identity_and_fresh_timeout(startup, monkeypatch):
    store, connection = startup
    local = connection.workspace().client
    async with httpx.AsyncClient(base_url="http://test", transport=transport(store)) as http:
        await local.sync(http)
        local.enqueue(MODEL, "saved")
        await local.sync(http)

    async def offline(_url):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr("omnigent.preference_startup.open_preference_http", offline)
    launch = await prepare_preferences(connection)
    assert launch.state == "cached" and launch.warning
    assert launch.resolve({}, {}).config["model"] == "saved"
    fresh = connection.model_copy(update={"identity": store.identity("bob")})
    with pytest.raises(ValueError, match="temporary defaults"):
        await prepare_preferences(fresh)

    async def wrong_user(_url):
        return httpx.AsyncClient(base_url="http://test", transport=transport(store, user="bob"))

    monkeypatch.setattr("omnigent.preference_startup.open_preference_http", wrong_user)
    with pytest.raises(ValueError, match="another identity"):
        await prepare_preferences(connection)
    assert local.cached().entries[0].value == "saved"

    async def stalled(_url):
        await asyncio.Event().wait()

    monkeypatch.setattr("omnigent.preference_startup.open_preference_http", stalled)
    with pytest.raises(ValueError, match="temporary defaults"):
        await prepare_preferences(fresh, timeout=0.02)


def test_temporary_choice_only_labels_its_cli_context():
    with click.Context(click.Command("run")) as context:
        assert temporary_preference_labels() == {}
        context.meta["temporary_preferences"] = True
        assert temporary_preference_labels() == {"omnigent.preferences.temporary": "true"}
    assert temporary_preference_labels() == {}


@pytest.mark.asyncio
async def test_reconnect_is_idle_or_bounded_and_retains_failed_edits(startup, monkeypatch):
    store, connection = startup
    calls = []
    delays = []

    async def unavailable(_url):
        calls.append(_url)
        raise httpx.ConnectError("offline")

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr("omnigent.preference_startup.open_preference_http", unavailable)
    monkeypatch.setattr(
        "omnigent.preference_startup.asyncio", SimpleNamespace(**{**vars(asyncio), "sleep": sleep})
    )
    await retry_pending_preferences("http://test")
    assert calls == [] and delays == []
    local = connection.workspace().client
    local._accept_snapshot(
        PreferenceSnapshot.model_validate(
            store.read(user_id="alice").model_dump(exclude={"next_cursor"})
        )
    )
    operation = local.enqueue(MODEL, "pending")
    await retry_pending_preferences("http://test")
    assert len(calls) == 4 and len(delays) == 4
    assert all(0 <= delay <= 2**attempt for attempt, delay in enumerate(delays))
    assert local.pending()[0].operation_id == operation


@pytest.mark.asyncio
async def test_discovery_budget_and_gate_off(monkeypatch):
    from omnigent.preference_startup import prepare_server_preferences

    monkeypatch.setattr("omnigent.preference_connection.load_connection", lambda _: None)
    monkeypatch.delenv("OMNIGENT_SHARED_PREFERENCES", raising=False)
    assert await prepare_server_preferences("http://test") is None

    async def stalled(_url):
        await asyncio.Event().wait()

    monkeypatch.setattr("omnigent.preference_connection.connect_preferences", stalled)
    monkeypatch.setenv("OMNIGENT_SHARED_PREFERENCES", "1")
    with pytest.raises(ValueError, match="temporary defaults"):
        await prepare_server_preferences("http://test", timeout=0.02)


@pytest.mark.parametrize("wrapper", [False, True])
def test_cli_resolves_once_and_skips_resume_and_temporary(startup, monkeypatch, wrapper):
    from omnigent.preference_cli_startup import resolve_cli_preferences
    from omnigent.preference_startup import PreferenceLaunch
    from omnigent.preference_sync import PreferenceSnapshot, SharedPreferenceEntry

    _, connection = startup
    calls = []

    async def prepare(server):
        calls.append(server)
        return PreferenceLaunch(
            shared=PreferenceSnapshot(
                identity=connection.identity,
                revision=1,
                entries=[SharedPreferenceEntry(key=MODEL, version=1, value="shared")],
            ),
            installation_id="machine",
            state="current",
        )

    monkeypatch.setattr("omnigent.preference_cli_startup.prepare_server_preferences", prepare)
    config = {"server": "http://test", "model": "legacy"}
    if wrapper:
        import os
        import tempfile

        from omnigent.preference_wrapper import (
            PREFERENCE_HANDOFF_FD_ENV,
            prepare_wrapper_preferences,
        )

        monkeypatch.setattr("omnigent.preference_wrapper.prepare_server_preferences", prepare)
        prepared = asyncio.run(prepare_wrapper_preferences("http://test"))
        with tempfile.TemporaryFile() as handoff:
            handoff.write(prepared.model_dump_json().encode())
            handoff.seek(0)
            descriptor = os.dup(handoff.fileno())
        monkeypatch.setenv(PREFERENCE_HANDOFF_FD_ENV, str(descriptor))
    with click.Context(click.Command("run"), info_name="run") as context:
        assert resolve_cli_preferences(config, {}).config["model"] == "shared"
        assert resolve_cli_preferences(config, {"model": "project"}).config["model"] == "project"
        assert calls == ["http://test"]
        context.params["resume"] = "session"
        assert resolve_cli_preferences(config, {}).config["model"] == "legacy"
        context.params.clear()
        context.meta["temporary_preferences"] = True
        assert resolve_cli_preferences(config, {}).config["model"] == "legacy"
        assert calls == ["http://test"]
    if wrapper:
        assert PREFERENCE_HANDOFF_FD_ENV not in os.environ
        with pytest.raises(OSError):
            os.fstat(descriptor)


@pytest.mark.parametrize("fault", ["server", "account", "invalid", "oversized"])
def test_wrapper_handoff_rejects_invalid_context_and_closes_descriptor(monkeypatch, fault):
    import os
    import tempfile

    from omnigent.preference_connection import account_context
    from omnigent.preference_wrapper import (
        MAX_HANDOFF_BYTES,
        PREFERENCE_HANDOFF_FD_ENV,
        PreparedPreferences,
        consume_wrapper_preferences,
    )

    prepared = PreparedPreferences(
        server_url="http://test", account_context=account_context("http://test"), launch=None
    )
    if fault == "server":
        prepared = prepared.model_copy(update={"server_url": "http://other"})
    elif fault == "account":
        prepared = prepared.model_copy(update={"account_context": "0" * 64})
    payload = prepared.model_dump_json().encode()
    if fault == "invalid":
        payload = b"not json"
    elif fault == "oversized":
        payload = b"x" * (MAX_HANDOFF_BYTES + 1)
    with tempfile.TemporaryFile() as handoff:
        handoff.write(payload)
        handoff.seek(0)
        descriptor = os.dup(handoff.fileno())
    monkeypatch.setenv(PREFERENCE_HANDOFF_FD_ENV, str(descriptor))
    with pytest.raises(ValueError):
        consume_wrapper_preferences("http://test")
    assert PREFERENCE_HANDOFF_FD_ENV not in os.environ
    with pytest.raises(OSError):
        os.fstat(descriptor)


def test_login_change_cannot_borrow_offline_cache(tmp_path, monkeypatch):
    from omnigent.cli_auth import clear_token, store_token
    from omnigent.config import save_global_config
    from omnigent.preference_connection import account_context, connection_key, load_connection
    from omnigent.preference_sync import PreferenceNamespace

    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    server = "http://test"
    store_token(server, "first-token", "alice", 9999999999)
    connection = PreferenceConnection(
        server_url=server,
        identity=PreferenceNamespace(authority_id="test", workspace_id="one", user_id="alice"),
        installation_id="machine",
        account_context=account_context(server),
    )
    save_global_config({"shared_preferences": {connection_key(server): connection.model_dump()}})
    assert load_connection(server) == connection
    store_token(server, "refreshed-token", "alice", 9999999999)
    assert load_connection(server) == connection
    store_token(server, "other-token", "bob", 9999999999)
    with pytest.raises(ValueError, match="login changed"):
        load_connection(server)
    clear_token(server)
    with pytest.raises(ValueError, match="login changed"):
        load_connection(server)


@pytest.mark.asyncio
async def test_snapshot_limit_and_installation_mismatch_are_visible(
    startup, tmp_path, monkeypatch
):
    from omnigent.preference_startup import write_launch_snapshot

    _, connection = startup
    client = connection.workspace().client
    store, _ = startup
    async with httpx.AsyncClient(base_url="http://test", transport=transport(store)) as http:
        await client.sync(http)
        client.enqueue(MODEL, "saved")
        await client.sync(http)
    launch = await prepare_preferences(connection)
    path = tmp_path / "snapshots" / "one.json"
    write_launch_snapshot(path, launch)
    assert path.stat().st_mode & 0o777 == 0o600
    monkeypatch.setattr("omnigent.preference_startup.MAX_SESSION_SNAPSHOTS", 1)
    write_launch_snapshot(path, launch)
    with pytest.raises(ValueError, match="cache is full"):
        write_launch_snapshot(path.with_name("two.json"), launch)
    with pytest.raises(ValueError, match="another identity"):
        write_launch_snapshot(path, launch.model_copy(update={"installation_id": "other"}))


def test_blocked_auth_does_not_hold_cli_executor_shutdown(tmp_path):
    import subprocess
    import sys
    import time

    script = """
import asyncio, time, pathlib, sys
from unittest.mock import patch
from omnigent.preference_startup import prepare_server_preferences

def blocked(**kwargs):
    pathlib.Path(sys.argv[1]).write_text("auth started")
    time.sleep(30)

async def run():
    with patch("omnigent.preference_connection.load_connection", return_value=None), \
         patch("omnigent.runner._entry._make_auth_token_factory", blocked):
        try:
            await prepare_server_preferences("http://test", timeout=0.05)
        except ValueError as exc:
            assert "temporary defaults" in str(exc)
        else:
            raise AssertionError("startup did not time out")
asyncio.run(run())
print("deadline respected")
"""
    from tests._helpers.live_server import isolated_process_env

    environment = isolated_process_env(tmp_path, {"OMNIGENT_SHARED_PREFERENCES": "1"}, base_env={})
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "auth-started")],
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "auth-started").is_file()
    assert result.stdout.strip() == "deadline respected"
    assert time.monotonic() - started < 10


@pytest.mark.asyncio
async def test_timed_out_reconnects_share_unfinished_credential_lookup(tmp_path, monkeypatch):
    import threading

    from omnigent.preference_connection import open_preference_http

    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    started, release = threading.Event(), threading.Event()
    calls = []

    def blocked(**_kwargs):
        calls.append(1)
        started.set()
        assert release.wait(10)
        return lambda: None

    monkeypatch.setattr("omnigent.runner._entry._make_auth_token_factory", blocked)
    try:
        for _ in range(2):
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.05):
                    await open_preference_http("http://credentials-test")
        assert started.is_set() and len(calls) == 1
    finally:
        release.set()


@pytest.mark.asyncio
async def test_saved_handoff_rejects_changed_intent_and_runner_reuse(
    startup, tmp_path, monkeypatch
):
    from omnigent.preference_startup import PreferenceLaunch, write_launch_snapshot
    from omnigent.runner.session_init_protocol import validate_runner_preferences

    store, connection = startup
    local = connection.workspace().client
    async with httpx.AsyncClient(base_url="http://test", transport=transport(store)) as http:
        await local.sync(http)
        local.enqueue(MODEL, "frozen")
        await local.sync(http)
    launch = await prepare_preferences(connection)
    snapshot = launch.model_dump(mode="json")
    env = await host_preference_environment(
        "http://test", "saved", prepared=True, snapshot=snapshot, owner_user_id="alice"
    )
    monkeypatch.setenv(PREFERENCE_SNAPSHOT_ENV, env[PREFERENCE_SNAPSHOT_ENV])
    validate_runner_preferences(snapshot)
    with pytest.raises(ValueError, match="different saved preferences"):
        validate_runner_preferences(None)
    altered = launch.model_dump(mode="json")
    altered["shared"]["entries"][0]["value"] = "changed"
    with pytest.raises(ValueError, match="different saved preferences"):
        validate_runner_preferences(altered)
    with pytest.raises(ValueError, match="saved choice"):
        write_launch_snapshot(
            Path(env[PREFERENCE_SNAPSHOT_ENV]), PreferenceLaunch.model_validate(altered)
        )
    with pytest.raises(ValueError, match="another account"):
        await host_preference_environment(
            "http://test", "saved", prepared=True, snapshot=snapshot, owner_user_id="bob"
        )
    for entries in (
        [snapshot["shared"]["entries"][0]] * 2,
        [{**snapshot["shared"]["entries"][0], "value": False}],
    ):
        with pytest.raises(ValueError):
            PreferenceLaunch.model_validate({**snapshot, "machine": entries})


@pytest.mark.asyncio
async def test_disposable_handoffs_outlive_the_legacy_limit_and_reap_only_dead_owners(
    startup, tmp_path, monkeypatch
):
    from omnigent.cli import _start_cli_runner_process, _stop_cli_runner_process
    from omnigent.native.owner_claim import read_owner_claim
    from omnigent.preference_runtime_files import prune_runtime_snapshots, write_runtime_snapshot
    from omnigent.preference_startup import write_launch_snapshot
    from tests._helpers.live_server import isolated_process_env
    from tests._helpers.polling import poll_until_async

    store, connection = startup
    async with httpx.AsyncClient(base_url="http://test", transport=transport(store)) as http:
        await connection.workspace().client.sync(http)
        connection.workspace().client.enqueue(MODEL, "saved")
        await connection.workspace().client.sync(http)
    launch = await prepare_preferences(connection)
    environment = isolated_process_env(tmp_path / "runtime-child", {}, base_env={})
    monkeypatch.setenv("OMNIGENT_DATA_DIR", environment["OMNIGENT_DATA_DIR"])
    monkeypatch.setattr("omnigent.preference_startup.MAX_SESSION_SNAPSHOTS", 1)
    legacy = Path(environment["OMNIGENT_DATA_DIR"]) / "preferences" / "sessions" / "old.json"
    write_launch_snapshot(legacy, launch)
    first = write_runtime_snapshot(launch)
    second = write_runtime_snapshot(launch)
    assert first != second and prune_runtime_snapshots() == 0
    monkeypatch.setenv(PREFERENCE_SNAPSHOT_ENV, str(first))
    runner = _start_cli_runner_process(
        server_url="http://127.0.0.1:1",
        capture_logs=True,
        log_dir=tmp_path / "logs",
        extra_env=environment,
    )
    try:
        handoffs = set(first.parent.parent.glob("launch-*/preferences.json")) - {first, second}
        assert len(handoffs) == 1
        handoff = handoffs.pop()
        assert json.loads(handoff.read_text()) == launch.model_dump(mode="json")
        await poll_until_async(
            lambda: (
                (claim := read_owner_claim(handoff.parent)) is not None
                and claim.pid == runner.proc.pid
            ),
            timeout_s=30,
            interval_s=0.1,
        )
        assert prune_runtime_snapshots() == 0
    finally:
        _stop_cli_runner_process(runner.proc, grace_timeout=1.0)
    assert prune_runtime_snapshots() == 1
    assert not handoff.exists() and first.exists() and second.exists() and legacy.exists()
    assert prune_runtime_snapshots() == 0
