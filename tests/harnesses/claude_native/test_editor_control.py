"""Private handoff framing and refusal without starting a socket server."""

from __future__ import annotations

import json
import struct
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from omnigent.harnesses.claude_native import editor_control
from omnigent.harnesses.claude_native.editor_transaction import NativeDraftEditRefused


def test_partial_frames_and_socket_timeouts_preserve_the_exact_payload() -> None:
    payload = {"token": "claim", "phase": "prepared", "text": " exact λ \n"}
    raw = json.dumps(payload).encode()
    frame = struct.pack("!I", len(raw)) + raw
    connection = MagicMock()
    connection.recv.side_effect = [
        TimeoutError(),
        *(frame[index : index + 1] for index in range(len(frame))),
    ]
    check = MagicMock()
    channel = editor_control._Channel(connection, "claim", check)
    assert channel.receive("prepared") == payload
    sent = bytearray()
    blocked = False

    def send(value: memoryview) -> int:
        nonlocal blocked
        if not blocked:
            blocked = True
            raise TimeoutError
        sent.extend(value[:3])
        return min(len(value), 3)

    connection.send.side_effect = send
    channel.send("prepared", text=payload["text"])
    assert struct.unpack("!I", sent[:4])[0] == len(sent) - 4
    assert json.loads(sent[4:]) == payload
    check.assert_called()


@pytest.mark.parametrize("invalid", ["empty", "oversized", "wrong-token", "wrong-phase", "eof"])
def test_invalid_or_abandoned_frames_cannot_advance_an_operation(invalid: str) -> None:
    connection = MagicMock()
    if invalid in {"empty", "oversized"}:
        connection.recv.return_value = struct.pack("!I", 0 if invalid == "empty" else 2**32 - 1)
    elif invalid == "eof":
        connection.recv.return_value = b""
    else:
        raw = json.dumps(
            {
                "token": "other" if invalid == "wrong-token" else "claim",
                "phase": "commit" if invalid == "wrong-phase" else "prepared",
            }
        ).encode()
        connection.recv.side_effect = [struct.pack("!I", len(raw)), raw]
    with pytest.raises(NativeDraftEditRefused):
        editor_control._Channel(connection, "claim", lambda: None).receive("prepared")
    connection.send.assert_not_called()


@pytest.mark.parametrize("claim", [None, "malformed", "public", "symlink"])
def test_unarmed_delegation_and_invalid_claims_touch_no_native_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, claim: str | None
) -> None:
    path = tmp_path / "draft.txt"
    path.write_text("preserve original draft")
    request = tmp_path / "editor-operation.json"
    if claim == "symlink":
        request.symlink_to(path)
    elif claim is not None:
        request.write_text("{" if claim == "malformed" else "{}")
        request.chmod(0o644 if claim == "public" else 0o600)
    connect = MagicMock(side_effect=AssertionError("No socket should be opened"))
    monkeypatch.setattr(editor_control, "socket", SimpleNamespace(socket=connect))
    result = editor_control.run_editor_operation(
        path, bridge_dir=tmp_path, executable=Path("/native/claude")
    )
    if claim is None:
        assert result is None
    else:
        assert result == 1
    connect.assert_not_called()
    assert path.read_text() == "preserve original draft"
