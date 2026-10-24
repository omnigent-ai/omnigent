"""A browser clipboard paste reaches a real tmux pane as one bracketed block."""

import base64
import json
import shlex
import sys
from pathlib import Path

from playwright.sync_api import Page

from tests.e2e_ui.shells.test_terminal_cmd_line_editing import (
    _await_shell_ready,
    _capture_attach_frames,
    _connected_shell_textarea,
)


def test_multiline_clipboard_reaches_tmux_as_one_block(
    page: Page, terminal_session: tuple[str, str], tmp_path: Path
) -> None:
    base_url, session_id = terminal_session
    frames = _capture_attach_frames(page)
    page.goto(f"{base_url}/c/{session_id}")
    textarea = _connected_shell_textarea(page)
    _await_shell_ready(page, textarea, tmp_path)
    sink = tmp_path / "paste-bytes"
    ready = tmp_path / "paste-ready"
    page.keyboard.type(
        "stty raw -echo; printf '\\033[?2004h'; "
        f"touch {shlex.quote(str(ready))}; exec cat > {shlex.quote(str(sink))}"
    )
    page.keyboard.press("Enter")
    for _ in range(100):
        if ready.exists():
            break
        page.wait_for_timeout(100)
    assert ready.exists(), "raw input receiver did not start"

    text = "first line café\nsecond line"
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    page.evaluate("text => navigator.clipboard.writeText(text)", text)
    textarea.press("Meta+V" if sys.platform == "darwin" else "Control+Shift+V")
    expected = b"\x1b[200~" + text.replace("\n", "\r").encode() + b"\x1b[201~"
    for _ in range(100):
        if sink.exists() and sink.read_bytes() == expected:
            break
        page.wait_for_timeout(100)
    assert sink.read_bytes() == expected
    pastes = [json.loads(frame) for frame in frames if frame.startswith(b'{"type":"paste"')]
    assert len(pastes) == 1
    assert base64.b64decode(pastes[0]["data"]).decode() == text
