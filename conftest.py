"""Repository-wide pytest fixtures for shared test plugins."""

from __future__ import annotations

import os
from collections.abc import Generator
from pathlib import Path

import pytest


@pytest.fixture(scope="session", autouse=True)
def cleanup_snapshot_failures(pytestconfig: pytest.Config) -> Generator[None, None, None]:
    """Isolate snapshot cleanup across every test lane and xdist worker."""
    if not pytestconfig.pluginmanager.hasplugin("playwright_visual_snapshot"):
        yield
        return

    import shutil

    from pytest_playwright_visual_snapshot.plugin import SnapshotPaths, _get_option

    root_dir = Path(pytestconfig.rootdir)  # type: ignore[arg-type]

    SnapshotPaths.snapshots_path = Path(
        _get_option(pytestconfig, "playwright_visual_snapshots_path", cast=str)
        or (root_dir / "__snapshots__")
    )

    base_failures_path = Path(
        _get_option(pytestconfig, "playwright_visual_snapshot_failures_path", cast=str)
        or (root_dir / "snapshot_failures")
    )
    # Per-worker leaf under xdist; the base path itself when run serially
    # (master process / no xdist), keeping non-xdist output identical.
    worker = os.environ.get("PYTEST_XDIST_WORKER")
    failures_path = base_failures_path / worker if worker else base_failures_path
    SnapshotPaths.failures_path = failures_path

    # Only this worker's own leaf is ever removed, so the rmtree/mkdir pair is
    # uncontended; parents=True only creates the shared parent, never deletes it.
    shutil.rmtree(failures_path, ignore_errors=True)
    failures_path.mkdir(parents=True, exist_ok=True)

    yield
