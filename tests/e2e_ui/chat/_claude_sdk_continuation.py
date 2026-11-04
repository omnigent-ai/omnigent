"""Shared lifecycle for the claude-sdk ``/clear`` and ``/rewind`` browser journeys."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field

import httpx
import pytest

from tests.e2e_ui.conftest import _ensure_runner_online, _server_state


@dataclass
class ClaudeSdkRun:
    """The shared runner a journey uses, and the sessions it made there."""

    runner_id: str
    created: list[str] = field(default_factory=list)


@pytest.fixture
def claude_sdk_run(
    live_server: str, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[ClaudeSdkRun]:
    """Bring the shared runner online; afterwards delete the journey's sessions.

    A runner this fixture had to respawn is stopped again.
    """
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    run = ClaudeSdkRun(runner_id=str(_server_state["runner_id"]))
    try:
        yield run
    finally:
        for session_id in run.created:
            httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        if respawned is not None:
            respawned.terminate()
            try:
                respawned.wait(timeout=5)
            except Exception:  # best-effort teardown
                respawned.kill()
                respawned.wait(timeout=5)
