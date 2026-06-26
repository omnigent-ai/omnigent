"""Tests for claude-native resume bridge selection (post-/clear isolation).

After a Claude ``/clear`` (or ``/fork``) the rotation hands the live terminal to
the NEW session and leaves the OLD session's natural bridge dir
(``D(session_id)``) with its on-disk ``active_session_id`` pointing at that
sibling. A host relaunch then resumes the old session in a SEPARATE runner
process; reusing the shared dir there puts a second forwarder on the live
transcript (duplicate items) and trips the executor's "no longer active" guard.
:func:`_resolve_claude_resume_bridge_id` detects the collision via the on-disk
``active_session_id`` (the one signal visible across runner processes) and forks
the old session onto an isolated bridge dir.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from omnigent.runner import app as runner_app
from omnigent.runner.app import _resolve_claude_resume_bridge_id


@pytest.fixture(autouse=True)
def _bridge_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Key every bridge dir under the per-test tmp dir."""
    monkeypatch.setattr("omnigent.claude_native_bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr("omnigent.claude_native_bridge._BRIDGE_ROOT", tmp_path / "root")
    yield


def _seed_bridge(bridge_id: str, active: str, tmp_path: Path) -> None:
    """Create ``D(bridge_id)`` with its on-disk active_session_id = ``active``."""
    from omnigent.claude_native_bridge import prepare_bridge_dir, write_active_session_id

    bridge_dir = prepare_bridge_dir(bridge_id, bridge_id=bridge_id, workspace=tmp_path)
    write_active_session_id(bridge_dir, active)


def _label_returning(label: str):
    """A stand-in for ``_claude_native_bridge_id_for_session`` returning ``label``."""

    async def _inner(*, server_client: object, session_id: str) -> str:
        return label

    return _inner


@pytest.mark.asyncio
async def test_fresh_session_uses_session_id(tmp_path: Path) -> None:
    """No bridge dir on disk yet → keep the natural session_id bridge."""
    result = await _resolve_claude_resume_bridge_id(server_client=None, session_id="conv_a")
    assert result == "conv_a"


@pytest.mark.asyncio
async def test_reconnect_reuses_own_bridge(tmp_path: Path) -> None:
    """A plain reconnect (D(conv_a).active == conv_a) keeps session_id."""
    _seed_bridge("conv_a", "conv_a", tmp_path)
    result = await _resolve_claude_resume_bridge_id(server_client=None, session_id="conv_a")
    assert result == "conv_a"


@pytest.mark.asyncio
async def test_collision_forks_to_fresh_bridge(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """D(conv_a) owned by sibling conv_b, label still conv_a → mint a fresh id."""
    _seed_bridge("conv_a", "conv_b", tmp_path)
    monkeypatch.setattr(
        runner_app, "_claude_native_bridge_id_for_session", _label_returning("conv_a")
    )
    result = await _resolve_claude_resume_bridge_id(server_client=object(), session_id="conv_a")
    assert result != "conv_a"
    assert result.startswith("conv_a-clr-")


@pytest.mark.asyncio
async def test_collision_reuses_prior_fork(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A prior resume's forked dir that is still ours is reused (idempotent)."""
    _seed_bridge("conv_a", "conv_b", tmp_path)  # natural dir taken by sibling
    _seed_bridge("conv_a-clr-cafe", "conv_a", tmp_path)  # prior fork, still ours
    monkeypatch.setattr(
        runner_app,
        "_claude_native_bridge_id_for_session",
        _label_returning("conv_a-clr-cafe"),
    )
    result = await _resolve_claude_resume_bridge_id(server_client=object(), session_id="conv_a")
    assert result == "conv_a-clr-cafe"


@pytest.mark.asyncio
async def test_collision_prior_fork_taken_mints_fresh(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If the prior fork dir is now owned by yet another session, mint fresh."""
    _seed_bridge("conv_a", "conv_b", tmp_path)  # natural taken by conv_b
    _seed_bridge("conv_a-clr-cafe", "conv_d", tmp_path)  # prior fork taken by conv_d
    monkeypatch.setattr(
        runner_app,
        "_claude_native_bridge_id_for_session",
        _label_returning("conv_a-clr-cafe"),
    )
    result = await _resolve_claude_resume_bridge_id(server_client=object(), session_id="conv_a")
    assert result.startswith("conv_a-clr-")
    assert result != "conv_a-clr-cafe"
