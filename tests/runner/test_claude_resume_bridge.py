"""Tests for claude-native resume bridge selection (post-/clear isolation).

``_resolve_claude_resume_bridge_id`` chooses which bridge dir a (re)started
claude-native session should use, driven by the session's ``bridge_id`` LABEL
and that dir's on-disk ``active_session_id`` (the one signal visible across
runner processes):

- ``active`` None / == session_id → use the labelled dir (fresh, reconnect, CLI
  random bridge, the /clear rotation's NEW session whose inherited dir is its
  own, and a prior resume's fork).
- ``active`` == a different live session → a /clear handed the live pane to that
  sibling; resuming on the shared dir would double-mirror the transcript and
  trip the executor guard, so fork to an isolated dir and persist it.
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


def _label(monkeypatch: pytest.MonkeyPatch, label: str) -> None:
    """Make ``_claude_native_bridge_id_for_session`` report ``label``."""

    async def _inner(*, server_client: object, session_id: str) -> str:
        return label

    monkeypatch.setattr(runner_app, "_claude_native_bridge_id_for_session", _inner)


class _RecordingClient:
    """Minimal async client capturing the resolver's persist PATCH."""

    def __init__(self) -> None:
        self.patches: list[tuple[str, dict]] = []

    async def patch(self, url: str, *, json: dict) -> None:
        """Record a label-persist PATCH and return nothing (best-effort)."""
        self.patches.append((url, json))


@pytest.mark.asyncio
async def test_fresh_session_uses_label(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No bridge dir yet (label == session_id) → use it, no fork."""
    _label(monkeypatch, "conv_a")
    result = await _resolve_claude_resume_bridge_id(server_client=object(), session_id="conv_a")
    assert result == "conv_a"


@pytest.mark.asyncio
async def test_reconnect_reuses_own_bridge(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A plain reconnect (D(label).active == session_id) keeps the label."""
    _label(monkeypatch, "conv_a")
    _seed_bridge("conv_a", "conv_a", tmp_path)
    result = await _resolve_claude_resume_bridge_id(server_client=object(), session_id="conv_a")
    assert result == "conv_a"


@pytest.mark.asyncio
async def test_new_session_uses_inherited_bridge(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The /clear rotation's NEW session keeps its inherited (shared) bridge.

    Its label points at the old session's dir, whose ``active_session_id`` is the
    NEW session itself (the live pane) — so it must use that dir, NOT its own
    session_id dir (which is empty and has no tmux target). Regression guard for
    the "tmux target not advertised" break.
    """
    _label(monkeypatch, "conv_shared")
    _seed_bridge("conv_shared", "conv_new", tmp_path)
    result = await _resolve_claude_resume_bridge_id(server_client=object(), session_id="conv_new")
    assert result == "conv_shared"


@pytest.mark.asyncio
async def test_cli_random_bridge_is_kept(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A CLI session's random bridge_id (active == session_id) is preserved."""
    _label(monkeypatch, "tok_random")
    _seed_bridge("tok_random", "conv_a", tmp_path)
    result = await _resolve_claude_resume_bridge_id(server_client=object(), session_id="conv_a")
    assert result == "tok_random"


@pytest.mark.asyncio
async def test_collision_forks_and_persists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Old session whose labelled dir is owned by a live sibling → fork + persist."""
    _label(monkeypatch, "conv_a")
    _seed_bridge("conv_a", "conv_b", tmp_path)  # sibling conv_b owns it
    client = _RecordingClient()
    result = await _resolve_claude_resume_bridge_id(server_client=client, session_id="conv_a")
    assert result != "conv_a"
    assert result.startswith("conv_a-clr-")
    # Persisted to the label so auto-create + the executor converge on it.
    assert client.patches == [
        ("/v1/sessions/conv_a", {"labels": {"omnigent.claude_native.bridge_id": result}})
    ]


@pytest.mark.asyncio
async def test_minted_fork_not_yet_prepared_converges(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A just-minted fork (label set, dir not prepared yet) is reused, not re-forked.

    This is the convergence between the session-init spawn_env (which mints +
    persists) and auto-create (which then reads the label before the dir is
    prepared, so its active is still None).
    """
    _label(monkeypatch, "conv_a-clr-cafe")  # label already points at the fork
    # D(conv_a-clr-cafe) intentionally NOT seeded → active is None.
    result = await _resolve_claude_resume_bridge_id(server_client=object(), session_id="conv_a")
    assert result == "conv_a-clr-cafe"


@pytest.mark.asyncio
async def test_prepared_fork_is_reused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A prior resume's prepared fork (active == session_id) is reused."""
    _label(monkeypatch, "conv_a-clr-cafe")
    _seed_bridge("conv_a-clr-cafe", "conv_a", tmp_path)
    result = await _resolve_claude_resume_bridge_id(server_client=object(), session_id="conv_a")
    assert result == "conv_a-clr-cafe"


@pytest.mark.asyncio
async def test_stale_label_repairs_to_session_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A stale label (foreign id, dir absent) is repaired to the natural dir."""
    _label(monkeypatch, "m0-bridge_from_prior_rotation")
    # D(m0-bridge_from_prior_rotation) is absent → active None, not our fork.
    result = await _resolve_claude_resume_bridge_id(server_client=object(), session_id="conv_a")
    assert result == "conv_a"
