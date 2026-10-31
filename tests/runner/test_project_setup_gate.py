"""A runner's first turn waits for its worktree's project setup marker."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from omnigent.runner import project_setup_gate
from omnigent.runner.project_setup_gate import (
    PROJECT_SETUP_MARKER_ENV_VAR,
    wait_for_project_setup,
)


@pytest.fixture(autouse=True)
def _fresh_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(project_setup_gate, "_released", False)


async def test_turns_wait_for_the_marker_then_never_wait_again(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    marker = tmp_path / "omnigent-setup.json"
    monkeypatch.setenv(PROJECT_SETUP_MARKER_ENV_VAR, str(marker))

    first_turn = asyncio.create_task(wait_for_project_setup(poll_interval_s=0.01))
    await asyncio.sleep(0.1)
    assert not first_turn.done()
    marker.write_text("{}")
    await asyncio.wait_for(first_turn, timeout=2)

    marker.unlink()
    await asyncio.wait_for(wait_for_project_setup(poll_interval_s=0.01), timeout=0.5)


async def test_a_setup_that_never_finishes_does_not_wedge_the_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(PROJECT_SETUP_MARKER_ENV_VAR, str(tmp_path / "never-written"))

    await asyncio.wait_for(wait_for_project_setup(timeout_s=0.1, poll_interval_s=0.01), timeout=2)


async def test_runners_without_due_setup_do_not_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PROJECT_SETUP_MARKER_ENV_VAR, raising=False)

    await asyncio.wait_for(wait_for_project_setup(), timeout=0.5)
