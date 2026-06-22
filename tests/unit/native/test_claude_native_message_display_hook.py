"""Tests for the fast ``MessageDisplay`` deltas-appender hook."""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

import pytest

from omnigent import claude_native_message_display_hook as hook
from omnigent.claude_native_bridge import read_message_deltas_from_offset


def _run_hook(
    payload: dict[str, object], bridge_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> int:
    """
    Drive the hook ``main`` with one payload on stdin.

    :param payload: Hook JSON object to feed on stdin, e.g.
        ``{"hook_event_name": "MessageDisplay", "message_id": "m1",
        "index": 0, "final": False, "delta": "hi"}``.
    :param bridge_dir: Bridge directory the hook should append to.
    :param monkeypatch: Pytest monkeypatch fixture used to set stdin.
    :returns: The hook's process exit code.
    """
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    return hook.main(["--bridge-dir", str(bridge_dir)])


def test_message_display_hook_appends_well_formed_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A valid ``MessageDisplay`` payload is appended verbatim, in order.

    Fails if the hook drops fields, reorders, or writes a shape the
    bridge reader can't parse — which would break live streaming end
    to end (the forwarder would forward nothing).
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()

    assert (
        _run_hook(
            {
                "hook_event_name": "MessageDisplay",
                "message_id": "m1",
                "index": 0,
                "final": False,
                "delta": "Hello ",
            },
            bridge_dir,
            monkeypatch,
        )
        == 0
    )
    assert (
        _run_hook(
            {
                "hook_event_name": "MessageDisplay",
                "message_id": "m1",
                "index": 1,
                "final": True,
                "delta": "world",
            },
            bridge_dir,
            monkeypatch,
        )
        == 0
    )

    result = read_message_deltas_from_offset(bridge_dir, 0)
    # Both chunks land, in order, with every field preserved — proving
    # the on-disk shape round-trips through the reader the forwarder uses.
    assert [(d.message_id, d.index, d.final, d.delta) for d in result.deltas] == [
        ("m1", 0, False, "Hello "),
        ("m1", 1, True, "world"),
    ]


def test_message_display_hook_writes_owner_only_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    The deltas file is created with owner-only (0600) permissions.

    Fails if the streamed assistant text (same content as the message)
    becomes world-readable on a shared host.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    _run_hook(
        {"hook_event_name": "MessageDisplay", "message_id": "m1", "index": 0, "delta": "x"},
        bridge_dir,
        monkeypatch,
    )
    mode = (bridge_dir / hook.MESSAGE_DELTAS_FILE).stat().st_mode
    assert oct(mode & 0o777) == "0o600"


@pytest.mark.parametrize(
    "payload",
    [
        {"hook_event_name": "MessageDisplay", "delta": "no id"},
        {"hook_event_name": "MessageDisplay", "message_id": "", "delta": "empty id"},
        {"hook_event_name": "MessageDisplay", "message_id": "m1"},
        {"hook_event_name": "MessageDisplay", "message_id": "m1", "delta": 123},
        {"hook_event_name": "MessageDisplay", "message_id": "m1", "delta": None},
    ],
    ids=["missing-id", "empty-id", "missing-delta", "non-string-delta", "null-delta"],
)
def test_message_display_hook_skips_unforwardable_payloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: dict[str, object]
) -> None:
    """
    Payloads lacking a usable ``message_id``/``delta`` write nothing.

    Fails if the hook appends a record the forwarder couldn't turn into
    a valid delta event (e.g. an empty message id or a non-string delta),
    which would surface as a malformed SSE event downstream.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    assert _run_hook(payload, bridge_dir, monkeypatch) == 0
    # No file at all — there was nothing forwardable to record.
    assert not (bridge_dir / hook.MESSAGE_DELTAS_FILE).exists()


def test_message_display_hook_defaults_missing_index_to_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A single-chunk message with no ``index`` still forwards (index 0).

    Fails if a missing index is dropped or coerced to something the
    reader rejects, which would lose the one-and-only chunk of a short
    assistant message.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    _run_hook(
        {"hook_event_name": "MessageDisplay", "message_id": "m1", "final": True, "delta": "hi"},
        bridge_dir,
        monkeypatch,
    )
    result = read_message_deltas_from_offset(bridge_dir, 0)
    assert [(d.message_id, d.index, d.final, d.delta) for d in result.deltas] == [
        ("m1", 0, True, "hi")
    ]


def test_message_display_hook_swallows_malformed_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """
    Malformed stdin exits 0 (never blocks Claude) and writes nothing.

    Claude blocks on command hooks, so a parse error must be a silent
    no-op for the TUI; fails if the hook raises or appends garbage.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    monkeypatch.setattr(sys, "stdin", io.StringIO("{not json"))
    assert hook.main(["--bridge-dir", str(bridge_dir)]) == 0
    assert not (bridge_dir / hook.MESSAGE_DELTAS_FILE).exists()
    # Diagnostic goes to stderr so it never lands in Claude's stdout
    # (which Claude would interpret as hook output).
    assert "malformed JSON" in capsys.readouterr().err


def test_message_display_hook_many_appends_stay_line_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Many successive appends to the shared file stay newline-framed.

    Each ``MessageDisplay`` chunk is a separate hook invocation appending
    one line; this asserts every line is independently parseable and the
    reader's reported offset reaches EOF after consuming them all. Fails
    if the hook ever wrote a record without a trailing newline (which
    would make the next record's bytes glom onto it and break the
    reader's per-line decode). NOTE: this exercises sequential appends,
    not true concurrent subprocesses — the O_APPEND atomicity that makes
    real per-chunk parallelism safe is a POSIX guarantee, not asserted
    here.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    for i in range(50):
        _run_hook(
            {
                "hook_event_name": "MessageDisplay",
                "message_id": "m1",
                "index": i,
                "delta": f"c{i} ",
            },
            bridge_dir,
            monkeypatch,
        )
    result = read_message_deltas_from_offset(bridge_dir, 0)
    # All 50 chunks parse and arrive in index order — no line was torn.
    assert [d.index for d in result.deltas] == list(range(50))
    assert result.byte_offset == os.path.getsize(bridge_dir / hook.MESSAGE_DELTAS_FILE)
