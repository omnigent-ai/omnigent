"""Tests for the ``harness: cursor-cloud`` wrap shape.

Verifies the module-registry entry, FastAPI routes, and env-var-driven lazy
executor construction. The inner ``CursorCloudExecutor.__init__`` is mocked so
the test runs without a live cloud client / ``cursor-sdk`` call.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest

from omnigent.inner import cursor_cloud_harness
from omnigent.runtime.harnesses import _HARNESS_MODULES


def test_harness_module_registered_in_module_registry() -> None:
    assert _HARNESS_MODULES.get("cursor-cloud") == "omnigent.inner.cursor_cloud_harness"


def test_create_app_returns_fastapi_with_required_routes() -> None:
    app = cursor_cloud_harness.create_app()
    paths = {route.path for route in app.routes}  # type: ignore[attr-defined]
    assert "/health" in paths
    assert "/v1/sessions/{conversation_id}/events" in paths


def test_executor_factory_reads_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_CURSOR_CLOUD_MODEL", "claude-4.6-sonnet-thinking")
    monkeypatch.setenv("HARNESS_CURSOR_CLOUD_API_KEY", "crsr_secret")
    monkeypatch.setenv("HARNESS_CURSOR_CLOUD_REPO", "https://github.com/org/repo")
    monkeypatch.setenv("HARNESS_CURSOR_CLOUD_REF", "main")
    monkeypatch.setenv("HARNESS_CURSOR_CLOUD_CWD", "/tmp/test-cwd")
    monkeypatch.setenv("HARNESS_CURSOR_CLOUD_AGENT_NAME", "demo")

    captured: dict[str, Any] = {}

    def _fake_init(self: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    with patch(
        "omnigent.inner.cursor_cloud_harness.CursorCloudExecutor.__init__",
        _fake_init,
    ):
        cursor_cloud_harness._build_cursor_cloud_executor()

    assert captured["model"] == "claude-4.6-sonnet-thinking"
    assert captured["api_key"] == "crsr_secret"
    assert captured["repo_url"] == "https://github.com/org/repo"
    assert captured["ref"] == "main"
    assert captured["cwd"] == "/tmp/test-cwd"
    assert captured["agent_name"] == "demo"
    # Default os_env when unset: caller_process + sandbox=none.
    os_env_value = captured["os_env"]
    assert os_env_value is not None
    assert os_env_value.type == "caller_process"
    assert os_env_value.sandbox is not None
    assert os_env_value.sandbox.type == "none"


def test_executor_factory_unset_optional_env_passes_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for var in (
        "HARNESS_CURSOR_CLOUD_MODEL",
        "HARNESS_CURSOR_CLOUD_API_KEY",
        "HARNESS_CURSOR_CLOUD_REPO",
        "HARNESS_CURSOR_CLOUD_REF",
        "HARNESS_CURSOR_CLOUD_CWD",
        "HARNESS_CURSOR_CLOUD_AGENT_NAME",
    ):
        monkeypatch.delenv(var, raising=False)

    captured: dict[str, Any] = {}

    def _fake_init(self: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    with patch(
        "omnigent.inner.cursor_cloud_harness.CursorCloudExecutor.__init__",
        _fake_init,
    ):
        cursor_cloud_harness._build_cursor_cloud_executor()

    assert captured["model"] is None
    assert captured["api_key"] is None
    assert captured["repo_url"] is None
    assert captured["ref"] is None
    assert captured["cwd"] is None
    assert captured["agent_name"] is None


def test_executor_factory_decodes_os_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "HARNESS_CURSOR_CLOUD_OS_ENV",
        json.dumps({"type": "caller_process", "cwd": "/srv/app", "sandbox": {"type": "none"}}),
    )
    captured: dict[str, Any] = {}

    def _fake_init(self: Any, **kwargs: Any) -> None:
        captured["os_env"] = kwargs["os_env"]

    with patch(
        "omnigent.inner.cursor_cloud_harness.CursorCloudExecutor.__init__",
        _fake_init,
    ):
        cursor_cloud_harness._build_cursor_cloud_executor()

    assert captured["os_env"].cwd == "/srv/app"


def test_malformed_os_env_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_CURSOR_CLOUD_OS_ENV", "{not-json")
    captured: dict[str, Any] = {}

    def _fake_init(self: Any, **kwargs: Any) -> None:
        captured["os_env"] = kwargs["os_env"]

    with patch(
        "omnigent.inner.cursor_cloud_harness.CursorCloudExecutor.__init__",
        _fake_init,
    ):
        cursor_cloud_harness._build_cursor_cloud_executor()

    assert captured["os_env"].type == "caller_process"
    assert captured["os_env"].sandbox.type == "none"
