"""Tests for the ``harness: databricks-genie`` wrap shape.

Verifies the registry entry, FastAPI routes, and env-var-driven lazy executor
construction. ``DatabricksGenieExecutor.__init__`` is mocked so the test runs
without ``databricks-sdk`` or a live workspace.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from omnigent.inner import databricks_genie_harness
from omnigent.inner.databricks_genie_executor import _DEFAULT_TIMEOUT_SECONDS
from omnigent.runtime.harnesses import _HARNESS_MODULES


def test_harness_module_registered_in_module_registry() -> None:
    assert _HARNESS_MODULES.get("databricks-genie") == "omnigent.inner.databricks_genie_harness"


def test_create_app_returns_fastapi_with_required_routes() -> None:
    app = databricks_genie_harness.create_app()
    paths = {route.path for route in app.routes}  # type: ignore[attr-defined]
    assert "/health" in paths
    assert "/v1/sessions/{conversation_id}/events" in paths


def test_executor_factory_reads_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_DATABRICKS_GENIE_MODEL", "space-123")
    monkeypatch.setenv("HARNESS_DATABRICKS_GENIE_PROFILE", "dev")
    monkeypatch.setenv("HARNESS_DATABRICKS_GENIE_TIMEOUT", "120")

    captured: dict[str, Any] = {}

    def _fake_init(self: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    with patch(
        "omnigent.inner.databricks_genie_harness.DatabricksGenieExecutor.__init__",
        _fake_init,
    ):
        databricks_genie_harness._build_databricks_genie_executor()

    assert captured["space_id"] == "space-123"
    assert captured["profile"] == "dev"
    assert captured["timeout_seconds"] == 120.0


def test_executor_factory_unset_optional_env_passes_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for var in (
        "HARNESS_DATABRICKS_GENIE_MODEL",
        "HARNESS_DATABRICKS_GENIE_PROFILE",
        "HARNESS_DATABRICKS_GENIE_TIMEOUT",
    ):
        monkeypatch.delenv(var, raising=False)

    captured: dict[str, Any] = {}

    def _fake_init(self: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    with patch(
        "omnigent.inner.databricks_genie_harness.DatabricksGenieExecutor.__init__",
        _fake_init,
    ):
        databricks_genie_harness._build_databricks_genie_executor()

    assert captured["space_id"] is None
    assert captured["profile"] is None
    assert captured["timeout_seconds"] == _DEFAULT_TIMEOUT_SECONDS


def test_malformed_timeout_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_DATABRICKS_GENIE_TIMEOUT", "not-a-number")
    assert databricks_genie_harness._resolve_timeout() == _DEFAULT_TIMEOUT_SECONDS


def test_valid_timeout_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_DATABRICKS_GENIE_TIMEOUT", "45.5")
    assert databricks_genie_harness._resolve_timeout() == 45.5
