"""Tests for client-side opencode-native launch state."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import pytest

from omnigent.opencode_native_state import (
    read_launch_state,
    write_launch_state,
)


def test_write_and_read_round_trips(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OMNIGENT_OPENCODE_NATIVE_STATE_DIR", str(tmp_path / "state"))
    write_launch_state("conv_abc", "/repo")
    state = read_launch_state("conv_abc")
    assert state is not None
    assert state.working_directory == "/repo"


def test_missing_state_is_none(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OMNIGENT_OPENCODE_NATIVE_STATE_DIR", str(tmp_path / "state"))
    assert read_launch_state("nope") is None


def test_path_hashes_conversation_id(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    monkeypatch.setenv("OMNIGENT_OPENCODE_NATIVE_STATE_DIR", str(state_root))
    conversation_id = "../../../etc/passwd"
    digest = hashlib.sha256(conversation_id.encode("utf-8")).hexdigest()[:32]
    write_launch_state(conversation_id, "/repo")
    assert (state_root / digest / "launch.json").is_file()
    assert not (tmp_path / "etc").exists()


def test_relative_path_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OMNIGENT_OPENCODE_NATIVE_STATE_DIR", str(tmp_path / "state"))
    with pytest.raises(ValueError, match="absolute path"):
        write_launch_state("conv_abc", "relative/dir")


def test_conflicting_write_keeps_original(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("OMNIGENT_OPENCODE_NATIVE_STATE_DIR", str(tmp_path / "state"))
    logging.getLogger("omnigent").propagate = True
    write_launch_state("conv_abc", "/original")
    with caplog.at_level(logging.WARNING):
        write_launch_state("conv_abc", "/other")
    state = read_launch_state("conv_abc")
    assert state is not None
    assert state.working_directory == "/original"
    assert any("launch state mismatch" in r.message for r in caplog.records)


def test_malformed_state_is_none(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    monkeypatch.setenv("OMNIGENT_OPENCODE_NATIVE_STATE_DIR", str(state_root))
    digest = hashlib.sha256(b"conv_abc").hexdigest()[:32]
    target = state_root / digest
    target.mkdir(parents=True)
    (target / "launch.json").write_text("{bad", encoding="utf-8")
    assert read_launch_state("conv_abc") is None
