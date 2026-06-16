"""Tests for the ``harness: cursor`` wrap shape.

Verifies the registry entry (+ ``cursor`` alias), FastAPI routes, and
env-var-driven lazy executor construction. The inner ``CursorExecutor.__init__``
is mocked so the test runs without a ``cursor-agent`` binary.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from omnigent.inner import cursor_harness
from omnigent.runtime.harnesses import _HARNESS_MODULES


def test_harness_module_registered_in_module_registry() -> None:
    assert _HARNESS_MODULES.get("cursor") == "omnigent.inner.cursor_harness"


def test_create_app_returns_fastapi_with_required_routes() -> None:
    app = cursor_harness.create_app()
    paths = {route.path for route in app.routes}  # type: ignore[attr-defined]
    assert "/health" in paths
    assert "/v1/sessions/{conversation_id}/events" in paths


def test_executor_factory_reads_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_CURSOR_MODEL", "gpt-5")
    monkeypatch.setenv("HARNESS_CURSOR_CWD", "/tmp/test-cwd")
    monkeypatch.setenv("HARNESS_CURSOR_API_KEY", "cur_secret")
    monkeypatch.setenv("HARNESS_CURSOR_AGENT_NAME", "demo")

    captured: dict[str, Any] = {}

    def _fake_init(self: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    with patch(
        "omnigent.inner.cursor_harness.CursorExecutor.__init__",
        _fake_init,
    ):
        cursor_harness._build_cursor_executor()

    assert captured["model"] == "gpt-5"
    assert captured["cwd"] == "/tmp/test-cwd"
    assert captured["api_key"] == "cur_secret"
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
        "HARNESS_CURSOR_MODEL",
        "HARNESS_CURSOR_PATH",
        "HARNESS_CURSOR_CWD",
        "HARNESS_CURSOR_API_KEY",
        "HARNESS_CURSOR_BUNDLE_DIR",
        "HARNESS_CURSOR_AGENT_NAME",
    ):
        monkeypatch.delenv(var, raising=False)

    captured: dict[str, Any] = {}

    def _fake_init(self: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    with patch(
        "omnigent.inner.cursor_harness.CursorExecutor.__init__",
        _fake_init,
    ):
        cursor_harness._build_cursor_executor()

    assert captured["model"] is None
    assert captured["cwd"] is None
    assert captured["api_key"] is None
    assert captured["bundle_dir"] is None
    assert captured["agent_name"] is None
    assert captured["skills_filter"] == "all"


def test_executor_factory_decodes_os_env_and_skills(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "HARNESS_CURSOR_OS_ENV",
        json.dumps({"type": "caller_process", "cwd": "/srv/app", "sandbox": {"type": "none"}}),
    )
    monkeypatch.setenv("HARNESS_CURSOR_SKILLS_FILTER", '["a","b"]')
    monkeypatch.setenv("HARNESS_CURSOR_BUNDLE_DIR", "/tmp/bundle")

    captured: dict[str, Any] = {}

    def _fake_init(self: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    with patch(
        "omnigent.inner.cursor_harness.CursorExecutor.__init__",
        _fake_init,
    ):
        cursor_harness._build_cursor_executor()

    assert captured["os_env"].cwd == "/srv/app"
    assert captured["skills_filter"] == ["a", "b"]
    assert captured["bundle_dir"] == Path("/tmp/bundle")


def test_malformed_os_env_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_CURSOR_OS_ENV", "{not-json")
    captured: dict[str, Any] = {}

    def _fake_init(self: Any, **kwargs: Any) -> None:
        captured["os_env"] = kwargs["os_env"]

    with patch(
        "omnigent.inner.cursor_harness.CursorExecutor.__init__",
        _fake_init,
    ):
        cursor_harness._build_cursor_executor()

    assert captured["os_env"].type == "caller_process"
    assert captured["os_env"].sandbox.type == "none"
