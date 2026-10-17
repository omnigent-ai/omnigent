"""Live catalog rows belong to the actual launched Codex account and profile."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from omnigent.harnesses.codex_native import app_server as codex
from omnigent.harnesses.codex_native import bridge
from omnigent.models import model_catalog_store
from omnigent.runner import create_runner_app
from omnigent.runner.native import orchestration
from tests.runner.conftest import _FakeProcessManager, _runner_client, _ScriptedHarnessClient
from tests.runner.helpers import NullServerClient
from tests.runner.test_app_sessions_native_events_lifecycle import _RecordingCodexAppServerClient
from tests.runner.test_codex_model_pick_fallback import (
    _CODEX_PATH,
    _SESSION_ID,
    _LaunchHarness,
)
from tests.runner.test_codex_model_pick_fallback import (
    codex_launch_harness as codex_launch_harness,
)

_REAL_RESOLVE = codex.resolve_native_codex_launch


def _rows(
    prefix: str, *, hidden_default: bool = False, live: bool = False
) -> list[dict[str, Any]]:
    return [
        {
            "id": prefix + "-model",
            "model": prefix + "/model[large]",
            **({"isDefault": True} if not hidden_default else {}),
            "displayName": "Live model" if live else "Cached model",
            "defaultReasoningEffort": "high",
            "supportedReasoningEfforts": [{"reasoningEffort": "high", "description": "High"}],
        },
        {"id": prefix + "-session-pick", "model": prefix + "/session"},
    ]


async def _started_catalog(
    harness: _LaunchHarness,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    selected: bool,
    hidden_default: bool = False,
    source_auth: bool = False,
) -> tuple[Any, Path, dict[str, str], dict[str, list[dict[str, Any]]]]:
    source = tmp_path / "source-home"
    source.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(source))
    (source / "config.toml").write_text('model = "root-model"\n')
    (source / "work.config.toml").write_text('model = "work-model"\n')
    if source_auth:
        (source / "auth.json").write_text("{}")
        start = harness.app_server.start

        async def start_with_linked_auth() -> None:
            home = harness.app_server.codex_home
            home.mkdir(parents=True, exist_ok=True)
            codex._populate_codex_home_config(home, source, minimal_config=True)
            await start()

        monkeypatch.setattr(harness.app_server, "start", start_with_linked_auth)
    monkeypatch.setattr(codex, "resolve_native_codex_launch", _REAL_RESOLVE)
    monkeypatch.setattr(
        codex,
        "_resolve_native_codex_routing",
        lambda *, model, spec=None, terminal_launch_args=(): codex.NativeCodexLaunch(
            [], model, None
        ),
    )
    harness.snapshot.update(
        model_override=None,
        agent_id="catalog-agent",
        terminal_launch_args=["--profile", "work"] if selected else [],
    )
    fingerprints = {}
    catalogs = {}
    for profile in ("root", "work"):
        launch = _REAL_RESOLVE(
            model=None,
            spec=harness.agent_spec,
            terminal_launch_args=["--profile", "work"] if profile == "work" else [],
        )
        fingerprint = codex.codex_catalog_fingerprint(launch, codex_path=_CODEX_PATH)
        rows = _rows(profile, hidden_default=hidden_default)
        model_catalog_store.write_catalog("codex-native", fingerprint, rows)
        fingerprints[profile], catalogs[profile] = fingerprint, rows

    launched_view = await harness.launch()
    assert tuple(harness.builds[0]["terminal_launch_args"]) == tuple(
        harness.snapshot["terminal_launch_args"]
    )
    assert orchestration._AUTO_CODEX_APP_SERVERS[_SESSION_ID] is harness.app_server
    home = harness.app_server.codex_home
    home.mkdir(parents=True, exist_ok=True)
    profile = "work" if selected else "root"
    (home / "config.toml").write_text(f'model = "{profile}-session-pick"\n')
    directory = bridge.bridge_dir_for_bridge_id(_SESSION_ID)
    bridge.write_bridge_state(
        directory,
        bridge.CodexNativeBridgeState(
            session_id=_SESSION_ID,
            socket_path=harness.app_server.listen_url,
            thread_id="catalog-thread-a",
            codex_home=str(home),
        ),
    )

    async def resolve_spec(*_args: Any, **_kwargs: Any) -> Any:
        return harness.agent_spec

    async def adopt_existing(*_args: Any, **_kwargs: Any) -> Any:
        return launched_view

    monkeypatch.setattr(orchestration, "_auto_create_codex_terminal", adopt_existing)
    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=resolve_spec,
        server_client=NullServerClient(),
    )
    return app, directory, fingerprints, catalogs


async def _register_session(http: Any) -> None:
    response = await http.post(
        "/v1/sessions",
        json={
            "session_id": _SESSION_ID,
            "agent_id": "catalog-agent",
            "harness_override": "codex-native",
        },
    )
    assert response.status_code == 201, response.text


async def _finish_writebacks() -> None:
    await asyncio.gather(
        *[
            task
            for task in asyncio.all_tasks()
            if getattr(task.get_coro(), "__name__", "") == "_write_back_codex_catalog"
        ]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("selected", [False, True], ids=["root", "selected-profile"])
@pytest.mark.parametrize("hidden_default", [False, True], ids=["visible", "hidden-default"])
async def test_live_catalog_refresh_retains_its_actual_profile_and_shape_default(
    codex_launch_harness: _LaunchHarness,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    selected: bool,
    hidden_default: bool,
) -> None:
    harness = codex_launch_harness
    app, _, fingerprints, catalogs = await _started_catalog(
        harness, monkeypatch, tmp_path, selected=selected, hidden_default=hidden_default
    )
    profile = "work" if selected else "root"
    live = _rows(profile, live=True)
    client = _RecordingCodexAppServerClient(harness.app_server.listen_url, "catalog-test")
    client.model_list_responses = [{"result": {"data": live}}]
    monkeypatch.setattr(codex, "client_for_transport", lambda *_args, **_kwargs: client)
    async with _runner_client(app) as http:
        await _register_session(http)
        response = await http.get(f"/v1/sessions/{_SESSION_ID}/codex-model-options")
        assert response.status_code == 200, response.text
        assert response.json()["models"] == codex.mark_launch_default(
            live, profile + "-session-pick"
        )
        await _finish_writebacks()

    expected = _rows(profile, live=True, hidden_default=hidden_default)
    assert model_catalog_store.read_catalog("codex-native", fingerprints[profile]) == expected
    other = "root" if selected else "work"
    assert model_catalog_store.read_catalog("codex-native", fingerprints[other]) == catalogs[other]
    assert client.closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "replacement",
    ["backend-aba", "bridge-rebind", "receipt-aba", "source-config", "active-turn"],
)
async def test_late_catalog_reply_cannot_adopt_a_replacement_source(
    codex_launch_harness: _LaunchHarness,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    replacement: str,
) -> None:
    harness = codex_launch_harness
    app, directory, fingerprints, catalogs = await _started_catalog(
        harness, monkeypatch, tmp_path, selected=False
    )
    entered, release = asyncio.Event(), asyncio.Event()
    live = _rows("root", live=True)
    client = _RecordingCodexAppServerClient(harness.app_server.listen_url, "catalog-test")

    async def model_list(method: str, params: dict[str, Any]) -> dict[str, Any]:
        assert method == "model/list"
        entered.set()
        await release.wait()
        return {"result": {"data": live}}

    monkeypatch.setattr(client, "request", model_list)
    monkeypatch.setattr(codex, "client_for_transport", lambda *_args, **_kwargs: client)
    replacement_key = None
    replacement_rows = _rows("replacement")
    async with _runner_client(app) as http:
        await _register_session(http)
        request = asyncio.create_task(http.get(f"/v1/sessions/{_SESSION_ID}/codex-model-options"))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            if replacement == "backend-aba":
                # Coordinates are identical; the actual backend generation changed.
                newer = SimpleNamespace(**vars(harness.app_server))
                orchestration._AUTO_CODEX_APP_SERVERS[_SESSION_ID] = newer
            elif replacement == "bridge-rebind":
                state = bridge.read_bridge_state(directory)
                assert state is not None
                bridge.write_bridge_state(
                    directory, replace(state, session_id="rebound-session", thread_id="thread-b")
                )
            elif replacement == "receipt-aba":
                identity = harness.app_server.catalog_identity
                assert identity is not None
                harness.app_server.catalog_identity = replace(identity)
            elif replacement == "active-turn":
                state = bridge.read_bridge_state(directory)
                assert state is not None
                bridge.write_bridge_state(directory, replace(state, active_turn_id="new-turn"))
            else:
                source = tmp_path / "source-home"
                (source / "config.toml").write_text('model = "replacement-model"\n')
                launch = _REAL_RESOLVE(model=None, spec=harness.agent_spec)
                replacement_key = codex.codex_catalog_fingerprint(launch, codex_path=_CODEX_PATH)
                assert replacement_key != fingerprints["root"]
                model_catalog_store.write_catalog(
                    "codex-native", replacement_key, replacement_rows
                )
            release.set()
            response = await asyncio.wait_for(request, 10)
            await _finish_writebacks()
            if replacement == "active-turn":
                assert response.status_code == 200, response.text
                assert (
                    model_catalog_store.read_catalog("codex-native", fingerprints["root"]) == live
                )
            elif replacement_key is not None:
                assert response.status_code == 200, response.text
                assert (
                    model_catalog_store.read_catalog("codex-native", replacement_key)
                    == replacement_rows
                )
                assert (
                    model_catalog_store.read_catalog("codex-native", fingerprints["root"])
                    == catalogs["root"]
                )
            else:
                assert response.status_code == 503, response.text
                assert (
                    model_catalog_store.read_catalog("codex-native", fingerprints["root"])
                    == catalogs["root"]
                )
            assert (
                model_catalog_store.read_catalog("codex-native", fingerprints["work"])
                == catalogs["work"]
            )
        finally:
            release.set()
            await asyncio.gather(request, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provenance", ["cli-owned", "unknown-receipt", "missing-catalog", "known-empty"]
)
async def test_live_options_survive_conservative_shared_writeback_skips(
    codex_launch_harness: _LaunchHarness,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    provenance: str,
) -> None:
    harness = codex_launch_harness
    app, _, fingerprints, catalogs = await _started_catalog(
        harness, monkeypatch, tmp_path, selected=True
    )
    if provenance == "cli-owned":
        orchestration._AUTO_CODEX_APP_SERVERS.pop(_SESSION_ID)
    elif provenance == "unknown-receipt":
        harness.app_server.catalog_identity = None
    elif provenance == "missing-catalog":
        model_catalog_store.catalog_path("codex-native", fingerprints["work"]).unlink()
    else:
        model_catalog_store.write_catalog("codex-native", fingerprints["work"], [])
    live = _rows("work", live=True)
    client = _RecordingCodexAppServerClient(harness.app_server.listen_url, "catalog-test")
    client.model_list_responses = [{"result": {"data": live}}]
    monkeypatch.setattr(codex, "client_for_transport", lambda *_args, **_kwargs: client)
    async with _runner_client(app) as http:
        await _register_session(http)
        response = await http.get(f"/v1/sessions/{_SESSION_ID}/codex-model-options")
        assert response.status_code == 200, response.text
        assert response.json()["models"] == codex.mark_launch_default(live, "work-session-pick")
        await _finish_writebacks()
    stored = model_catalog_store.read_catalog("codex-native", fingerprints["work"])
    if provenance == "known-empty":
        assert stored == _rows("work", live=True, hidden_default=True)
        assert all(row.get("isDefault") is not True for row in stored)
    else:
        assert stored == (None if provenance == "missing-catalog" else catalogs["work"])
    assert (
        model_catalog_store.read_catalog("codex-native", fingerprints["root"]) == catalogs["root"]
    )
    assert client.closed


@pytest.mark.asyncio
async def test_empty_live_refresh_keeps_the_existing_useful_catalog(
    codex_launch_harness: _LaunchHarness,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    harness = codex_launch_harness
    app, _, fingerprints, catalogs = await _started_catalog(
        harness, monkeypatch, tmp_path, selected=False
    )
    client = _RecordingCodexAppServerClient(harness.app_server.listen_url, "catalog-test")
    client.model_list_responses = [{"result": {"data": []}}]
    monkeypatch.setattr(codex, "client_for_transport", lambda *_args, **_kwargs: client)
    async with _runner_client(app) as http:
        await _register_session(http)
        response = await http.get(f"/v1/sessions/{_SESSION_ID}/codex-model-options")
        assert response.status_code == 200, response.text
        assert response.json() == {"models": []}
        await _finish_writebacks()
    assert (
        model_catalog_store.read_catalog("codex-native", fingerprints["root"]) == catalogs["root"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_file", ["config.toml", "work.config.toml", "auth.json"])
async def test_source_drift_during_actual_startup_disables_shared_writeback(
    codex_launch_harness: _LaunchHarness,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    changed_file: str,
) -> None:
    harness = codex_launch_harness

    async def start_with_source_drift() -> None:
        path = tmp_path / "source-home" / changed_file
        path.write_text("{}" if changed_file == "auth.json" else 'model = "replacement-model"\n')

    monkeypatch.setattr(harness.app_server, "start", start_with_source_drift)
    app, _, fingerprints, catalogs = await _started_catalog(
        harness, monkeypatch, tmp_path, selected=True
    )
    assert harness.app_server.catalog_identity is None
    launch = _REAL_RESOLVE(
        model=None, spec=harness.agent_spec, terminal_launch_args=["--profile", "work"]
    )
    newer_key = codex.codex_catalog_fingerprint(launch, codex_path=_CODEX_PATH)
    assert newer_key != fingerprints["work"]
    newer_catalog = _rows("newer")
    model_catalog_store.write_catalog("codex-native", newer_key, newer_catalog)
    live = _rows("work", live=True)
    client = _RecordingCodexAppServerClient(harness.app_server.listen_url, "catalog-test")
    client.model_list_responses = [{"result": {"data": live}}]
    monkeypatch.setattr(codex, "client_for_transport", lambda *_args, **_kwargs: client)
    async with _runner_client(app) as http:
        await _register_session(http)
        response = await http.get(f"/v1/sessions/{_SESSION_ID}/codex-model-options")
        assert response.status_code == 200, response.text
        assert response.json()["models"] == codex.mark_launch_default(live, "work-session-pick")
        await _finish_writebacks()
    assert model_catalog_store.read_catalog("codex-native", newer_key) == newer_catalog
    assert (
        model_catalog_store.read_catalog("codex-native", fingerprints["work"]) == catalogs["work"]
    )


@pytest.mark.asyncio
async def test_backend_replacement_during_private_config_read_rejects_the_old_reply(
    codex_launch_harness: _LaunchHarness,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import threading

    harness = codex_launch_harness
    app, _, fingerprints, catalogs = await _started_catalog(
        harness, monkeypatch, tmp_path, selected=False
    )
    entered, release = threading.Event(), threading.Event()
    read_model = bridge.read_codex_home_config_model

    def held_read(path: Path) -> str | None:
        entered.set()
        assert release.wait(10)
        return read_model(path)

    monkeypatch.setattr(bridge, "read_codex_home_config_model", held_read)
    client = _RecordingCodexAppServerClient(harness.app_server.listen_url, "catalog-test")
    client.model_list_responses = [{"result": {"data": _rows("root", live=True)}}]
    monkeypatch.setattr(codex, "client_for_transport", lambda *_args, **_kwargs: client)
    async with _runner_client(app) as http:
        await _register_session(http)
        request = asyncio.create_task(http.get(f"/v1/sessions/{_SESSION_ID}/codex-model-options"))
        try:
            assert await asyncio.to_thread(entered.wait, 10)
            orchestration._AUTO_CODEX_APP_SERVERS[_SESSION_ID] = SimpleNamespace(
                **vars(harness.app_server)
            )
            release.set()
            response = await asyncio.wait_for(request, 10)
            assert response.status_code == 503, response.text
            await _finish_writebacks()
            assert (
                model_catalog_store.read_catalog("codex-native", fingerprints["root"])
                == catalogs["root"]
            )
        finally:
            release.set()
            await asyncio.gather(request, return_exceptions=True)


@pytest.mark.asyncio
async def test_unknown_provenance_live_options_need_no_writable_bridge_lock(
    codex_launch_harness: _LaunchHarness,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    harness = codex_launch_harness
    app, directory, fingerprints, catalogs = await _started_catalog(
        harness, monkeypatch, tmp_path, selected=False
    )
    harness.app_server.catalog_identity = None
    live = _rows("root", live=True)
    client = _RecordingCodexAppServerClient(harness.app_server.listen_url, "catalog-test")
    client.model_list_responses = [{"result": {"data": live}}]
    monkeypatch.setattr(codex, "client_for_transport", lambda *_args, **_kwargs: client)
    lock_file = directory / "state.lock"
    async with _runner_client(app) as http:
        await _register_session(http)
        lock_file.chmod(0o400)
        try:
            response = await http.get(f"/v1/sessions/{_SESSION_ID}/codex-model-options")
            assert response.status_code == 200, response.text
            assert response.json()["models"] == codex.mark_launch_default(
                live, "root-session-pick"
            )
            await _finish_writebacks()
        finally:
            lock_file.chmod(0o600)
    assert (
        model_catalog_store.read_catalog("codex-native", fingerprints["root"]) == catalogs["root"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("timing", ["startup", "inflight"])
async def test_private_auth_without_original_source_provenance_never_updates_shared_cache(
    codex_launch_harness: _LaunchHarness,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    timing: str,
) -> None:
    harness = codex_launch_harness

    def write_private_auth() -> None:
        home = harness.app_server.codex_home
        home.mkdir(parents=True, exist_ok=True)
        (home / "auth.json").write_text("{}")

    if timing == "startup":

        async def start_with_private_auth() -> None:
            write_private_auth()

        monkeypatch.setattr(harness.app_server, "start", start_with_private_auth)
    app, _, fingerprints, catalogs = await _started_catalog(
        harness, monkeypatch, tmp_path, selected=False
    )
    if timing == "startup":
        assert harness.app_server.catalog_identity is None
    entered, release = asyncio.Event(), asyncio.Event()
    live = _rows("root", live=True)
    client = _RecordingCodexAppServerClient(harness.app_server.listen_url, "catalog-test")

    async def model_list(method: str, params: dict[str, Any]) -> dict[str, Any]:
        assert method == "model/list"
        entered.set()
        await release.wait()
        return {"result": {"data": live}}

    monkeypatch.setattr(client, "request", model_list)
    monkeypatch.setattr(codex, "client_for_transport", lambda *_args, **_kwargs: client)
    async with _runner_client(app) as http:
        await _register_session(http)
        request = asyncio.create_task(http.get(f"/v1/sessions/{_SESSION_ID}/codex-model-options"))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            if timing == "inflight":
                write_private_auth()
            release.set()
            response = await asyncio.wait_for(request, 10)
            assert response.status_code == 200, response.text
            assert response.json()["models"] == codex.mark_launch_default(
                live, "root-session-pick"
            )
            await _finish_writebacks()
            assert (
                model_catalog_store.read_catalog("codex-native", fingerprints["root"])
                == catalogs["root"]
            )
        finally:
            release.set()
            await asyncio.gather(request, return_exceptions=True)


@pytest.mark.asyncio
async def test_linked_private_login_retains_known_catalog_writeback(
    codex_launch_harness: _LaunchHarness,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    harness = codex_launch_harness
    app, _, fingerprints, _ = await _started_catalog(
        harness, monkeypatch, tmp_path, selected=True, source_auth=True
    )
    assert (harness.app_server.codex_home / "auth.json").is_symlink()
    assert harness.app_server.catalog_identity is not None
    live = _rows("work", live=True)
    client = _RecordingCodexAppServerClient(harness.app_server.listen_url, "catalog-test")
    client.model_list_responses = [{"result": {"data": live}}]
    monkeypatch.setattr(codex, "client_for_transport", lambda *_args, **_kwargs: client)
    async with _runner_client(app) as http:
        await _register_session(http)
        response = await http.get(f"/v1/sessions/{_SESSION_ID}/codex-model-options")
        assert response.status_code == 200, response.text
        assert response.json()["models"] == codex.mark_launch_default(live, "work-session-pick")
        await _finish_writebacks()
    assert model_catalog_store.read_catalog("codex-native", fingerprints["work"]) == live


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", [False, True], ids=["same-route", "fallback-route-changed"])
async def test_model_fallback_cannot_rebind_the_original_catalog_receipt(
    codex_launch_harness: _LaunchHarness,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    drift: bool,
) -> None:
    import threading

    harness = codex_launch_harness
    keys = {}
    cached = {"a": _rows("root"), "b": _rows("other")}
    original_launch = harness.launch
    main_thread = threading.current_thread()
    actual_calls = 0
    launching = False

    def route(*, model: str | None, **_kwargs: Any) -> codex.NativeCodexLaunch:
        nonlocal actual_calls
        if launching and threading.current_thread() is main_thread:
            actual_calls += 1
        provider = "b" if drift and actual_calls >= 2 else "a"
        return codex.NativeCodexLaunch(
            config_overrides=[f'model_provider="provider-{provider}"'],
            model=model or f"{provider}-shape-default",
            profile=None,
        )

    async def launch_with_changed_fallback() -> Any:
        nonlocal launching
        monkeypatch.setattr(codex, "_resolve_native_codex_routing", route)
        for provider in ("a", "b"):
            launch = codex.NativeCodexLaunch(
                [f'model_provider="provider-{provider}"'], f"{provider}-shape-default", None
            )
            keys[provider] = codex.codex_catalog_fingerprint(launch, codex_path=_CODEX_PATH)
            model_catalog_store.write_catalog("codex-native", keys[provider], cached[provider])
        harness.snapshot["model_override"] = "retired-picker"
        launching = True
        return await original_launch()

    monkeypatch.setattr(harness, "launch", launch_with_changed_fallback)
    app, _, _, _ = await _started_catalog(harness, monkeypatch, tmp_path, selected=False)
    provider = "b" if drift else "a"
    assert actual_calls == 2
    assert f'model_provider="provider-{provider}"' in harness.builds[0]["extra_config_overrides"]
    live = _rows("other" if drift else "root", live=True)
    client = _RecordingCodexAppServerClient(harness.app_server.listen_url, "catalog-test")
    client.model_list_responses = [{"result": {"data": live}}]
    monkeypatch.setattr(codex, "client_for_transport", lambda *_args, **_kwargs: client)
    async with _runner_client(app) as http:
        await _register_session(http)
        response = await http.get(f"/v1/sessions/{_SESSION_ID}/codex-model-options")
        assert response.status_code == 200, response.text
        assert response.json()["models"] == codex.mark_launch_default(live, "root-session-pick")
        await _finish_writebacks()
    assert model_catalog_store.read_catalog("codex-native", keys["b"]) == cached["b"]
    assert model_catalog_store.read_catalog("codex-native", keys["a"]) == (
        cached["a"] if drift else live
    )
