"""Tests for the codex-native client-side launch-state store."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import pytest

from omnigent.codex_native_state import (
    read_launch_state,
    write_launch_state,
)


def test_write_and_read_launch_state_round_trips(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Launch state persists the cwd needed by resume-time alignment.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary directory for isolated state.
    :returns: None.
    """
    state_root = tmp_path / "state"
    monkeypatch.setenv("OMNIGENT_CODEX_NATIVE_STATE_DIR", str(state_root))

    write_launch_state("conv_abc", "/repo")

    state = read_launch_state("conv_abc")
    assert state is not None
    assert state.working_directory == "/repo"


def test_launch_state_path_hashes_conversation_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Conversation ids never land in the filesystem path directly.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary directory for isolated state.
    :returns: None.
    """
    state_root = tmp_path / "state"
    monkeypatch.setenv("OMNIGENT_CODEX_NATIVE_STATE_DIR", str(state_root))
    conversation_id = "../../../etc/passwd"
    digest = hashlib.sha256(conversation_id.encode("utf-8")).hexdigest()[:32]

    write_launch_state(conversation_id, "/repo")

    assert (state_root / digest / "launch.json").is_file()
    assert not (tmp_path / "etc").exists()


def test_conflicting_launch_state_write_keeps_original(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    A second write for the same conversation cannot silently move cwd.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary directory for isolated state.
    :param caplog: Pytest log capture fixture.
    :returns: None.
    """
    monkeypatch.setenv("OMNIGENT_CODEX_NATIVE_STATE_DIR", str(tmp_path / "state"))
    # Defensive: sibling CLI/logging tests can leave the package
    # logger with propagation disabled in this xdist worker. The
    # warning is emitted by ``omnigent.codex_native_state`` and
    # caplog's handler is attached at root.
    logging.getLogger("omnigent").propagate = True
    write_launch_state("conv_abc", "/original")

    with caplog.at_level(logging.WARNING):
        write_launch_state("conv_abc", "/other")

    state = read_launch_state("conv_abc")
    assert state is not None
    assert state.working_directory == "/original"
    assert any("codex-native launch state mismatch" in record.message for record in caplog.records)


def test_missing_or_malformed_launch_state_returns_none(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Bad state files behave like legacy unrecorded sessions.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary directory for isolated state.
    :returns: None.
    """
    state_root = tmp_path / "state"
    monkeypatch.setenv("OMNIGENT_CODEX_NATIVE_STATE_DIR", str(state_root))
    digest = hashlib.sha256(b"conv_bad").hexdigest()[:32]
    state_dir = state_root / digest
    state_dir.mkdir(parents=True)
    (state_dir / "launch.json").write_text("{not-json\n", encoding="utf-8")

    assert read_launch_state("conv_missing") is None
    assert read_launch_state("conv_bad") is None


def test_empty_working_directory_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Empty cwd would make every later resume comparison meaningless.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary directory for isolated state.
    :returns: None.
    """
    monkeypatch.setenv("OMNIGENT_CODEX_NATIVE_STATE_DIR", str(tmp_path / "state"))

    with pytest.raises(ValueError, match="working_directory"):
        write_launch_state("conv_abc", "")


def test_relative_working_directory_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Launch state records canonical absolute cwd values only.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary directory for isolated state.
    :returns: None.
    """
    monkeypatch.setenv("OMNIGENT_CODEX_NATIVE_STATE_DIR", str(tmp_path / "state"))

    with pytest.raises(ValueError, match="absolute path"):
        write_launch_state("conv_abc", "relative/repo")
