"""Per-test bridge isolation for the forwarder suite."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _allow_tmp_path_as_bridge_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """
    Treat each test's temp dir as the Claude bridge root.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Per-test temp directory.
    :returns: None.
    """
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._BRIDGE_ROOT", tmp_path)
