"""Unit tests for the disk persistence helpers in ``omnigent.telemetry.installation_id``."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

import omnigent.telemetry.installation_id as id_mod


@pytest.fixture()
def telemetry_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the module at a temp file and start with an empty in-memory cache."""
    path = tmp_path / "data" / "telemetry.json"
    monkeypatch.setattr(id_mod, "_telemetry_file_path", lambda: path)
    monkeypatch.setattr(id_mod, "_cache", None)
    monkeypatch.setattr(id_mod, "_cache_initialized", False)
    return path


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


# ── _load_from_disk ──────────────────────────────────────────


def test_load_returns_none_when_file_is_absent(telemetry_file: Path) -> None:
    assert id_mod._load_from_disk() is None


@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": 1},  # key missing
        {"installation_id": ""},
        {"installation_id": 42},
        {"installation_id": "not-a-uuid"},
        ["not", "an", "object"],
    ],
)
def test_load_rejects_missing_or_invalid_ids(telemetry_file: Path, payload: object) -> None:
    _write(telemetry_file, payload)

    assert id_mod._load_from_disk() is None


def test_load_returns_stored_uuid(telemetry_file: Path) -> None:
    stored = str(uuid.uuid4())
    _write(telemetry_file, {"installation_id": stored})

    assert id_mod._load_from_disk() == stored


# ── _write_to_disk ───────────────────────────────────────────


def test_write_creates_parent_dirs_and_leaves_no_temp_file(telemetry_file: Path) -> None:
    new_id = str(uuid.uuid4())

    id_mod._write_to_disk(new_id)

    data = json.loads(telemetry_file.read_text(encoding="utf-8"))
    assert data["installation_id"] == new_id
    assert data["schema_version"] == 1
    assert {"created_at", "created_version"} <= set(data)
    assert not telemetry_file.with_suffix(".tmp").exists()


def test_write_failure_is_swallowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory", encoding="utf-8")
    monkeypatch.setattr(id_mod, "_telemetry_file_path", lambda: blocker / "telemetry.json")

    id_mod._write_to_disk(str(uuid.uuid4()))  # must not raise


# ── get_installation_id ──────────────────────────────────────


def test_get_installation_id_still_returns_an_id_when_persisting_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory", encoding="utf-8")
    monkeypatch.setattr(id_mod, "_telemetry_file_path", lambda: blocker / "telemetry.json")
    monkeypatch.setattr(id_mod, "_cache", None)
    monkeypatch.setattr(id_mod, "_cache_initialized", False)

    first = id_mod.get_installation_id()

    assert first is not None
    uuid.UUID(first)
    # The id is stable for the rest of the process even though nothing was saved.
    assert id_mod.get_installation_id() == first


def test_get_installation_id_returns_none_after_unexpected_error(
    monkeypatch: pytest.MonkeyPatch, telemetry_file: Path
) -> None:
    def exploding() -> str | None:
        raise RuntimeError("boom")

    monkeypatch.setattr(id_mod, "_load_from_disk", exploding)

    assert id_mod.get_installation_id() is None
    # The failure is cached so later calls don't retry the disk on every event.
    assert id_mod._cache_initialized is True
    assert id_mod.get_installation_id() is None
