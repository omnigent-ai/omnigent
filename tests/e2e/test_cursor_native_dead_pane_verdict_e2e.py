"""E2E guard: a Cursor approval verdict delivered to a torn-down TUI pane must
not be logged as an operational error.

The cursor-native harness answers a web approval card by sending tmux keystrokes
into the runner-owned pane that hosts cursor-agent. When that pane has already
exited (session teardown, runner disconnect, or the user ending the run) its
tmux socket is gone, so the keystroke cannot land. That is expected teardown:
dropping the verdict is the only possible outcome and belongs below ERROR, not
at it.

This drives the real bridge + permissions delivery path against a real tmux pane
that is created, advertised exactly as the runner advertises it, and then torn
down mid-flight. Delivering the decline sequence (Escape, Enter) that a Reject
verdict sends must report the keystroke as undelivered without logging the
tracked error signature.
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

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None,
    reason="requires the tmux binary to stand up and tear down a real cursor pane",
)

_PERMISSIONS_LOGGER = "omnigent.harnesses.cursor_native.permissions"
_ERROR_SIGNATURE = "failed to send cursor keystroke"
_SESSION_ID = "1080588264878826"
# What _run_one_approval sends for a Reject verdict: the decline key then Enter.
_DECLINE_SEQUENCE = ("Escape", "Enter")


class _RecordCollector(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _kill_tmux_pane(socket_path: Path) -> None:
    subprocess.run(
        ["tmux", "-S", str(socket_path), "kill-server"],
        check=False,
        capture_output=True,
    )
    socket_path.unlink(missing_ok=True)


@pytest.fixture
def cursor_bridge_with_live_pane() -> Iterator[tuple[Path, Path]]:
    """A cursor-native bridge dir advertising a real, live tmux pane."""
    socket_dir = Path(tempfile.mkdtemp(prefix="omnigent-terminal-"))
    bridge_dir = Path(tempfile.mkdtemp(prefix="cursor-native-bridge-"))
    socket_path = socket_dir / "tmux.sock"
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
        shutil.rmtree(socket_dir, ignore_errors=True)
        shutil.rmtree(bridge_dir, ignore_errors=True)


async def test_cursor_decline_verdict_to_dead_pane_is_not_an_error_signature(
    cursor_bridge_with_live_pane: tuple[Path, Path],
) -> None:
    bridge_dir, socket_path = cursor_bridge_with_live_pane

    # The pane is advertised and a verdict lands while it is alive.
    assert read_tmux_info(bridge_dir) is not None
    send_cursor_pane_keys(bridge_dir, "Escape")

    # The pane exits before the web Reject verdict is delivered.
    _kill_tmux_pane(socket_path)
    assert not socket_path.exists()

    collector = _RecordCollector()
    logger = logging.getLogger(_PERMISSIONS_LOGGER)
    previous_level = logger.level
    logger.addHandler(collector)
    logger.setLevel(logging.DEBUG)
    try:
        delivered = await _send_cursor_keys(bridge_dir, _SESSION_ID, *_DECLINE_SEQUENCE)
    finally:
        logger.removeHandler(collector)
        logger.setLevel(previous_level)

    assert delivered is False

    error_signatures = [
        record
        for record in collector.records
        if record.levelno >= logging.ERROR and _ERROR_SIGNATURE in record.getMessage()
    ]
    assert not error_signatures, (
        "delivering a Cursor decline verdict (Escape, Enter) to a torn-down tmux "
        f"pane logged the tracked error signature {_ERROR_SIGNATURE!r}; a gone pane "
        "is expected teardown and the dropped keystroke should be recorded below "
        f"ERROR: {[r.getMessage() for r in error_signatures]}"
    )
