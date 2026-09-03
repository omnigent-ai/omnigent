from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest


def _remove_fsmonitor_state(repo: Path) -> None:
    """Stop only this disposable repo's daemon and remove its IPC state."""
    subprocess.run(
        ["git", "-c", "core.fsmonitor=true", "fsmonitor--daemon", "stop"],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )
    for name in ("fsmonitor--daemon", "fsmonitor--daemon.ipc"):
        result = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "rev-parse", "--git-path", name],
            cwd=repo,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0 or not result.stdout.strip():
            continue
        state = Path(result.stdout.strip())
        if not state.is_absolute():
            state = repo / state
        with contextlib.suppress(FileNotFoundError):
            if state.is_dir() and not state.is_symlink():
                shutil.rmtree(state)
            else:
                state.unlink()


@pytest.fixture(autouse=True)
def _disable_fsmonitor_for_fixture_repos(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[None]:
    """Disable inherited fsmonitor before any disposable-repo Git command."""
    count = int(os.environ.get("GIT_CONFIG_COUNT", "0"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", str(count + 1))
    monkeypatch.setenv(f"GIT_CONFIG_KEY_{count}", "core.fsmonitor")
    monkeypatch.setenv(f"GIT_CONFIG_VALUE_{count}", "false")
    yield
    for git_entry in tmp_path.rglob(".git"):
        _remove_fsmonitor_state(git_entry.parent)
