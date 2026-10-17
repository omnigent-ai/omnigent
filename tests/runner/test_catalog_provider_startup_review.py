"""Private review of provider drift between the actual launch and cache shape."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from omnigent.harnesses.codex_native import app_server as codex
from omnigent.models import model_catalog_store
from tests.runner.conftest import _runner_client
from tests.runner.test_app_sessions_native_events_lifecycle import _RecordingCodexAppServerClient
from tests.runner.test_codex_live_catalog_ownership import (
    _finish_writebacks,
    _register_session,
    _rows,
    _started_catalog,
)
from tests.runner.test_codex_model_pick_fallback import _CODEX_PATH, _SESSION_ID, _LaunchHarness
from tests.runner.test_codex_model_pick_fallback import (
    codex_launch_harness as codex_launch_harness,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", [False, True], ids=["stable-provider", "provider-changed"])
async def test_actual_provider_cannot_write_live_rows_to_a_later_shape(
    codex_launch_harness: _LaunchHarness,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    drift: bool,
) -> None:
    harness = codex_launch_harness
    route_file = tmp_path / "runtime-provider-state"
    route_file.write_text("a")
    keys: dict[str, str] = {}
    initial = {"a": _rows("root"), "b": _rows("other")}
    original_launch = harness.launch
    main_thread = threading.current_thread()

    def route(*, model: str | None, **_kwargs: Any) -> codex.NativeCodexLaunch:
        if drift and threading.current_thread() is not main_thread:
            route_file.write_text("b")
        provider = route_file.read_text()
        return codex.NativeCodexLaunch(
            config_overrides=[f'model_provider="provider-{provider}"'],
            model=model or f"{provider}-shape-default",
            profile=None,
        )

    async def launch_with_provider_config() -> Any:
        monkeypatch.setattr(codex, "_resolve_native_codex_routing", route)
        for provider in ("a", "b"):
            route_file.write_text(provider)
            launch = codex.resolve_native_codex_launch(model=None, spec=harness.agent_spec)
            keys[provider] = codex.codex_catalog_fingerprint(launch, codex_path=_CODEX_PATH)
            model_catalog_store.write_catalog("codex-native", keys[provider], initial[provider])
        route_file.write_text("a")
        return await original_launch()

    monkeypatch.setattr(harness, "launch", launch_with_provider_config)
    app, _, _, _ = await _started_catalog(harness, monkeypatch, tmp_path, selected=False)
    assert keys["a"] != keys["b"]
    assert 'model_provider="provider-a"' in harness.builds[0]["extra_config_overrides"]
    assert route_file.read_text() == ("b" if drift else "a")

    live = _rows("root", live=True)
    client = _RecordingCodexAppServerClient(harness.app_server.listen_url, "review")
    client.model_list_responses = [{"result": {"data": live}}]
    monkeypatch.setattr(codex, "client_for_transport", lambda *_args, **_kwargs: client)
    async with _runner_client(app) as http:
        await _register_session(http)
        response = await http.get(f"/v1/sessions/{_SESSION_ID}/codex-model-options")
        assert response.status_code == 200, response.text
        assert response.json()["models"] == codex.mark_launch_default(live, "root-session-pick")
        await _finish_writebacks()

    assert model_catalog_store.read_catalog("codex-native", keys["b"]) == initial["b"]
    assert model_catalog_store.read_catalog("codex-native", keys["a"]) == (
        initial["a"] if drift else live
    )
