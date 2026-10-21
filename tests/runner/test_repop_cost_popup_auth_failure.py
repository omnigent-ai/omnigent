"""The attach-time cost popup repopulation must not leak credential failures."""

from __future__ import annotations

import logging
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
