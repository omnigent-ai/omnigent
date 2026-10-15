"""Pytest fixtures shared by every repository test tree."""

from __future__ import annotations

import os
import shutil
from collections.abc import Generator
from pathlib import Path

import pytest


@pytest.fixture(scope="session", autouse=True)
def cleanup_snapshot_failures(pytestconfig: pytest.Config) -> Generator[None, None, None]:
    """Keep a worker's snapshot cleanup from deleting another worker's output."""
    if not any(
        getattr(plugin, "__name__", None) == "pytest_playwright_visual_snapshot.plugin"
        for plugin in pytestconfig.pluginmanager.get_plugins()
    ):
        yield
        return

    from pytest_playwright_visual_snapshot.plugin import SnapshotPaths, _get_option

    root_dir = Path(str(pytestconfig.rootdir))
    SnapshotPaths.snapshots_path = Path(
        _get_option(pytestconfig, "playwright_visual_snapshots_path", cast=str)
        or (root_dir / "__snapshots__")
    )
    base_failures_path = Path(
        _get_option(pytestconfig, "playwright_visual_snapshot_failures_path", cast=str)
        or (root_dir / "snapshot_failures")
    )
    worker = os.environ.get("PYTEST_XDIST_WORKER")
    failures_path = base_failures_path / worker if worker else base_failures_path
    SnapshotPaths.failures_path = failures_path

    # Workers only remove their own leaf; serial runs retain the existing path.
    shutil.rmtree(failures_path, ignore_errors=True)
    failures_path.mkdir(parents=True, exist_ok=True)
    yield
