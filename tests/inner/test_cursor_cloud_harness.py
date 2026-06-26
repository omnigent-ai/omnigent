"""Tests for the ``harness: cursor-cloud`` wrap shape.

Verifies the module-registry entry, FastAPI routes, and env-var-driven lazy
executor construction. ``CursorCloudExecutor.__init__`` is pure (no ``cursor-sdk``
import — that is lazily imported inside ``run_turn``), so the env-var tests build
the REAL executor and assert its private fields, rather than mocking the
constructor (which would only prove the mock captured kwargs).
"""

from __future__ import annotations

import json

import pytest

from omnigent.inner import cursor_cloud_harness
from omnigent.inner.cursor_cloud_executor import CursorCloudExecutor
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

    # The constructor is pure (cursor-sdk is imported lazily inside run_turn),
    # so build the REAL executor and assert its resolved private fields.
    executor = cursor_cloud_harness._build_cursor_cloud_executor()
    assert isinstance(executor, CursorCloudExecutor)

    assert executor._model_override == "claude-4.6-sonnet-thinking"
    assert executor._api_key == "crsr_secret"
    assert executor._repo_url == "https://github.com/org/repo"
    assert executor._ref == "main"
    assert executor._agent_name == "demo"
    # HARNESS_CURSOR_CLOUD_CWD takes precedence over the os_env cwd.
    assert executor._cwd == "/tmp/test-cwd"


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
        "HARNESS_CURSOR_CLOUD_OS_ENV",
    ):
        monkeypatch.delenv(var, raising=False)

    executor = cursor_cloud_harness._build_cursor_cloud_executor()
    assert isinstance(executor, CursorCloudExecutor)

    assert executor._model_override is None
    # API key unset -> None (the spawn-env builder resolves the key before spawn;
    # the wrap just reads the resolved value).
    assert executor._api_key is None
    assert executor._repo_url is None
    assert executor._ref is None
    assert executor._agent_name is None
    # No cwd env and default os_env (cwd=None) -> None.
    assert executor._cwd is None


def test_executor_factory_decodes_os_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HARNESS_CURSOR_CLOUD_CWD", raising=False)
    monkeypatch.setenv(
        "HARNESS_CURSOR_CLOUD_OS_ENV",
        json.dumps({"type": "caller_process", "cwd": "/srv/app", "sandbox": {"type": "none"}}),
    )

    executor = cursor_cloud_harness._build_cursor_cloud_executor()
    assert isinstance(executor, CursorCloudExecutor)
    # With no HARNESS_CURSOR_CLOUD_CWD, the os_env cwd seeds the executor cwd.
    assert executor._cwd == "/srv/app"


def test_malformed_os_env_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HARNESS_CURSOR_CLOUD_CWD", raising=False)
    monkeypatch.setenv("HARNESS_CURSOR_CLOUD_OS_ENV", "{not-json")

    # A malformed os_env falls back to the default (cwd=None), so the executor's
    # cwd is None rather than crashing the constructor.
    executor = cursor_cloud_harness._build_cursor_cloud_executor()
    assert isinstance(executor, CursorCloudExecutor)
    assert executor._cwd is None
