"""E2E (backend): a Cursor approval verdict sent to a torn-down TUI pane.

Journey the user hits (no live cursor-agent needed to exercise the failing
code path — see below):

1. A cursor-native session is working in its embedded terminal.
2. ``cursor-agent`` raises a per-tool approval prompt, which the runner-side
   mirror (:mod:`omnigent.harnesses.cursor_native.permissions`) surfaces as a
   web ``ApprovalCard``.
3. The session's terminal pane exits / is torn down (session end, runner
   disconnect, or the pane simply dying) — so its ``tmux`` socket is gone.
4. The user clicks **Reject** on the still-parked card. The mirror delivers the
   decline sequence ``("Escape", "Enter")`` to the pane via
   :func:`omnigent.harnesses.cursor_native.permissions._send_cursor_keys`.

Because the pane's socket no longer exists,
:func:`omnigent.harnesses.cursor_native.bridge.send_cursor_pane_keys` runs
``tmux send-keys`` against a dead socket and ``_run_tmux`` raises::

    RuntimeError: tmux command failed (rc=1): error connecting to
    <tmpdir>/tmux.sock (No such file or directory)

On the buggy build ``_send_cursor_keys`` catches that and logs it at **ERROR**
as the tracked signature::

    failed to send cursor keystroke 'Escape' (of ('Escape', 'Enter')); session=...

then returns ``False`` (aborting before ``Enter``) — i.e. a plain teardown /
disconnect consequence is surfaced as an ERROR-level defect signature instead
of being handled as an expected consequence of the pane going away.

The failing delivery path (bridge + real ``tmux``) is entirely independent of
whether ``cursor-agent`` is logged in — it just sends keys to whatever pane the
bridge advertises — so this drives the **real** product functions against a
**real** ``tmux`` pane that is then torn down, reproducing the reported error
faithfully without a Cursor account.

Assertion (keyed to the tracked ERROR signature, fix-agnostic): delivering an
approval verdict to a cursor session whose TUI pane has already exited must NOT
emit the ERROR-level ``failed to send cursor keystroke`` record. This FAILS on
the buggy build (the ERROR is logged) and must PASS once the dead-pane teardown
case is handled as a non-error condition.

Gated: needs a real ``tmux`` on ``PATH`` (the runner-owned TUI pane transport);
skipped (not failed) otherwise.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import time
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
    reason="cursor-native keystroke delivery needs a real `tmux` on PATH.",
)

# The tracked ERROR signature: the message prefix `_send_cursor_keys` emits when
# a keystroke cannot be delivered.
_ERROR_SIGNATURE = "failed to send cursor keystroke"
# The exact decline sequence `_run_one_approval` sends for a Reject verdict
# (`prompt.decline_key` == "Escape", then "Enter" to submit an empty reason).
_DECLINE_SEQUENCE = ("Escape", "Enter")
# A representative cursor chat/session id.
_SESSION_ID = "1080588264878826"
# Logger `_send_cursor_keys` uses (module logger); the ERROR record lands here.
_PERMISSIONS_LOGGER = "omnigent.harnesses.cursor_native.permissions"


def _kill_tmux_socket(socket_path: Path) -> None:
    """Tear the pane down: kill the tmux server and remove its socket file."""
    subprocess.run(
        ["tmux", "-S", str(socket_path), "kill-server"],
        check=False,
        capture_output=True,
    )
    if socket_path.exists():
        socket_path.unlink()


@pytest.fixture
def cursor_pane_then_torn_down() -> Iterator[tuple[Path, Path]]:
    """A real cursor-native bridge dir + tmux pane, torn down mid-test.

    Yields ``(bridge_dir, socket_path)`` with a live pane advertised in
    ``tmux.json`` exactly as the runner's launch path does
    (:func:`omnigent.harnesses.cursor_native.bridge.write_tmux_target`). The
    test kills the socket to reach the reported teardown state; the fixture's
    teardown is idempotent (kills again + removes the temp dirs).
    """
    # Shape the socket dir like the runner's (`omnigent-terminal-*`).
    socket_dir = Path(tempfile.mkdtemp(prefix="omnigent-terminal-"))
    socket_path = socket_dir / "tmux.sock"
    bridge_dir = Path(tempfile.mkdtemp(prefix="cursor-native-bridge-"))
    subprocess.run(
        ["tmux", "-S", str(socket_path), "new-session", "-d", "-s", "cursor", "sleep 600"],
        check=True,
        capture_output=True,
    )
    write_tmux_target(bridge_dir, socket_path=socket_path, tmux_target="cursor")
    try:
        yield bridge_dir, socket_path
    finally:
        _kill_tmux_socket(socket_path)
        shutil.rmtree(socket_dir, ignore_errors=True)
        shutil.rmtree(bridge_dir, ignore_errors=True)


async def test_cursor_decline_verdict_to_dead_pane_is_not_an_error_signature(
    cursor_pane_then_torn_down: tuple[Path, Path],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A Reject verdict to a torn-down cursor pane must not log the tracked ERROR.

    With the pane's tmux socket gone (a teardown / disconnect consequence),
    delivering the ``("Escape", "Enter")`` decline sequence emitted the tracked
    ERROR signature on the buggy build. After a fix that treats a dead pane as
    a non-error teardown case, no such ERROR is logged.
    """
    bridge_dir, socket_path = cursor_pane_then_torn_down

    # Control: while the pane is live, the bridge advertises it and a real
    # keystroke lands — so the failure below is the teardown, not a broken rig.
    assert read_tmux_info(bridge_dir) is not None
    send_cursor_pane_keys(bridge_dir, "Escape")

    # The reported trigger: the pane exits / is torn down, so its socket is gone.
    _kill_tmux_socket(socket_path)
    time.sleep(0.2)

    # The user's Reject verdict is delivered to the (now dead) pane, exactly as
    # `_run_one_approval` does for a decline: send "Escape", then "Enter".
    with caplog.at_level(logging.ERROR, logger=_PERMISSIONS_LOGGER):
        delivered = await _send_cursor_keys(bridge_dir, _SESSION_ID, *_DECLINE_SEQUENCE)

    # The keystroke genuinely cannot reach a pane that no longer exists — that
    # much is expected on both the buggy and fixed builds.
    assert delivered is False

    # THE BUG: the teardown consequence is surfaced as the tracked ERROR
    # signature. Fails on the buggy build (the ERROR is present); must pass
    # once a dead pane is handled without emitting this Omnigent-owned ERROR.
    kpi_errors = [
        record
        for record in caplog.records
        if record.levelno >= logging.ERROR
        and record.name == _PERMISSIONS_LOGGER
        and _ERROR_SIGNATURE in record.getMessage()
    ]
    assert not kpi_errors, (
        "delivering a Cursor approval verdict to a torn-down TUI pane logged "
        f"the tracked ERROR signature ({_ERROR_SIGNATURE!r}) instead of "
        "handling the dead pane as a teardown consequence: "
        f"{kpi_errors[0].getMessage()!r}"
    )
