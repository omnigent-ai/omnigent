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


def _build_with_env(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> dict[str, Any]:
    """Build the executor under *env* and return the kwargs it was given.

    Every ``HARNESS_DATABRICKS_GENIE_*`` var absent from *env* is cleared, so a
    developer's real shell can't leak into the expectations.
    """
    for var in (
        "HARNESS_DATABRICKS_GENIE_MODEL",
        "HARNESS_DATABRICKS_GENIE_PROFILE",
        "HARNESS_DATABRICKS_GENIE_TIMEOUT",
        "HARNESS_DATABRICKS_GENIE_ENABLE_VIZ",
    ):
        monkeypatch.delenv(var, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    captured: dict[str, Any] = {}

    def _fake_init(self: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    with patch(
        "omnigent.inner.databricks_genie_harness.DatabricksGenieExecutor.__init__",
        _fake_init,
    ):
        databricks_genie_harness._build_databricks_genie_executor()
    return captured


def test_executor_factory_reads_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _build_with_env(
        monkeypatch,
        {
            "HARNESS_DATABRICKS_GENIE_MODEL": "space-123",
            "HARNESS_DATABRICKS_GENIE_PROFILE": "dev",
            "HARNESS_DATABRICKS_GENIE_TIMEOUT": "120",
            "HARNESS_DATABRICKS_GENIE_ENABLE_VIZ": "true",
        },
    )

    assert captured["space_id"] == "space-123"
    assert captured["profile"] == "dev"
    assert captured["timeout_seconds"] == 120.0
    assert captured["enable_viz"] is True


def test_executor_factory_unset_optional_env_passes_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _build_with_env(monkeypatch, {})

    assert captured["space_id"] is None
    assert captured["profile"] is None
    assert captured["timeout_seconds"] == _DEFAULT_TIMEOUT_SECONDS
    assert captured["enable_viz"] is False


def test_default_timeout_leaves_room_for_a_real_warehouse_query() -> None:
    """An Agent-mode turn plans, queries, and writes a report — 300s was too tight."""
    assert _DEFAULT_TIMEOUT_SECONDS == 900.0


def test_malformed_timeout_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_DATABRICKS_GENIE_TIMEOUT", "not-a-number")
    assert databricks_genie_harness._resolve_timeout() == _DEFAULT_TIMEOUT_SECONDS


def test_valid_timeout_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_DATABRICKS_GENIE_TIMEOUT", "45.5")
    assert databricks_genie_harness._resolve_timeout() == 45.5


@pytest.mark.parametrize(
    "raw",
    [
        "0",  # every request would time out immediately
        "0.0",
        "-1",  # negative deadlines are not a deadline
        "nan",  # parses as a float, but bounds nothing
        "inf",  # a wedged stream would hang forever
        "-inf",
    ],
)
def test_unusable_timeout_falls_back_to_default(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    """Only a positive, finite number of seconds is a usable HTTP deadline."""
    monkeypatch.setenv("HARNESS_DATABRICKS_GENIE_TIMEOUT", raw)
    assert databricks_genie_harness._resolve_timeout() == _DEFAULT_TIMEOUT_SECONDS


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # ``executor.config`` scalars arrive stringified, so a YAML ``true``
        # reaches the wrap as ``"True"`` — the spelling that matters most.
        ("True", True),
        ("true", True),
        ("TRUE", True),
        ("1", True),
        ("  true  ", True),
        ("False", False),
        ("false", False),
        ("0", False),
        ("", False),
        # Not a boolean spelling we accept: stay off rather than guess.
        ("yes", False),
        ("nonsense", False),
    ],
)
def test_enable_viz_env_parsing(monkeypatch: pytest.MonkeyPatch, raw: str, expected: bool) -> None:
    monkeypatch.setenv("HARNESS_DATABRICKS_GENIE_ENABLE_VIZ", raw)
    assert databricks_genie_harness._resolve_enable_viz() is expected


def test_enable_viz_defaults_off_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Visualizations are opt-in, so an unset var must not enable them."""
    monkeypatch.delenv("HARNESS_DATABRICKS_GENIE_ENABLE_VIZ", raising=False)
    assert databricks_genie_harness._resolve_enable_viz() is False
