"""Tests for the machine's stored random routing key."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent import routing_key


@pytest.fixture
def key_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr("omnigent.host.local_server._local_data_dir", lambda: tmp_path)
    monkeypatch.setattr(routing_key, "_cached", None)
    return tmp_path


def test_key_is_created_once_and_reused_across_processes(key_dir: Path, monkeypatch) -> None:
    key = routing_key.client_routing_key()
    assert len(key) == 32 and int(key, 16) >= 0
    assert (key_dir / "routing_key").read_text() == key
    # A new process (empty cache) reads the stored key.
    monkeypatch.setattr(routing_key, "_cached", None)
    assert routing_key.client_routing_key() == key


@pytest.mark.parametrize("stored", [b"not-a-key", b"\xff\xfe"], ids=["text", "non-utf8"])
def test_invalid_stored_key_is_replaced(key_dir: Path, stored: bytes) -> None:
    (key_dir / "routing_key").write_bytes(stored)
    key = routing_key.client_routing_key()
    assert len(key) == 32 and (key_dir / "routing_key").read_text() == key


def test_unwritable_data_dir_keeps_a_process_key(tmp_path: Path, monkeypatch) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("")
    monkeypatch.setattr("omnigent.host.local_server._local_data_dir", lambda: blocker / "sub")
    monkeypatch.setattr(routing_key, "_cached", None)
    key = routing_key.client_routing_key()
    assert routing_key.client_routing_key() == key


def test_a_key_stored_by_another_process_first_wins(key_dir: Path) -> None:
    # Another process stored its key after this one found none; creating must adopt it.
    (key_dir / "routing_key").write_text("a" * 32)
    assert routing_key._create() == "a" * 32
    assert (key_dir / "routing_key").read_text() == "a" * 32
    assert [p.name for p in key_dir.iterdir()] == ["routing_key"]
