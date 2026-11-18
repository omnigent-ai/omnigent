"""Per-test bridge isolation for the forwarder suite."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.harnesses.claude_native import forwarder


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


@pytest.fixture(autouse=True)
def _isolated_forward_health(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Start each test with healthy process-wide forward-sync tracking.

    A test that leaves sync degraded would otherwise make the next test's
    first delivery log a stray recovery line.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    monkeypatch.setattr(forwarder, "_forward_health", forwarder._ForwardHealth())
