"""Model picks are checked against the catalog of the actual Codex launch."""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from omnigent import codex_native_app_server as codex_app
from omnigent import codex_native_bridge, model_catalog_store
from omnigent.entities.session_resources import SessionResourceView
from omnigent.runner.app import ResolvedSpec
from omnigent.runner.native import orchestration as runner_native
from omnigent.spec.types import AgentSpec, ExecutorSpec
from tests.runner.conftest import (
    REAL_CODEX_LAUNCH_CATALOG,
    REAL_CODEX_REPROBED_LAUNCH_CATALOG,
)

_CODEX_PATH = "/missing-test-bin/codex"
_SESSION_ID = "c91a9d508b344ad59c65628ed80b5230"
_PROVIDER_DEFAULT = "gpt-5.6-terra"
_RETIRED_PICK = "gpt-5.4-retired"


@dataclass
class _LaunchHarness:
    """A real runner launch with scripted provider configuration and processes."""

    agent_spec: AgentSpec | ResolvedSpec
    snapshot: dict[str, Any]
    server_client: httpx.AsyncClient
    app_server: SimpleNamespace
    event_client: SimpleNamespace
    registry: SimpleNamespace
    events: list[str]
    builds: list[dict[str, Any]]
    resets: list[dict[str, Any]]
    probe: AsyncMock
    response_status: dict[str, int]

    async def launch(self) -> SessionResourceView:
        """Run the production launch path, including snapshot and catalog reads."""
        return await runner_native._auto_create_codex_terminal(
            _SESSION_ID,
            self.registry,  # type: ignore[arg-type]
            lambda _sid, _event: None,
            agent_spec=self.agent_spec,
            server_client=self.server_client,
        )

    def catalog_shape(self, *, ambient: bool = False) -> codex_app.NativeCodexLaunch:
        """Resolve the catalog shape without a per-session model selection."""
        spec = (
            self.agent_spec.spec if isinstance(self.agent_spec, ResolvedSpec) else self.agent_spec
        )
        return codex_app.resolve_native_codex_launch(model=None, spec=None if ambient else spec)

    def seed_catalog(
        self,
        rows: list[dict[str, Any]],
        *,
        stale: bool = False,
        ambient: bool = False,
    ) -> str:
        """Seed a real store entry, optionally older than its freshness budget."""
        fingerprint = codex_app.codex_catalog_fingerprint(
            self.catalog_shape(ambient=ambient), codex_path=_CODEX_PATH
        )
        model_catalog_store.write_catalog("codex-native", fingerprint, rows)
        if stale:
            old = time.time() - model_catalog_store.CATALOG_STALE_AFTER_S - 60
            os.utime(model_catalog_store.catalog_path("codex-native", fingerprint), (old, old))
        return fingerprint


@pytest.fixture
async def codex_launch_harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[_LaunchHarness]:
    """Keep the real catalog store and launcher isolated from all live CLIs."""
    monkeypatch.setattr(codex_native_bridge, "_BRIDGE_ROOT", tmp_path / "codex-bridge")
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://runner.test")
    monkeypatch.setattr("omnigent.config.load_effective_config", dict)
    monkeypatch.setattr("omnigent.runner._entry._make_auth_token_factory", lambda: None)
    monkeypatch.setattr("omnigent.inner.codex_executor._find_codex_cli", lambda: _CODEX_PATH)
    monkeypatch.setattr(codex_app, "_find_codex_cli", lambda: _CODEX_PATH)
    monkeypatch.setattr(
        "omnigent.codex_native_process_registry.reap_codex_native_processes_for_state_dir",
        lambda _path: None,
    )

    def resolve_launch(
        *, model: str | None, spec: AgentSpec | None = None
    ) -> codex_app.NativeCodexLaunch:
        return codex_app.NativeCodexLaunch(
            config_overrides=[],
            model=model or _PROVIDER_DEFAULT,
            profile="session-provider" if spec is not None else "ambient-provider",
        )

    monkeypatch.setattr(codex_app, "resolve_native_codex_launch", resolve_launch)
    monkeypatch.setattr(codex_app, "codex_launch_catalog", REAL_CODEX_LAUNCH_CATALOG)
    monkeypatch.setattr(
        codex_app, "codex_reprobed_launch_catalog", REAL_CODEX_REPROBED_LAUNCH_CATALOG
    )
    probe = AsyncMock(return_value=[])
    monkeypatch.setattr(codex_app, "probe_codex_model_options", probe)
    events: list[str] = []
    builds: list[dict[str, Any]] = []
    resets: list[dict[str, Any]] = []
    snapshot: dict[str, Any] = {"model_override": _RETIRED_PICK}
    response_status = {"reset": 200}
    initial_probes = set(model_catalog_store._inflight.values())

    def request(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            assert request.url.path == f"/v1/sessions/{_SESSION_ID}"
            return httpx.Response(200, json=snapshot)
        assert request.method == "POST", "fallback must never unconditionally PATCH the pick"
        assert request.url.path == f"/v1/sessions/{_SESSION_ID}/model-override/reset"
        reset = json.loads(request.content)
        assert set(reset) == {"expected_model_override"}
        events.append("reset")
        resets.append(reset)
        if response_status["reset"] >= 400:
            return httpx.Response(response_status["reset"], json={"error": "reset unavailable"})
        applied = snapshot["model_override"] == reset["expected_model_override"]
        if applied:
            snapshot["model_override"] = None
        return httpx.Response(200, json={"reset": applied})

    app_server = SimpleNamespace(
        codex_path=_CODEX_PATH,
        codex_cli_version=(0, 145, 0),
        codex_home=tmp_path / "unused-home",
        env={"OPENAI_API_KEY": "test-key"},
        config_overrides=[],
        listen_url=None,
        start=AsyncMock(side_effect=lambda: events.append("app-server-start")),
        close=AsyncMock(),
    )

    def build_server(**kwargs: Any) -> SimpleNamespace:
        builds.append(kwargs)
        app_server.codex_home = kwargs["codex_home"]
        # The real builder writes provider definitions into the private config.
        app_server.config_overrides = [
            override
            for override in kwargs["extra_config_overrides"]
            if not override.startswith("model_providers.")
        ]
        return app_server

    async def launch_terminal(**kwargs: Any) -> SessionResourceView:
        events.append("terminal-launch")
        return SessionResourceView(
            id="terminal_codex_main",
            type="terminal",
            session_id=kwargs["session_id"],
            name="Codex",
        )

    event_client = SimpleNamespace(connect=AsyncMock(), close=AsyncMock())
    forwarder_release = asyncio.Event()

    async def forwarder(**_kwargs: Any) -> None:
        await forwarder_release.wait()

    monkeypatch.setattr(codex_app, "build_codex_native_server", build_server)
    monkeypatch.setattr(codex_app, "CodexAppServerClient", lambda **_kwargs: event_client)
    monkeypatch.setattr(runner_native, "_codex_discover_thread_and_forward", forwarder)
    registry = SimpleNamespace(launch_auxiliary_terminal=AsyncMock(side_effect=launch_terminal))
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(request), base_url="http://runner.test"
    ) as server_client:
        yield _LaunchHarness(
            agent_spec=AgentSpec(
                spec_version=1,
                name="codex",
                executor=ExecutorSpec(config={"harness": "codex-native"}),
            ),
            snapshot=snapshot,
            server_client=server_client,
            app_server=app_server,
            event_client=event_client,
            registry=registry,
            events=events,
            builds=builds,
            resets=resets,
            probe=probe,
            response_status=response_status,
        )
    await runner_native._cancel_auto_forwarder_task(_SESSION_ID)
    runner_native._AUTO_CODEX_APP_SERVERS.pop(_SESSION_ID, None)
    pending_probes = set(model_catalog_store._inflight.values()) - initial_probes
    for task in pending_probes:
        task.cancel()
    await asyncio.gather(*pending_probes, return_exceptions=True)
    await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize("pick", ["databricks-gpt-5-6-sol", "system.ai.gpt-5-6-sol"])
async def test_equivalent_gateway_pick_launches_without_reset(
    pick: str, codex_launch_harness: _LaunchHarness
) -> None:
    """Gateway prefixes and endpoint punctuation do not make a pick unavailable."""
    harness = codex_launch_harness
    harness.snapshot["model_override"] = pick
    harness.seed_catalog([{"id": "gpt-5.6-sol", "model": "gpt-5.6-sol", "isDefault": True}])

    await harness.launch()

    assert harness.builds[0]["model"] == pick
    assert harness.resets == []
    harness.probe.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_model", [None, "gpt-5.6-astra"])
async def test_unavailable_pick_resets_only_after_fallback_terminal_launch(
    agent_model: str | None,
    codex_launch_harness: _LaunchHarness,
) -> None:
    """Fresh evidence retires a pick only after its replacement terminal exists."""
    harness = codex_launch_harness
    assert isinstance(harness.agent_spec, AgentSpec)
    if agent_model is not None:
        harness.agent_spec.executor.config["model"] = agent_model
    harness.seed_catalog(
        [
            {"id": "gpt-5.6-sol", "isDefault": True},
            {"id": _PROVIDER_DEFAULT},
            {"id": "gpt-5.6-astra"},
        ]
    )

    await harness.launch()

    assert harness.builds[0]["model"] == (agent_model or _PROVIDER_DEFAULT)
    assert harness.resets == [{"expected_model_override": _RETIRED_PICK}]
    assert harness.snapshot["model_override"] is None
    assert harness.events.index("terminal-launch") < harness.events.index("reset")


@pytest.mark.asyncio
@pytest.mark.parametrize("refresh", ["hit", "miss", "empty", "error"])
async def test_stale_miss_awaits_reprobe_before_deciding_fallback(
    refresh: str, codex_launch_harness: _LaunchHarness
) -> None:
    """Only fresh contrary evidence resets a pick; failed refreshes preserve it."""
    harness = codex_launch_harness
    pick = "databricks-gpt-5-6-sol"
    harness.snapshot["model_override"] = pick
    stale_rows = [{"id": "gpt-5.4-yesterday", "isDefault": True}]
    fingerprint = harness.seed_catalog(stale_rows, stale=True)
    fresh_rows = [{"id": _PROVIDER_DEFAULT, "isDefault": True}]
    if refresh == "hit":
        fresh_rows.append({"id": "gpt-5.6-sol"})
    probe_started = asyncio.Event()
    release_probe = asyncio.Event()

    async def reprobe(**kwargs: Any) -> list[dict[str, Any]]:
        assert kwargs == {"codex_path": _CODEX_PATH, "launch": harness.catalog_shape()}
        probe_started.set()
        await release_probe.wait()
        if refresh == "error":
            raise RuntimeError("catalog temporarily unavailable")
        return [] if refresh == "empty" else fresh_rows

    harness.probe.side_effect = reprobe
    launch = asyncio.create_task(harness.launch())
    try:
        await asyncio.wait_for(probe_started.wait(), timeout=2.0)
        assert not launch.done(), "the launch must join the stale catalog's pending re-probe"
        assert harness.builds == []
        assert harness.resets == []
    finally:
        release_probe.set()
        await launch

    assert harness.builds[0]["model"] == (pick if refresh == "hit" else _PROVIDER_DEFAULT)
    expected_resets = [{"expected_model_override": pick}] if refresh == "miss" else []
    assert harness.resets == expected_resets
    harness.probe.assert_awaited_once()
    assert model_catalog_store.read_catalog("codex-native", fingerprint) == (
        fresh_rows if refresh in {"hit", "miss"} else stale_rows
    )


@pytest.mark.asyncio
async def test_stale_catalog_hit_does_not_wait_for_background_refresh(
    codex_launch_harness: _LaunchHarness,
) -> None:
    """A still-served pick does not need to wait for an unrelated catalog refresh."""
    harness = codex_launch_harness
    harness.seed_catalog([{"id": harness.snapshot["model_override"]}], stale=True)
    release_probe = asyncio.Event()

    async def reprobe(**_kwargs: Any) -> list[dict[str, Any]]:
        await release_probe.wait()
        return [{"id": _PROVIDER_DEFAULT, "isDefault": True}]

    harness.probe.side_effect = reprobe
    try:
        await asyncio.wait_for(harness.launch(), timeout=2.0)
        assert harness.builds[0]["model"] == harness.snapshot["model_override"]
        assert harness.resets == []
    finally:
        release_probe.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("probe_fails", [False, True])
async def test_no_catalog_keeps_explicit_pick(
    probe_fails: bool, codex_launch_harness: _LaunchHarness
) -> None:
    """An absent catalog is not evidence that a requested model is unavailable."""
    harness = codex_launch_harness
    if probe_fails:
        harness.probe.side_effect = RuntimeError("catalog temporarily unavailable")

    await harness.launch()

    assert harness.builds[0]["model"] == harness.snapshot["model_override"]
    assert harness.resets == []
    harness.probe.assert_awaited_once_with(codex_path=_CODEX_PATH, launch=harness.catalog_shape())


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["app-server", "discovery", "terminal"])
async def test_failed_fallback_launch_does_not_reset_pick(
    failure: str, codex_launch_harness: _LaunchHarness
) -> None:
    """Starting the fallback is not sufficient to retire the persisted selection."""
    harness = codex_launch_harness
    harness.seed_catalog([{"id": _PROVIDER_DEFAULT, "isDefault": True}])
    failing_call = {
        "app-server": harness.app_server.start,
        "discovery": harness.event_client.connect,
        "terminal": harness.registry.launch_auxiliary_terminal,
    }[failure]
    failing_call.side_effect = RuntimeError(f"{failure} launch failed")

    with pytest.raises(RuntimeError, match=f"{failure} launch failed"):
        await harness.launch()

    assert harness.builds[0]["model"] == _PROVIDER_DEFAULT
    assert harness.resets == []
    if failure != "app-server":
        harness.app_server.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapped_spec", [False, True])
@pytest.mark.parametrize("stale", [False, True])
@pytest.mark.parametrize("spec_serves_pick", [False, True])
async def test_spec_catalog_is_not_replaced_by_ambient_provider_catalog(
    wrapped_spec: bool,
    stale: bool,
    spec_serves_pick: bool,
    codex_launch_harness: _LaunchHarness,
    tmp_path: Path,
) -> None:
    """The agent's provider controls catalog lookup, freshness, and re-probing."""
    harness = codex_launch_harness
    if wrapped_spec:
        assert isinstance(harness.agent_spec, AgentSpec)
        harness.agent_spec = ResolvedSpec(spec=harness.agent_spec, workdir=tmp_path)
    pick = harness.snapshot["model_override"]
    ambient_model = _PROVIDER_DEFAULT if spec_serves_pick else pick
    harness.seed_catalog([{"id": ambient_model, "isDefault": True}], ambient=True)
    session_rows = [{"id": _PROVIDER_DEFAULT, "isDefault": True}]
    if spec_serves_pick:
        session_rows.append({"id": pick})
    fingerprint = harness.seed_catalog(session_rows, stale=stale)
    harness.probe.return_value = session_rows

    await harness.launch()

    assert harness.builds[0]["model"] == (pick if spec_serves_pick else _PROVIDER_DEFAULT)
    assert harness.builds[0]["profile"] == "session-provider"
    assert harness.resets == ([] if spec_serves_pick else [{"expected_model_override": pick}])
    if stale:
        task = model_catalog_store._inflight.get(("codex-native", fingerprint))
        if task is not None:
            await task
        harness.probe.assert_awaited_once_with(
            codex_path=_CODEX_PATH, launch=harness.catalog_shape()
        )
    else:
        harness.probe.assert_not_awaited()


@pytest.mark.asyncio
async def test_generic_provider_fallback_rebuilds_model_config_overrides(
    codex_launch_harness: _LaunchHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fallback replaces the resolved launch, including its embedded model config."""
    harness = codex_launch_harness
    pick = harness.snapshot["model_override"]

    def resolve_launch(
        *, model: str | None, spec: AgentSpec | None = None
    ) -> codex_app.NativeCodexLaunch:
        del spec
        model = model or _PROVIDER_DEFAULT
        return codex_app.NativeCodexLaunch(
            config_overrides=[
                'model_provider="configured-provider"',
                'model_providers.configured-provider.base_url="https://provider.test/v1"',
                f"model={json.dumps(model)}",
            ],
            model=model,
            profile=None,
        )

    monkeypatch.setattr(codex_app, "resolve_native_codex_launch", resolve_launch)
    harness.seed_catalog([{"id": _PROVIDER_DEFAULT, "isDefault": True}])

    await harness.launch()

    assert harness.builds[0]["model"] == _PROVIDER_DEFAULT
    assert f'model="{_PROVIDER_DEFAULT}"' in harness.builds[0]["extra_config_overrides"]
    assert all(pick not in override for override in harness.builds[0]["extra_config_overrides"])
    assert harness.resets == [{"expected_model_override": pick}]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [404, 500])
async def test_failed_pick_reset_keeps_successfully_launched_terminal(
    status: int,
    codex_launch_harness: _LaunchHarness,
) -> None:
    """An old or failing server keeps the terminal and pick; never retry with PATCH."""
    harness = codex_launch_harness
    harness.seed_catalog([{"id": _PROVIDER_DEFAULT, "isDefault": True}])
    harness.response_status["reset"] = status

    resource = await harness.launch()

    assert resource.id == "terminal_codex_main"
    assert harness.resets == [{"expected_model_override": _RETIRED_PICK}]
    assert harness.snapshot["model_override"] == _RETIRED_PICK
    assert harness.builds[0]["model"] == _PROVIDER_DEFAULT
    await asyncio.sleep(0)
    forwarder = runner_native._AUTO_FORWARDER_TASKS[_SESSION_ID]
    assert not forwarder.done()
    harness.app_server.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_closed_reset_client_keeps_successfully_launched_terminal(
    codex_launch_harness: _LaunchHarness,
) -> None:
    """A client closed during terminal startup cannot turn bookkeeping into launch failure."""
    harness = codex_launch_harness
    harness.seed_catalog([{"id": _PROVIDER_DEFAULT, "isDefault": True}])
    launch_terminal = harness.registry.launch_auxiliary_terminal.side_effect

    async def launch_and_close_client(**kwargs: Any) -> SessionResourceView:
        resource = await launch_terminal(**kwargs)
        await harness.server_client.aclose()
        return resource

    harness.registry.launch_auxiliary_terminal.side_effect = launch_and_close_client

    resource = await harness.launch()

    assert resource.id == "terminal_codex_main"
    assert harness.resets == []
    assert harness.snapshot["model_override"] == _RETIRED_PICK
    assert harness.builds[0]["model"] == _PROVIDER_DEFAULT
    await asyncio.sleep(0)
    assert not runner_native._AUTO_FORWARDER_TASKS[_SESSION_ID].done()
    harness.app_server.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_conditional_reset_still_propagates_cancellation(
    codex_launch_harness: _LaunchHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Best-effort metadata handling must not swallow cancellation of its caller."""
    harness = codex_launch_harness
    monkeypatch.setattr(
        harness.server_client, "post", AsyncMock(side_effect=asyncio.CancelledError)
    )

    with pytest.raises(asyncio.CancelledError):
        await runner_native._clear_session_model_override(
            _SESSION_ID, harness.server_client, expected_model_override=_RETIRED_PICK
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("stale", [False, True])
async def test_subscription_fallback_pins_only_fresh_account_default(
    stale: bool,
    codex_launch_harness: _LaunchHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fallback on native login never turns yesterday's default into a model pin."""
    harness = codex_launch_harness

    def resolve_launch(
        *, model: str | None, spec: AgentSpec | None = None
    ) -> codex_app.NativeCodexLaunch:
        del spec
        return codex_app.NativeCodexLaunch(
            config_overrides=['model_provider="openai"'], model=model, profile=None
        )

    monkeypatch.setattr(codex_app, "resolve_native_codex_launch", resolve_launch)
    harness.seed_catalog([{"id": "gpt-5.6-sol", "isDefault": True}], stale=stale)
    harness.probe.side_effect = RuntimeError("catalog temporarily unavailable")

    await harness.launch()

    assert harness.builds[0]["model"] == (None if stale else "gpt-5.6-sol")
    assert harness.builds[0]["profile"] is None
    assert 'model_provider="openai"' in harness.builds[0]["extra_config_overrides"]
    assert harness.resets == ([] if stale else [{"expected_model_override": _RETIRED_PICK}])


@pytest.mark.asyncio
@pytest.mark.parametrize("preload_fails", [False, True])
async def test_resumed_fallback_resets_pick_only_if_preload_and_terminal_succeed(
    preload_fails: bool,
    codex_launch_harness: _LaunchHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed known-thread preload must not discard the selection before launch."""
    harness = codex_launch_harness
    harness.snapshot["external_session_id"] = "019e96aa-0be2-7343-8d3b-6f914d60936c"
    harness.seed_catalog([{"id": _PROVIDER_DEFAULT, "isDefault": True}])
    preload = AsyncMock(side_effect=RuntimeError("preload unavailable") if preload_fails else None)
    monkeypatch.setattr("omnigent.codex_native._ensure_local_codex_resume_rollout", AsyncMock())
    monkeypatch.setattr(codex_app, "preload_codex_thread_for_resume", preload)
    monkeypatch.setattr(runner_native, "_codex_forward_known_thread", AsyncMock())

    if preload_fails:
        with pytest.raises(RuntimeError, match="preload unavailable"):
            await harness.launch()
        harness.app_server.close.assert_awaited_once()
        harness.registry.launch_auxiliary_terminal.assert_not_awaited()
    else:
        await harness.launch()
        harness.registry.launch_auxiliary_terminal.assert_awaited_once()

    preload.assert_awaited_once()
    assert harness.builds[0]["model"] == _PROVIDER_DEFAULT
    assert harness.resets == (
        [] if preload_fails else [{"expected_model_override": _RETIRED_PICK}]
    )


@pytest.mark.asyncio
async def test_first_message_does_not_reintroduce_retired_prelaunch_override(
    codex_launch_harness: _LaunchHarness,
) -> None:
    """An ordinary queued message keeps the working fallback even with an old snapshot."""
    from omnigent._wrapper_labels import CODEX_NATIVE_WRAPPER_VALUE, WRAPPER_LABEL_KEY
    from omnigent.entities.conversation import Conversation
    from omnigent.runtime.harnesses._scaffold import MessageEvent
    from omnigent.server.routes._sessions.orchestration import _build_native_terminal_message_event
    from omnigent.server.schemas import SessionEventInput

    harness = codex_launch_harness
    stale_snapshot = Conversation(
        id=_SESSION_ID,
        created_at=0,
        updated_at=0,
        root_conversation_id=_SESSION_ID,
        agent_id="test-codex-agent",
        labels={WRAPPER_LABEL_KEY: CODEX_NATIVE_WRAPPER_VALUE},
        model_override=harness.snapshot["model_override"],
    )
    harness.seed_catalog([{"id": _PROVIDER_DEFAULT, "isDefault": True}])

    await harness.launch()
    message = _build_native_terminal_message_event(
        stale_snapshot,
        SessionEventInput(
            type="message",
            data={"role": "user", "content": [{"type": "input_text", "text": "continue"}]},
        ),
    )
    request = MessageEvent.model_validate(message).to_create_request()

    assert harness.builds[0]["model"] == _PROVIDER_DEFAULT
    assert harness.resets == [{"expected_model_override": _RETIRED_PICK}]
    assert "model_override" not in message
    assert request.model_override is None


@pytest.mark.asyncio
@pytest.mark.parametrize("pause_at", ["reprobe", "terminal"])
async def test_newer_pick_survives_reset_after_slow_fallback_launch(
    pause_at: str, codex_launch_harness: _LaunchHarness
) -> None:
    """Fallback only retires its original rejected pick, never a newer user selection."""
    harness = codex_launch_harness
    new_pick = "gpt-5.6-sol"
    rows = [{"id": _PROVIDER_DEFAULT, "isDefault": True}, {"id": new_pick}]
    harness.seed_catalog(rows, stale=pause_at == "reprobe")
    paused = asyncio.Event()
    release = asyncio.Event()

    async def delayed_probe(**_kwargs: Any) -> list[dict[str, Any]]:
        paused.set()
        await release.wait()
        return rows

    original_terminal_launch = harness.registry.launch_auxiliary_terminal.side_effect

    async def delayed_terminal(**kwargs: Any) -> SessionResourceView:
        paused.set()
        await release.wait()
        return await original_terminal_launch(**kwargs)

    if pause_at == "reprobe":
        harness.probe.side_effect = delayed_probe
    else:
        harness.registry.launch_auxiliary_terminal.side_effect = delayed_terminal
    launch = asyncio.create_task(harness.launch())
    try:
        await asyncio.wait_for(paused.wait(), timeout=2.0)
        assert not launch.done()
        harness.snapshot["model_override"] = new_pick
    finally:
        release.set()
        await launch

    assert harness.snapshot["model_override"] == new_pick
    assert harness.resets == [{"expected_model_override": _RETIRED_PICK}]
    assert harness.builds[0]["model"] == _PROVIDER_DEFAULT
