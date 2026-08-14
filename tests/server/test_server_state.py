"""Unit tests for server-state sidecar persistence."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from omnigent.server.server_state import (
    ServerState,
    load_server_state,
    save_server_state,
)


def test_load_missing_returns_blank_state(tmp_path: Path) -> None:
    """A missing sidecar is treated as an unknown previous state."""
    os.environ["OMNIGENT_DATA_DIR"] = str(tmp_path)
    state = load_server_state()
    assert state == ServerState()


def test_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Save then load preserves auth posture."""
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))

    expected = ServerState(last_auth_source="accounts", last_local_single_user=True)
    save_server_state(expected)
    assert load_server_state() == expected


def test_corrupt_file_returns_blank_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A corrupt sidecar is treated like a missing one so startup never fails."""
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))

    state_path = tmp_path / "server-state.json"
    state_path.write_text("not json")

    state = load_server_state()
    assert state == ServerState()


def test_legacy_keys_default_to_blank(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An old sidecar with missing keys stays forward-compatible."""
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))

    state_path = tmp_path / "server-state.json"
    state_path.write_text(json.dumps({"last_auth_source": "header"}))

    state = load_server_state()
    assert state.last_auth_source == "header"
    assert state.last_local_single_user is False
