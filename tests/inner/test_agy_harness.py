"""Tests for the Antigravity CLI (``agy``) harness wrapper."""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI

from omnigent.inner import agy_harness
from omnigent.inner.agy_executor import AGY_DEFAULT_MODEL


def test_agy_harness_builds_executor_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    class FakeAgyExecutor:
        def __init__(self, **kwargs: object) -> None:
            seen.update(kwargs)

    monkeypatch.setattr(agy_harness, "AgyExecutor", FakeAgyExecutor)
    monkeypatch.setenv("HARNESS_AGY_PATH", "/bin/agy")
    monkeypatch.setenv("HARNESS_AGY_CWD", "/work")
    monkeypatch.setenv("HARNESS_AGY_MODEL", "Gemini 3.1 Pro (High)")
    monkeypatch.setenv("HARNESS_AGY_PRINT_TIMEOUT", "45m")
    monkeypatch.setenv("HARNESS_AGY_AGENT_NAME", "agy-reviewer")
    monkeypatch.setenv("HARNESS_AGY_SKILLS_FILTER", '["review"]')

    executor = agy_harness._build_agy_executor()

    assert isinstance(executor, FakeAgyExecutor)
    assert seen["agy_path"] == "/bin/agy"
    assert seen["cwd"] == "/work"
    assert seen["model"] == "Gemini 3.1 Pro (High)"
    assert seen["print_timeout"] == "45m"
    assert seen["agent_name"] == "agy-reviewer"
    assert seen["skills_filter"] == ["review"]


def test_agy_harness_defaults_to_gemini_31_pro_high(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    class FakeAgyExecutor:
        def __init__(self, **kwargs: object) -> None:
            seen.update(kwargs)

    monkeypatch.setattr(agy_harness, "AgyExecutor", FakeAgyExecutor)

    agy_harness._build_agy_executor()

    assert seen["model"] == AGY_DEFAULT_MODEL


def test_agy_harness_resolves_os_env(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    class FakeAgyExecutor:
        def __init__(self, **kwargs: object) -> None:
            seen.update(kwargs)

    monkeypatch.setattr(agy_harness, "AgyExecutor", FakeAgyExecutor)
    payload = {
        "type": "caller_process",
        "cwd": "/repo",
        "sandbox": {"type": "none"},
        "fork": False,
    }
    monkeypatch.setenv("HARNESS_AGY_OS_ENV", json.dumps(payload))

    agy_harness._build_agy_executor()

    os_env = seen["os_env"]
    assert os_env.cwd == "/repo"  # type: ignore[attr-defined]
    assert os_env.type == "caller_process"  # type: ignore[attr-defined]
    assert os_env.sandbox is not None  # type: ignore[attr-defined]
    assert os_env.sandbox.type == "none"  # type: ignore[attr-defined]


def test_agy_harness_create_app_returns_fastapi() -> None:
    app = agy_harness.create_app()
    assert isinstance(app, FastAPI)
