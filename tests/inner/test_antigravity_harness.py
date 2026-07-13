"""
Tests for the ``harness: antigravity`` wrap shape.

Mirror of the openai-agents wrap tests — verifies the wrap module has the
same shape (registry entry, FastAPI app routes, env-var-driven lazy
executor construction). Does NOT exercise the real ``google-antigravity``
SDK; the inner :class:`AntigravityExecutor.__init__` is mocked so the
tests pass without the package installed.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnigent.inner import antigravity_harness
from omnigent.runtime.harnesses import _HARNESS_MODULES


def test_harness_module_registered_in_module_registry() -> None:
    """``"antigravity"`` resolves to the harness module path.

    Without this entry, the runner subprocess can't find the wrap when the
    parent tries to spawn it for an ``executor.harness == "antigravity"``
    spec.
    """
    assert _HARNESS_MODULES.get("antigravity") == "omnigent.inner.antigravity_harness"


def test_create_app_returns_fastapi_with_required_routes() -> None:
    """``create_app()`` returns a FastAPI app exposing the harness API.

    The :class:`AntigravityExecutor` is constructed lazily on the first
    turn (not at app build time), so this passes without
    ``google-antigravity`` installed.
    """
    app = antigravity_harness.create_app()
    # The harness API routes are mounted via a lazily-included router, so the
    # OpenAPI schema is the reliable surface to assert against.
    paths = set(app.openapi().get("paths", {}).keys())
    assert "/health" in paths
    assert "/v1/sessions/{conversation_id}/events" in paths


_ALL_ANTIGRAVITY_ENV = (
    "HARNESS_ANTIGRAVITY_MODEL",
    "HARNESS_ANTIGRAVITY_API_KEY",
    "HARNESS_ANTIGRAVITY_VERTEX",
    "HARNESS_ANTIGRAVITY_PROJECT",
    "HARNESS_ANTIGRAVITY_LOCATION",
)


def _capture_factory_kwargs(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Build the executor with ``__init__`` stubbed and return its kwargs.

    :param monkeypatch: pytest monkeypatch fixture.
    :returns: The kwargs the factory passed to ``AntigravityExecutor.__init__``.
    """
    captured: dict[str, Any] = {}

    def _fake_init(self: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(
        "omnigent.inner.antigravity_harness.AntigravityExecutor.__init__", _fake_init
    )
    antigravity_harness._build_antigravity_executor()
    return captured


def test_executor_factory_threads_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ``HARNESS_ANTIGRAVITY_*`` env vars thread into the executor ctor.

    Locks in the canonical Gemini-native env-var contract the spawn-env builder
    (``_build_antigravity_spawn_env`` in workflow.py) emits. There are no
    ``*_GATEWAY_*`` vars — the SDK has no OpenAI-compatible base_url.
    """
    monkeypatch.setenv("HARNESS_ANTIGRAVITY_MODEL", "gemini-3-pro")
    monkeypatch.setenv("HARNESS_ANTIGRAVITY_API_KEY", "ag-test-key")
    monkeypatch.setenv("HARNESS_ANTIGRAVITY_VERTEX", "true")
    monkeypatch.setenv("HARNESS_ANTIGRAVITY_PROJECT", "my-proj")
    monkeypatch.setenv("HARNESS_ANTIGRAVITY_LOCATION", "us-central1")

    captured = _capture_factory_kwargs(monkeypatch)

    assert captured["model"] == "gemini-3-pro"
    assert captured["api_key"] == "ag-test-key"
    assert captured["vertex"] is True
    assert captured["project"] == "my-proj"
    assert captured["location"] == "us-central1"


def test_executor_factory_defaults_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset env vars resolve to ``None`` / ``False`` (SDK uses ambient creds)."""
    for var in _ALL_ANTIGRAVITY_ENV:
        monkeypatch.delenv(var, raising=False)

    captured = _capture_factory_kwargs(monkeypatch)

    assert captured["model"] is None
    assert captured["api_key"] is None
    # vertex is a bool flag, so its "unset" value is False (not None).
    assert captured["vertex"] is False
    assert captured["project"] is None
    assert captured["location"] is None


@pytest.mark.parametrize(
    "value,expected",
    [
        ("1", True),
        ("true", True),
        ("TRUE", True),
        ("yes", True),
        ("on", True),
        ("", False),
        ("false", False),
        ("0", False),
        ("no", False),
    ],
)
def test_vertex_flag_parsing(monkeypatch: pytest.MonkeyPatch, value: str, expected: bool) -> None:
    """The Vertex flag accepts the documented truthy spellings, else False."""
    for var in _ALL_ANTIGRAVITY_ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HARNESS_ANTIGRAVITY_VERTEX", value)

    captured = _capture_factory_kwargs(monkeypatch)

    # A wrong parse here would silently send Gemini traffic down the wrong auth
    # path (API key vs Vertex ADC), so the boolean must be exact.
    assert captured["vertex"] is expected
