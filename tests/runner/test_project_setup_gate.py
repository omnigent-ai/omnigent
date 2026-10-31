"""A runner's first turn waits for its worktree's project setup marker."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from omnigent.project_setup import PROJECT_DEFERRED_ENV_ENV_VAR
from omnigent.runner import project_setup_gate
from omnigent.runner.project_setup_gate import (
    PROJECT_SETUP_MARKER_ENV_VAR,
    apply_deferred_project_env,
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


def test_deferred_project_env_is_applied_once_the_runner_is_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYTHONSTALE", "old")
    monkeypatch.setenv(
        PROJECT_DEFERRED_ENV_ENV_VAR,
        json.dumps({"set": {"PYTHONPATH": "/proj"}, "unset": ["PYTHONSTALE"]}),
    )
    monkeypatch.delenv("PYTHONPATH", raising=False)

    apply_deferred_project_env()

    assert os.environ["PYTHONPATH"] == "/proj"
    assert "PYTHONSTALE" not in os.environ
    assert PROJECT_DEFERRED_ENV_ENV_VAR not in os.environ
