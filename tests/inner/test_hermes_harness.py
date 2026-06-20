"""
Tests for the ``harness: hermes`` wrap shape.

Mirror of ``tests/inner/test_pi_harness.py`` — verifies the wrap
module has the same shape (registry entry, FastAPI app routes,
env-var-driven lazy executor construction). Does NOT exercise
the real Hermes CLI; the inner ``HermesExecutor.__init__`` is
lightweight enough that no mocking is needed for shape tests.

End-to-end Hermes verification (real CLI, real API) should live
in the e2e suite, gated on the ``hermes`` binary being available.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from omnigent.inner import hermes_harness
from omnigent.runtime.harnesses import _HARNESS_MODULES


def test_harness_module_registered_in_module_registry() -> None:
    """``"hermes"`` resolves to the harness module path.

    Without this entry, the runner subprocess can't find the wrap
    when AP-side tries to spawn it for a ``harness: hermes`` spec.
    """
    assert _HARNESS_MODULES.get("hermes") == "omnigent.inner.hermes_harness"


def test_create_app_returns_fastapi_with_required_routes() -> None:
    """``create_app()`` returns a FastAPI app exposing the harness API.

    Verifies the wrap successfully:
    - Imports the executor adapter + Hermes executor module.
    - Builds the FastAPI app via ExecutorAdapter.build().
    - Mounts the standard harness routes.

    The actual HermesExecutor is constructed lazily on the first
    turn (not at app build time), so this test passes without
    a real ``hermes`` CLI on PATH.
    """
    app = hermes_harness.create_app()
    paths = {route.path for route in app.routes}  # type: ignore[attr-defined]
    # Session-keyed harness API: liveness probe + single
    # discriminated-event endpoint per §The Harness API Subset.
    assert "/health" in paths
    assert "/v1/sessions/{conversation_id}/events" in paths


def test_executor_factory_reads_env_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Factory passes env-var values through to HermesExecutor.

    Locks in the v1 config-flow contract: env vars set in AP's
    process before spawning the subprocess (which inherits
    them) are how the wrap learns its config. Verifies model,
    cwd, hermes_path all thread through.
    """
    monkeypatch.setenv("HARNESS_HERMES_MODEL", "test-model-id")
    monkeypatch.setenv("HARNESS_HERMES_CWD", "/tmp/test-cwd")
    monkeypatch.setenv("HARNESS_HERMES_PATH", "/custom/path/hermes")

    executor = hermes_harness._build_hermes_executor()

    assert executor._hermes_path == "/custom/path/hermes"
    assert executor._cwd == "/tmp/test-cwd"
    assert executor._model == "test-model-id"


def test_executor_factory_defaults_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Factory uses sensible defaults when env vars are not set."""
    monkeypatch.delenv("HARNESS_HERMES_MODEL", raising=False)
    monkeypatch.delenv("HARNESS_HERMES_CWD", raising=False)
    monkeypatch.delenv("HARNESS_HERMES_PATH", raising=False)

    executor = hermes_harness._build_hermes_executor()

    # Should fall back to PATH search for "hermes"
    assert executor._hermes_path is not None
    assert "hermes" in executor._hermes_path
    # Model should be None (no override)
    assert executor._model is None


def test_executor_factory_reads_os_env_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Factory decodes JSON-encoded OSEnvSpec."""
    import json

    os_env_spec = {
        "type": "caller_process",
        "cwd": "/workspace",
        "sandbox": {"type": "none"},
        "fork": False,
    }
    monkeypatch.setenv("HARNESS_HERMES_OS_ENV", json.dumps(os_env_spec))

    executor = hermes_harness._build_hermes_executor()

    assert executor._os_env is not None
    assert executor._os_env.type == "caller_process"
    assert executor._os_env.cwd == "/workspace"
    assert executor._os_env.sandbox is not None
    assert executor._os_env.sandbox.type == "none"


def test_executor_factory_reads_skills_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Factory decodes JSON-encoded skills_filter."""
    import json

    monkeypatch.setenv("HARNESS_HERMES_SKILLS_FILTER", json.dumps(["skill-a", "skill-b"]))

    executor = hermes_harness._build_hermes_executor()

    assert executor._skills_filter == ["skill-a", "skill-b"]


def test_executor_factory_skills_filter_default_all() -> None:
    """Factory falls back to 'all' when skills_filter is unset."""
    executor = hermes_harness._build_hermes_executor()

    assert executor._skills_filter == "all"


def test_executor_factory_reads_bundle_and_agent_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Factory reads bundle dir and agent name from env vars."""
    monkeypatch.setenv("HARNESS_HERMES_BUNDLE_DIR", "/tmp/bundle")
    monkeypatch.setenv("HARNESS_HERMES_AGENT_NAME", "my-hermes-agent")

    executor = hermes_harness._build_hermes_executor()

    assert executor._bundle_dir == "/tmp/bundle"
    assert executor._agent_name == "my-hermes-agent"
