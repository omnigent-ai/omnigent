"""The REPL idle-toolbar wait must survive prompt_toolkit's partial repaints."""

from __future__ import annotations

import sys
import textwrap

import pytest

from tests.e2e._repl_toolbar import expect_idle_toolbar, track_toolbar

pexpect = pytest.importorskip("pexpect")

# Paints a toolbar like the REPL's, then repaints it the way prompt_toolkit
# does: only changed cells, skipping unchanged ones with cursor-forward moves.
_FAKE_REPL = textwrap.dedent(
    r"""
    import sys, time
    out = sys.stdout
    out.write("\x1b[2J\x1b[H")
    out.write("hello world · ready  /help help · Ctrl+C exit  state: sleeping\r\n")
    out.flush()
    sys.stdin.readline()
    out.write("\x1b[H\x1b[14Cstreaming  /help help · Ctrl+C exit  state: running \r\n")
    out.flush()
    time.sleep(0.3)
    # Idle again: "hello world · " and the space after "state:" are unchanged.
    out.write("\x1b[H\x1b[14Cready      /help help · Ctrl+C exit  state:\x1b[Csleeping\r\n")
    out.flush()
    time.sleep(10)
    """
)


def _spawn() -> pexpect.spawn:
    return pexpect.spawn(
        sys.executable,
        ["-c", _FAKE_REPL],
        encoding="utf-8",
        dimensions=(10, 120),
        timeout=10,
    )


def test_idle_toolbar_matches_a_partial_repaint() -> None:
    """A repaint the raw ``· ready`` regex cannot match still reads idle, after the turn."""
    child = _spawn()
    try:
        expect_idle_toolbar(child, timeout=5)
        child.sendline("go")
        expect_idle_toolbar(child, timeout=5)
        # The stale pre-send idle toolbar must not end the wait early.
        assert "running" in child.before
    finally:
        child.close(force=True)


def test_raw_ready_regex_misses_the_same_repaint() -> None:
    """Guard the fixture: the raw-stream regex times out on this repaint."""
    child = _spawn()
    try:
        track_toolbar(child)
        child.expect(r"·\s*ready", timeout=5)
        child.sendline("go")
        with pytest.raises(pexpect.TIMEOUT):
            child.expect(r"·\s*ready", timeout=2)
    finally:
        child.close(force=True)
