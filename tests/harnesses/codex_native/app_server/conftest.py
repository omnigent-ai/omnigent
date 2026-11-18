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


@pytest.fixture(autouse=True)
def _no_installed_codex_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Keep the host's installed Codex catalog from reordering canned listings.

    Tests of that ordering patch ``_codex_catalog_models`` themselves.

    :param monkeypatch: Fixture used to stub the catalog read.
    """
    monkeypatch.setattr("omnigent.harnesses.codex_native.app_server._codex_catalog_models", list)
