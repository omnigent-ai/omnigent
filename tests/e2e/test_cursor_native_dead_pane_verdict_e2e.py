"""E2E guard: delivering a Cursor approval verdict to a torn-down TUI pane must
not log the tracked operational-error signature.

The harness answers a web card by sending tmux keystrokes into the runner-owned
cursor-agent pane. Once that pane exits its socket is gone, so the keystroke can
only be dropped — expected teardown that belongs below ERROR. This drives the
real bridge + permissions path against a real tmux pane that is torn down before
the Reject sequence (Escape, Enter) is delivered, asserting the sequence is
reported undelivered without the signature.
"""

import logging
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from omnigent.harnesses.cursor_native.bridge import (
    read_tmux_info,
    send_cursor_pane_keys,
    write_tmux_target,
)
from omnigent.harnesses.cursor_native.permissions import _send_cursor_keys
from omnigent.inner.terminal import tmux_reports_target_gone

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None,
    reason="requires the tmux binary to stand up and tear down a real cursor pane",
)

_PERMISSIONS_LOGGER = "omnigent.harnesses.cursor_native.permissions"
_ERROR_SIGNATURE = "failed to send cursor keystroke"
_SESSION_ID = "1080588264878826"
# What _run_one_approval sends for a Reject verdict: the decline key then Enter.
_DECLINE_SEQUENCE = ("Escape", "Enter")
# Bound the teardown call so a hung tmux surfaces instead of blocking cleanup.
_TMUX_TEARDOWN_TIMEOUT_S = 10.0


def _kill_tmux_pane(socket_path: Path) -> None:
    proc = subprocess.run(
        ["tmux", "-S", str(socket_path), "kill-server"],
        check=False,
        capture_output=True,
        text=True,
        timeout=_TMUX_TEARDOWN_TIMEOUT_S,
    )
    # Only drop the socket once the server is confirmed gone; unlinking after an
    # operational failure would strand a still-live server behind a missing socket.
    if proc.returncode != 0 and not tmux_reports_target_gone(proc.stderr.strip()):
        raise RuntimeError(f"tmux kill-server failed: {proc.stderr.strip()!r}")
    socket_path.unlink(missing_ok=True)


@pytest.fixture
def cursor_bridge_with_live_pane() -> Iterator[tuple[Path, Path]]:
    """A cursor-native bridge dir advertising a real, live tmux pane."""
    # A short /tmp path keeps the socket under the macOS Unix-socket length limit.
    with tempfile.TemporaryDirectory(prefix="og-cursor-", dir="/tmp") as directory:
        root = Path(directory)
        socket_path = root / "tmux.sock"
        bridge_dir = root / "bridge"
        bridge_dir.mkdir()
        subprocess.run(
            ["tmux", "-S", str(socket_path), "new-session", "-d", "-s", "cursor", "sleep 600"],
            check=True,
            capture_output=True,
        )
        write_tmux_target(bridge_dir, socket_path=socket_path, tmux_target="cursor")
        try:
            yield bridge_dir, socket_path
        finally:
            _kill_tmux_pane(socket_path)


async def test_cursor_decline_verdict_to_dead_pane_is_not_an_error_signature(
    cursor_bridge_with_live_pane: tuple[Path, Path],
    caplog: pytest.LogCaptureFixture,
) -> None:
    bridge_dir, socket_path = cursor_bridge_with_live_pane

    # The pane is advertised and a verdict lands while it is alive.
    assert read_tmux_info(bridge_dir) is not None
    send_cursor_pane_keys(bridge_dir, "Escape")

    # The pane exits before the web Reject verdict is delivered.
    _kill_tmux_pane(socket_path)
    assert not socket_path.exists()

    with caplog.at_level(logging.DEBUG, logger=_PERMISSIONS_LOGGER):
        delivered = await _send_cursor_keys(bridge_dir, _SESSION_ID, *_DECLINE_SEQUENCE)

    assert delivered is False

    error_signatures = [
        record
        for record in caplog.records
        if record.levelno >= logging.ERROR and _ERROR_SIGNATURE in record.getMessage()
    ]
    assert not error_signatures, (
        "delivering a Cursor decline verdict (Escape, Enter) to a torn-down tmux "
        f"pane logged the tracked error signature {_ERROR_SIGNATURE!r}; a gone pane "
        "is expected teardown and the dropped keystroke should be recorded below "
        f"ERROR: {[r.getMessage() for r in error_signatures]}"
    )
