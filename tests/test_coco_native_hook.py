"""Tests for the coco-native lifecycle-event hook."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from omnigent import coco_native_hook


def _feed_stdin(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw))


def _events(bridge_dir: Path) -> list[object]:
    path = bridge_dir / coco_native_hook.HOOK_EVENTS_FILE
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_main_appends_payload_as_one_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid JSON payload is appended verbatim as one JSONL record."""
    payload = {
        "hook_event_name": "SessionStart",
        "session_id": "coco-1234",
        "transcript_path": "/tmp/conversations/coco-1234.json",
    }
    _feed_stdin(monkeypatch, json.dumps(payload))

    exit_code = coco_native_hook.main(["--bridge-dir", str(tmp_path)])

    assert exit_code == 0
    assert _events(tmp_path) == [payload]


def test_main_appends_across_invocations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The events file is append-only: successive hooks add lines in order."""
    _feed_stdin(monkeypatch, json.dumps({"hook_event_name": "UserPromptSubmit"}))
    assert coco_native_hook.main(["--bridge-dir", str(tmp_path)]) == 0
    _feed_stdin(monkeypatch, json.dumps({"hook_event_name": "Stop"}))
    assert coco_native_hook.main(["--bridge-dir", str(tmp_path)]) == 0

    assert _events(tmp_path) == [
        {"hook_event_name": "UserPromptSubmit"},
        {"hook_event_name": "Stop"},
    ]


def test_main_empty_stdin_writes_empty_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty stdin still exits 0 and records an empty payload."""
    _feed_stdin(monkeypatch, "")

    assert coco_native_hook.main(["--bridge-dir", str(tmp_path)]) == 0
    assert _events(tmp_path) == [{}]


def test_main_invalid_json_never_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-JSON stdin exits 0 without raising (hook must never block the TUI)."""
    _feed_stdin(monkeypatch, "not json at all {")

    assert coco_native_hook.main(["--bridge-dir", str(tmp_path)]) == 0


def test_main_non_dict_payload_is_wrapped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A JSON payload that is not an object is wrapped as malformed_payload."""
    _feed_stdin(monkeypatch, json.dumps(["a", "list"]))

    assert coco_native_hook.main(["--bridge-dir", str(tmp_path)]) == 0
    assert _events(tmp_path) == [{"malformed_payload": '["a", "list"]'}]


def test_main_unwritable_bridge_dir_never_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing/unwritable bridge dir still exits 0 (failure stays silent)."""
    _feed_stdin(monkeypatch, json.dumps({"hook_event_name": "Stop"}))
    missing = tmp_path / "does" / "not" / "exist"

    assert coco_native_hook.main(["--bridge-dir", str(missing)]) == 0
    assert not missing.exists()
