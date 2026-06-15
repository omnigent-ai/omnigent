"""Tests for the Gemini harness wrapper."""

from __future__ import annotations

import json

import pytest

from omnigent.inner import gemini_harness


def test_gemini_harness_builds_executor_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``HARNESS_GEMINI_*`` env vars feed the executor constructor.

    A drift in the env-var contract (a renamed key, a missing one) would
    silently fall back to defaults — the worker would still launch, but with
    the wrong path / cwd / api key. Pin every key and check it lands.
    """
    seen: dict[str, object] = {}

    class FakeGeminiExecutor:
        def __init__(self, **kwargs: object) -> None:
            seen.update(kwargs)

    monkeypatch.setattr(gemini_harness, "GeminiExecutor", FakeGeminiExecutor)
    monkeypatch.setenv("HARNESS_GEMINI_PATH", "/bin/gemini")
    monkeypatch.setenv("HARNESS_GEMINI_CWD", "/work")
    monkeypatch.setenv("HARNESS_GEMINI_API_KEY", "my-key")

    executor = gemini_harness._build_gemini_executor()

    assert isinstance(executor, FakeGeminiExecutor)
    assert seen["gemini_path"] == "/bin/gemini"
    assert seen["cwd"] == "/work"
    assert seen["api_key"] == "my-key"


def test_gemini_harness_emits_no_model_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The harness intentionally has NO ``HARNESS_GEMINI_MODEL`` env var.

    The model is pinned in the executor — exposing a model env would
    reintroduce the override path the pin exists to close. This test
    documents that ``GeminiExecutor`` is never asked for a model from env.
    """
    seen: dict[str, object] = {}

    class FakeGeminiExecutor:
        def __init__(self, **kwargs: object) -> None:
            seen.update(kwargs)

    monkeypatch.setattr(gemini_harness, "GeminiExecutor", FakeGeminiExecutor)
    # Even setting the cursor/mimo-style model env must not influence the
    # constructor — there's no equivalent for gemini.
    monkeypatch.setenv("HARNESS_GEMINI_MODEL", "gemini-2.0-flash")

    gemini_harness._build_gemini_executor()

    assert "model" not in seen


def test_gemini_harness_resolves_os_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A JSON-encoded :class:`OSEnvSpec` round-trips into the executor."""
    seen: dict[str, object] = {}

    class FakeGeminiExecutor:
        def __init__(self, **kwargs: object) -> None:
            seen.update(kwargs)

    monkeypatch.setattr(gemini_harness, "GeminiExecutor", FakeGeminiExecutor)
    payload = {
        "type": "caller_process",
        "cwd": "/repo",
        "sandbox": {"type": "none"},
        "fork": False,
    }
    monkeypatch.setenv("HARNESS_GEMINI_OS_ENV", json.dumps(payload))

    gemini_harness._build_gemini_executor()

    os_env = seen["os_env"]
    assert os_env.cwd == "/repo"  # type: ignore[attr-defined]
    assert os_env.type == "caller_process"  # type: ignore[attr-defined]
    assert os_env.sandbox is not None  # type: ignore[attr-defined]
    assert os_env.sandbox.type == "none"  # type: ignore[attr-defined]


def test_gemini_harness_create_app_returns_fastapi() -> None:
    """``create_app`` must return a real FastAPI app — the runner imports
    this module and calls the factory; a wrong return type would crash the
    daemon at boot rather than at the first turn."""
    from fastapi import FastAPI

    app = gemini_harness.create_app()
    assert isinstance(app, FastAPI)
