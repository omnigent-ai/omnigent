"""End-to-end repro: claude-native web chat dies when the pane's only tmux client is read-only.

Reported journey: on the ``claude-native`` harness, web-UI **chat** input
stops working with::

    Error - execution - RuntimeError
    inner executor error: tmux command failed (rc=1): client is read-only

while the adjacent **Terminal** view keeps working, and a browser reload does
not recover (a fresh tab re-attaches as another read-only viewer).

Mechanism, confirmed live against tmux 3.7b (the reporter's version): the web
Terminal view attaches a **control-mode viewer** with the product argv
``tmux -S <sock> -f /dev/null -C attach -r`` (see
``omnigent/terminals/control_bridge.py``); its client flags are
``attached,focused,control-mode,ignore-size,read-only,UTF-8`` -- exactly the
report's ``list-clients`` output. From tmux 3.5 on, ``send-keys`` resolves the
session's attached client and **refuses with rc=1 "client is read-only"**
when that sole client is read-only (``load-buffer``/``paste-buffer`` are
unaffected). Web chat delivery (``inject_user_message`` ->
``_paste_and_submit``) issues ``send-keys`` for the draft-clear (C-a/C-k) and
the submit Enter, so the whole delivery fails and the turn surfaces the
reported executor error. A writable client attached to the same session (what
an owner's Terminal view normally holds) makes ``send-keys`` succeed -- the
reported workaround.

This test drives the REAL product path a web-UI chat send executes on the
harness side -- ``ClaudeNativeExecutor.run_turn`` -> ``inject_user_message``
-> tmux -- against a real tmux server on a private socket, advertised through
the production ``write_tmux_target``. Only the Claude Code binary is
substituted (claude-native needs an interactive Claude login that cannot be
relocated into CI; see ``test_host_claude_native_e2e.py``): the pane runs a
stand-in TUI that renders the framed ``❯`` composer the bridge's readiness
gate looks for and records every stdin line it receives.

Contract asserted (fails on unfixed main, passes once delivery no longer
depends on the attached client's read-only state):

* a chat send with a **sole read-only viewer** attached must be delivered;
* a chat send after a **viewer reconnect** (browser reload: fresh read-only
  viewer) must be delivered;
* a chat send with a **writable client also attached** keeps working (the
  workaround path a fix must not regress).

Requires a tmux whose ``send-keys`` enforces client read-onlyness (>= 3.5;
the CI-image 3.4 does not refuse and cannot exhibit the bug). Point
``OMNIGENT_E2E_TMUX`` at such a binary when the ``tmux`` on PATH is older.

Excluded from default ``pytest`` runs via ``--ignore=tests/e2e``. Invoke with::

    OMNIGENT_E2E_TMUX=/path/to/tmux-3.5-or-newer \\
    pytest tests/e2e/test_claude_native_readonly_client_e2e.py -v --timeout=300
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import shutil
import subprocess
import sys
import textwrap
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from omnigent.harnesses.claude_native.bridge import _BRIDGE_ROOT, write_tmux_target
from omnigent.inner.claude_native_executor import ClaudeNativeExecutor
from omnigent.inner.executor import ExecutorError, TurnComplete

_TMUX_SESSION = "main"
_MESSAGE = "readonly-client delivery probe: reply with the single word pong"
# First tmux release whose ``send-keys`` refuses when the session's attached
# client is read-only. Older tmux (e.g. 3.4) accepts the keystrokes, so the
# reported failure cannot exist there.
_READONLY_REFUSAL_FLOOR = (3, 5)
# Room for the bridge's paste-commit poll (5s) + submit + sink flush.
_DELIVERY_GRACE_S = 20.0
_POLL_S = 0.25

pytestmark = [
    pytest.mark.timeout(300, method="signal"),
]

# Stand-in for the Claude Code TUI: renders the framed ``❯`` composer row the
# bridge's readiness gate (``_wait_for_claude_prompt_ready``) looks for --
# the row directly under a box rule, led by the prompt glyph -- then appends
# every received stdin line to a sink file. The pty is left in cooked mode, so
# both a ``send-keys Enter`` (CR) and a pasted raw CR terminate a line.
_STANDIN_TUI = textwrap.dedent(
    """
    import sys

    sink_path = sys.argv[1]
    print("\\u2500" * 60)
    print("\\u276f ")
    print("\\u2500" * 60)
    sys.stdout.flush()
    with open(sink_path, "a", encoding="utf-8") as sink:
        for line in sys.stdin:
            sink.write(line)
            sink.flush()
    """
)


def _resolve_tmux() -> str:
    """Return the tmux binary to test with, or skip when none qualifies.

    ``OMNIGENT_E2E_TMUX`` overrides PATH so CI (whose image tmux may predate
    the read-only refusal) can point at a newer binary.
    """
    override = os.environ.get("OMNIGENT_E2E_TMUX", "").strip()
    tmux = override or shutil.which("tmux")
    if not tmux or not Path(tmux).exists():
        pytest.skip("requires tmux (or OMNIGENT_E2E_TMUX pointing at one)")
    version_line = subprocess.run(
        [tmux, "-V"], capture_output=True, text=True, timeout=10
    ).stdout.strip()
    match = re.search(r"(\d+)\.(\d+)", version_line)
    if match is None or (int(match.group(1)), int(match.group(2))) < _READONLY_REFUSAL_FLOOR:
        pytest.skip(
            f"{version_line or tmux!r} predates the send-keys read-only refusal "
            f"(needs >= {'.'.join(map(str, _READONLY_REFUSAL_FLOOR))}); set "
            "OMNIGENT_E2E_TMUX to a newer tmux to run this test"
        )
    return tmux


@dataclass
class _ClaudePane:
    """A live stand-in Claude pane advertised through the production bridge."""

    tmux: str
    socket_path: Path
    bridge_dir: Path
    sink: Path
    viewers: list[subprocess.Popen[bytes]]

    def run_tmux(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.tmux, "-S", str(self.socket_path), *args],
            check=check,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def attach_client(self, *, read_only: bool) -> subprocess.Popen[bytes]:
        """Attach a control-mode client exactly as the product's web bridge does.

        ``omnigent/terminals/control_bridge.py`` spawns
        ``tmux -S <sock> -f /dev/null -C attach [-r] -t <target>`` for every
        web Terminal-view WebSocket; ``-r`` is the read-only viewer.
        """
        argv = [self.tmux, "-S", str(self.socket_path), "-f", "/dev/null", "-C", "attach"]
        if read_only:
            argv.append("-r")
        argv += ["-t", _TMUX_SESSION]
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.viewers.append(proc)
        self._wait_for_client_count(len([v for v in self.viewers if v.poll() is None]))
        return proc

    def detach_client(self, proc: subprocess.Popen[bytes]) -> None:
        """Close one attached client (what closing/reloading the tab does)."""
        proc.terminate()
        proc.wait(timeout=10)
        self._wait_for_client_count(len([v for v in self.viewers if v.poll() is None]))

    def client_flags(self) -> str:
        return self.run_tmux(
            "list-clients", "-F", "ro=#{client_readonly} flags=[#{client_flags}]"
        ).stdout.strip()

    def _wait_for_client_count(self, expected: int) -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            listed = self.run_tmux("list-clients", "-F", "x", check=False).stdout
            if len(listed.splitlines()) == expected:
                return
            time.sleep(_POLL_S)
        raise AssertionError(
            f"tmux never settled at {expected} attached client(s); clients:\n"
            f"{self.client_flags()}"
        )


@pytest.fixture
def claude_pane(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_ClaudePane]:
    """A real tmux pane running the stand-in Claude TUI, bridge-advertised.

    The chosen tmux is symlinked first on ``PATH`` so the bridge's own
    ``_run_tmux`` subprocesses (which resolve ``tmux`` from ``PATH``) use the
    same binary the fixture drives.
    """
    tmux = _resolve_tmux()
    bin_dir = tmp_path / "tmuxbin"
    bin_dir.mkdir()
    (bin_dir / "tmux").symlink_to(tmux)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    socket_path = tmp_path / "tmux.sock"
    sink = tmp_path / "received.txt"
    standin = tmp_path / "standin_tui.py"
    standin.write_text(_STANDIN_TUI, encoding="utf-8")
    launch = f"{shlex.quote(sys.executable)} {shlex.quote(str(standin))} {shlex.quote(str(sink))}"
    subprocess.run(
        [
            tmux,
            "-S",
            str(socket_path),
            "new-session",
            "-d",
            "-s",
            _TMUX_SESSION,
            "-x",
            "100",
            "-y",
            "30",
            launch,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    # The bridge validates its dir sits under the trusted claude-native root,
    # so the fixture cannot use tmp_path for it.
    bridge_dir = _BRIDGE_ROOT / f"readonly-client-test-{uuid.uuid4().hex}"
    # What the runner does at Claude terminal-create time.
    write_tmux_target(bridge_dir, socket_path=socket_path, tmux_target=_TMUX_SESSION)
    pane = _ClaudePane(
        tmux=tmux, socket_path=socket_path, bridge_dir=bridge_dir, sink=sink, viewers=[]
    )
    try:
        yield pane
    finally:
        for viewer in pane.viewers:
            if viewer.poll() is None:
                viewer.kill()
                viewer.wait(timeout=10)
        subprocess.run(
            [tmux, "-S", str(socket_path), "kill-server"],
            check=False,
            capture_output=True,
            timeout=10,
        )
        shutil.rmtree(bridge_dir, ignore_errors=True)


async def _run_one_turn(executor: ClaudeNativeExecutor) -> list[Any]:
    events: list[Any] = []
    async for event in executor.run_turn(
        messages=[{"role": "user", "content": _MESSAGE}],
        tools=[],
        system_prompt="",
    ):
        events.append(event)
    return events


def _assert_chat_message_delivered(pane: _ClaudePane) -> None:
    """Send one web-UI chat message and require it to reach the Claude TUI.

    What a web chat send executes on the harness side. On unfixed main, with
    the pane's only client(s) read-only, ``inject_user_message``'s first
    ``send-keys`` is refused and the turn yields the reported
    ``ExecutorError`` -- this assertion then fails with that error text.
    """
    executor = ClaudeNativeExecutor(bridge_dir=pane.bridge_dir)
    events = asyncio.run(_run_one_turn(executor))

    errors = [e for e in events if isinstance(e, ExecutorError)]
    assert not errors, (
        "web-UI chat send failed on the claude-native harness side "
        f"(the web UI renders this as 'inner executor error: ...'):\n"
        f"  {errors[0].message}\n"
        f"attached tmux clients:\n{pane.client_flags()}"
    )
    assert any(isinstance(e, TurnComplete) for e in events), (
        f"turn yielded neither TurnComplete nor ExecutorError: {events!r}"
    )

    deadline = time.monotonic() + _DELIVERY_GRACE_S
    received = ""
    while time.monotonic() < deadline:
        received = pane.sink.read_text(encoding="utf-8") if pane.sink.exists() else ""
        if _MESSAGE in received:
            return
        time.sleep(_POLL_S)
    raise AssertionError(
        "turn reported success but the Claude TUI never received the message.\n"
        f"TUI received: {received!r}\n"
        f"attached tmux clients:\n{pane.client_flags()}"
    )


def test_chat_delivery_succeeds_with_sole_readonly_viewer(claude_pane: _ClaudePane) -> None:
    """A web chat send must be delivered while only a read-only viewer is attached.

    The reported state: the session's only attached client is the web
    Terminal view's read-only control-mode viewer (client flags
    ``attached,...,control-mode,ignore-size,read-only,UTF-8``). On unfixed
    main every ``send-keys`` is refused with rc=1 "client is read-only" and
    the chat turn errors -- chat is unusable while the terminal pane itself
    keeps working.
    """
    claude_pane.attach_client(read_only=True)
    _assert_chat_message_delivered(claude_pane)


def test_chat_delivery_succeeds_after_viewer_reconnect(claude_pane: _ClaudePane) -> None:
    """A browser reload (fresh read-only viewer) must not leave chat wedged.

    The report's second claim: reloading the tab does not recover -- the
    fresh tab re-attaches as another read-only control-mode viewer and the
    resend fails identically. A fix must make delivery independent of the
    reconnected viewer's read-only state.
    """
    first = claude_pane.attach_client(read_only=True)
    claude_pane.detach_client(first)
    claude_pane.attach_client(read_only=True)
    _assert_chat_message_delivered(claude_pane)


def test_chat_delivery_keeps_working_with_writable_client_attached(
    claude_pane: _ClaudePane,
) -> None:
    """The workaround path -- a writable client attached -- must stay working.

    With a read-write client attached alongside the viewer (what an owner's
    Terminal view holds when the write role isn't stuck), ``send-keys``
    succeeds and chat delivers even on unfixed main. Green before and after a
    fix; pins that a fix cannot regress the healthy topology.
    """
    claude_pane.attach_client(read_only=True)
    claude_pane.attach_client(read_only=False)
    _assert_chat_message_delivered(claude_pane)
