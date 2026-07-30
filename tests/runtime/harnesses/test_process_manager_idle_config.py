"""Tests for the configurable harness idle-reap window.

The harness idle window (after which an idle harness subprocess is reaped) is
now tunable via the ``OMNIGENT_HARNESS_IDLE_TIMEOUT_S`` env var, mirroring the
runner-level ``runner.idle_timeout_s`` knob. ``0`` disables reaping; an
unparseable / negative value falls back to the default rather than failing the
runner at boot.
"""

from __future__ import annotations

import pytest

from omnigent.runtime.harnesses.process_manager import (
    _DEFAULT_IDLE_TIMEOUT_S,
    _HARNESS_IDLE_TIMEOUT_ENV,
    HarnessProcessManager,
    _resolve_harness_idle_timeout_s,
)


def test_resolve_default_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_HARNESS_IDLE_TIMEOUT_ENV, raising=False)
    assert _resolve_harness_idle_timeout_s() == float(_DEFAULT_IDLE_TIMEOUT_S)


def test_resolve_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_HARNESS_IDLE_TIMEOUT_ENV, "7200")
    assert _resolve_harness_idle_timeout_s() == 7200.0


def test_resolve_zero_disables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_HARNESS_IDLE_TIMEOUT_ENV, "0")
    assert _resolve_harness_idle_timeout_s() == 0.0


@pytest.mark.parametrize("bad", ["abc", "-5", ""])
def test_resolve_invalid_falls_back_to_default(monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    monkeypatch.setenv(_HARNESS_IDLE_TIMEOUT_ENV, bad)
    assert _resolve_harness_idle_timeout_s() == float(_DEFAULT_IDLE_TIMEOUT_S)


def test_manager_uses_env_when_no_explicit_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv(_HARNESS_IDLE_TIMEOUT_ENV, "1800")
    mgr = HarnessProcessManager(tmp_parent=tmp_path)
    assert mgr._idle_timeout_s == 1800.0


def test_manager_explicit_value_overrides_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv(_HARNESS_IDLE_TIMEOUT_ENV, "1800")
    mgr = HarnessProcessManager(idle_timeout_s=42.0, tmp_parent=tmp_path)
    assert mgr._idle_timeout_s == 42.0
