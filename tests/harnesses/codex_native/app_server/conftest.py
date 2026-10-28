"""Shared fixtures for the native Codex app server tests."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_native_config_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Keep caller configuration and account state under test-owned paths.

    Ambient Codex config rewrites change catalog fingerprints mid-scenario.
    Profile launches also read ucode gateway/auth state from the caller's home.

    :param tmp_path: Per-test directory that holds no ucode state.
    :param monkeypatch: Fixture used to redirect the state path.
    """
    monkeypatch.setattr(
        "omnigent.onboarding.ucode_state._STATE_PATH", tmp_path / "no-ucode-state.json"
    )
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "caller-codex-home"))
