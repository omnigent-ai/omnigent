"""Tests for opt-in CLI startup profiling."""

from __future__ import annotations

import io

import pytest

from omnigent._startup_profile import StartupProfiler


def test_startup_profiler_env_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Truthy env vars enable startup timing output.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    monkeypatch.setenv("OMNIGENT_TEST_PROFILE", "yes")
    stream = io.StringIO()
    clock_values = iter([10.0, 10.25])

    profiler = StartupProfiler.from_env(
        name="test command",
        env_var="OMNIGENT_TEST_PROFILE",
        clock=lambda: next(clock_values),
        stream=stream,
    )
    profiler.mark("first stage")

    assert stream.getvalue() == "[test command startup +0.250s delta=0.250s] first stage\n"


def test_startup_profiler_disabled_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Missing env vars leave startup profiling silent by default.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    monkeypatch.delenv("OMNIGENT_TEST_PROFILE", raising=False)
    stream = io.StringIO()

    profiler = StartupProfiler.from_env(
        name="test command",
        env_var="OMNIGENT_TEST_PROFILE",
        stream=stream,
    )
    profiler.mark("hidden")

    assert stream.getvalue() == ""
