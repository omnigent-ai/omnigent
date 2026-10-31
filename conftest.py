"""Fixtures shared by every test tree, including integration packages."""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(scope="session", autouse=True)
def cleanup_snapshot_failures(pytestconfig: pytest.Config) -> Iterator[None]:
    """Give each xdist worker its own snapshot-failure directory."""
    plugin_manager = pytestconfig.pluginmanager
    if not any(
        plugin_manager.hasplugin(name)
        for name in (
            "playwright_visual_snapshot",
            "pytest_playwright_visual_snapshot.plugin",
        )
    ):
        yield
        return

    from pytest_playwright_visual_snapshot.plugin import SnapshotPaths, _get_option

    root_dir = pytestconfig.rootpath
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

    # Cleanup only this worker's leaf; other workers retain their artifacts.
    shutil.rmtree(failures_path, ignore_errors=True)
    failures_path.mkdir(parents=True, exist_ok=True)
    yield
