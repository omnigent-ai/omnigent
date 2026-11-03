"""Shared fixtures for the native Codex app server tests."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_ucode_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Point ucode state at an empty per-test path.

    A Databricks-profile launch reads ``~/.ucode/state.json`` for its gateway and
    auth, so without this the tests would depend on the machine running them.

    :param tmp_path: Per-test directory that holds no ucode state.
    :param monkeypatch: Fixture used to redirect the state path.
    """
    monkeypatch.setattr(
        "omnigent.onboarding.ucode_state._STATE_PATH", tmp_path / "no-ucode-state.json"
    )
