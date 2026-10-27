"""The attach-time cost popup repopulation must not leak credential failures."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from omnigent.inner.databricks_executor import DatabricksAuthError
from omnigent.runner import app as runner_app
from omnigent.runner import create_runner_app
from omnigent.spec.types import AgentSpec, ExecutorSpec
from tests.runner.helpers import NullServerClient


class _AuthFailingServerClient(NullServerClient):
    async def get(self, *args: Any, **kwargs: Any) -> Any:
        raise DatabricksAuthError("host credential service unavailable")


@pytest.mark.asyncio
async def test_repop_swallows_databricks_auth_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    captured: dict[str, Any] = {}
    real_register = runner_app.register_resource_routes

    def spy_register(app: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return real_register(app, **kwargs)

    captured_history: dict[str, Any] = {}
    real_history = runner_app.build_session_history

    def spy_history(**kwargs: Any) -> Any:
        captured_history.update(kwargs)
        return real_history(**kwargs)

    monkeypatch.setattr(runner_app, "register_resource_routes", spy_register)
    monkeypatch.setattr(runner_app, "build_session_history", spy_history)
    monkeypatch.setattr(
        "omnigent.native.native_cost_popup.wait_for_tmux_client", lambda *a, **k: True
    )
    create_runner_app(server_client=_AuthFailingServerClient())  # type: ignore[arg-type]

    spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )
    captured_history["_session_spec_cache"]["conv-1"] = spec

    with caplog.at_level(logging.DEBUG, logger=runner_app.__name__):
        await captured["_repop_pending_cost_popup_on_attach"]("conv-1", "/tmp/sock", "t:0")

    records = [r for r in caplog.records if "cost popup" in r.getMessage()]
    assert records
    assert all(r.levelno <= logging.WARNING and r.exc_info is None for r in records)


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["claude-native", "codex-native", "opencode-native"])
async def test_cost_popup_resolves_credentials_outside_event_loop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, harness: str
) -> None:
    captured: dict[str, Any] = {}
    real_build = runner_app.build_native_controls

    def spy_build(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return real_build(**kwargs)

    def token() -> str:
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        return "mock-popup-token"

    def make_factory() -> Any:
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        return token

    monkeypatch.setattr(runner_app, "build_native_controls", spy_build)
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://ap.example")
    monkeypatch.setattr("omnigent.runner._entry._make_auth_token_factory", make_factory)
    for adapter in ("claude_native", "codex_native", "opencode_native"):
        monkeypatch.setattr(
            f"omnigent.harnesses.{adapter}.bridge._BRIDGE_ROOT", tmp_path / adapter
        )
    create_runner_app(server_client=NullServerClient())  # type: ignore[arg-type]

    config_path = await captured["_native_cost_popup_config_file"]("conv-popup", harness)
    assert config_path.is_relative_to(tmp_path)
    assert json.loads(config_path.read_text()) == {
        "ap_server_url": "http://ap.example",
        "ap_auth_headers": {"Authorization": "Bearer mock-popup-token"},
    }
