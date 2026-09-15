"""Tests for the claude-native transcript-forwarder heal on a live pane.

The web Chat renders from the server conversation store, which only the
runner-owned transcript forwarder fills; the Terminal tab reads the tmux pane
directly. Pane (re)creation is the only launch path that starts a forwarder, so
a forwarder that dies while the pane stays alive leaves every later web turn's
assistant reply visible in the terminal but missing from Chat. These tests
cover the orchestration helpers that restart the forwarder from the per-turn
self-heal seam.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from omnigent.runner.native import orchestration
from omnigent.terminals import TerminalRegistry
from tests.runner.helpers import NullServerClient, make_test_terminal_instance

_SESSION_ID = "feedbeeffeedbeeffeedbeeffeedbeef"
_EXTERNAL_SESSION_ID = "0a1b2c3d-1111-2222-3333-444455556666"


@dataclass
class _RegistryStub:
    """Resource-registry stub exposing only the terminal registry."""

    terminal_registry: TerminalRegistry | None


class _SnapshotServerClient:
    """Server-client stub returning a fixed session snapshot for GETs."""

    def __init__(self, snapshot: dict[str, Any]) -> None:
        self._snapshot = snapshot

    async def get(self, url: str, **kwargs: Any) -> Any:
        del url, kwargs
        snapshot = self._snapshot

        class _Response:
            status_code = 200

            def json(self) -> dict[str, Any]:
                return snapshot

        return _Response()


class _ExplodingServerClient:
    """Server-client stub whose GET raises a non-HTTP error."""

    async def get(self, url: str, **kwargs: Any) -> Any:
        del url, kwargs
        raise RuntimeError("session lookup blew up")


def _registry_with_live_claude_pane(tmp_path: Path) -> TerminalRegistry:
    """Return a terminal registry holding a live ('claude', 'main') pane."""
    registry = TerminalRegistry()
    pane = make_test_terminal_instance("claude", "main", tmp_path, running=True)
    registry._by_conversation.setdefault(_SESSION_ID, {})[("claude", "main")] = pane
    return registry


async def _cleanup_forwarder_task() -> None:
    """Cancel and drop any forwarder task the test registered."""
    with contextlib.suppress(Exception):
        await orchestration._cancel_auto_forwarder_task(_SESSION_ID)
    orchestration._AUTO_FORWARDER_TASKS.pop(_SESSION_ID, None)


@pytest.mark.asyncio
async def test_heal_restarts_dead_forwarder_for_live_pane(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A live pane with no registered forwarder gets a fresh forwarder task."""
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:9")
    orchestration._AUTO_FORWARDER_TASKS.pop(_SESSION_ID, None)
    registry = _registry_with_live_claude_pane(tmp_path)
    try:
        await orchestration._ensure_claude_forwarder_for_session(
            _SESSION_ID,
            server_client=NullServerClient(),  # type: ignore[arg-type]
            resource_registry=_RegistryStub(terminal_registry=registry),  # type: ignore[arg-type]
        )
        task = orchestration._AUTO_FORWARDER_TASKS.get(_SESSION_ID)
        assert task is not None and not task.done(), (
            "heal did not register a live forwarder task for the live-pane session"
        )
        assert task.get_name() == f"claude-forwarder-{_SESSION_ID}"
    finally:
        await _cleanup_forwarder_task()


@pytest.mark.asyncio
async def test_heal_keeps_live_forwarder_untouched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A live forwarder is left alone — the heal must never double-forward."""
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:9")
    registry = _registry_with_live_claude_pane(tmp_path)
    incumbent: asyncio.Task[object] = asyncio.create_task(asyncio.sleep(60))
    orchestration._AUTO_FORWARDER_TASKS[_SESSION_ID] = incumbent
    try:
        await orchestration._ensure_claude_forwarder_for_session(
            _SESSION_ID,
            server_client=NullServerClient(),  # type: ignore[arg-type]
            resource_registry=_RegistryStub(terminal_registry=registry),  # type: ignore[arg-type]
        )
        assert orchestration._AUTO_FORWARDER_TASKS.get(_SESSION_ID) is incumbent
        assert not incumbent.cancelled()
    finally:
        incumbent.cancel()
        await _cleanup_forwarder_task()


@pytest.mark.asyncio
async def test_heal_skips_session_without_claude_pane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No registered claude pane means nothing to mirror — no task starts."""
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:9")
    orchestration._AUTO_FORWARDER_TASKS.pop(_SESSION_ID, None)
    await orchestration._ensure_claude_forwarder_for_session(
        _SESSION_ID,
        server_client=NullServerClient(),  # type: ignore[arg-type]
        resource_registry=_RegistryStub(terminal_registry=TerminalRegistry()),  # type: ignore[arg-type]
    )
    assert orchestration._AUTO_FORWARDER_TASKS.get(_SESSION_ID) is None


@pytest.mark.asyncio
async def test_heal_swallows_lookup_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failing session lookup is logged, not raised into the caller's turn."""
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:9")
    orchestration._AUTO_FORWARDER_TASKS.pop(_SESSION_ID, None)
    registry = _registry_with_live_claude_pane(tmp_path)
    try:
        await orchestration._ensure_claude_forwarder_for_session(
            _SESSION_ID,
            server_client=_ExplodingServerClient(),  # type: ignore[arg-type]
            resource_registry=_RegistryStub(terminal_registry=registry),  # type: ignore[arg-type]
        )
    finally:
        await _cleanup_forwarder_task()


@pytest.mark.asyncio
async def test_start_at_end_defers_to_persisted_cursor(tmp_path: Path) -> None:
    """A persisted forward cursor wins: the answer is False (and ignored)."""
    from omnigent.harnesses.claude_native.forwarder import (
        TranscriptForwardState,
        _write_forward_state,
    )

    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    _write_forward_state(
        bridge_dir,
        TranscriptForwardState(transcript_path=tmp_path / "t.jsonl", line_cursor=3),
    )
    assert (
        await orchestration._claude_forwarder_start_at_end_for_heal(
            _ExplodingServerClient(),  # type: ignore[arg-type]  # must not be consulted
            _SESSION_ID,
            bridge_dir,
        )
        is False
    )


@pytest.mark.asyncio
async def test_start_at_end_false_for_fresh_session(tmp_path: Path) -> None:
    """No cursor + no bound external Claude session → replay from the start."""
    assert (
        await orchestration._claude_forwarder_start_at_end_for_heal(
            _SnapshotServerClient({}),  # type: ignore[arg-type]
            _SESSION_ID,
            tmp_path / "bridge",
        )
        is False
    )


@pytest.mark.asyncio
async def test_start_at_end_true_for_cold_resume_with_assistant_history(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Cold resume whose assistant history already reached Omnigent skips the prefix."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    project_dir = tmp_path / "claude-projects"
    project_dir.mkdir()
    (project_dir / f"{_EXTERNAL_SESSION_ID}.jsonl").write_text(
        json.dumps({"type": "user"}) + "\n", encoding="utf-8"
    )

    from omnigent.harnesses.claude_native import main as claude_native_main

    monkeypatch.setattr(claude_native_main, "_claude_project_dir_for_cwd", lambda cwd: project_dir)

    async def _items(client: Any, session_id: str) -> list[dict[str, Any]]:
        del client, session_id
        return [{"role": "user"}, {"role": "assistant"}]

    monkeypatch.setattr(claude_native_main, "_fetch_all_session_items_for_claude_resume", _items)
    snapshot = {"external_session_id": _EXTERNAL_SESSION_ID, "workspace": str(workspace)}
    assert (
        await orchestration._claude_forwarder_start_at_end_for_heal(
            _SnapshotServerClient(snapshot),  # type: ignore[arg-type]
            _SESSION_ID,
            tmp_path / "bridge",
        )
        is True
    )


@pytest.mark.asyncio
async def test_start_at_end_false_without_assistant_history(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Cold resume whose Omnigent items hold no assistant turns replays from 0."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    project_dir = tmp_path / "claude-projects"
    project_dir.mkdir()
    (project_dir / f"{_EXTERNAL_SESSION_ID}.jsonl").write_text(
        json.dumps({"type": "user"}) + "\n", encoding="utf-8"
    )

    from omnigent.harnesses.claude_native import main as claude_native_main

    monkeypatch.setattr(claude_native_main, "_claude_project_dir_for_cwd", lambda cwd: project_dir)

    async def _items(client: Any, session_id: str) -> list[dict[str, Any]]:
        del client, session_id
        return [{"role": "user"}]

    monkeypatch.setattr(claude_native_main, "_fetch_all_session_items_for_claude_resume", _items)
    snapshot = {"external_session_id": _EXTERNAL_SESSION_ID, "workspace": str(workspace)}
    assert (
        await orchestration._claude_forwarder_start_at_end_for_heal(
            _SnapshotServerClient(snapshot),  # type: ignore[arg-type]
            _SESSION_ID,
            tmp_path / "bridge",
        )
        is False
    )
