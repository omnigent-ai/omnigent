"""The embedded ``omnigent attach`` REPL must stop painting once its last client leaves.

The runner's start-on-attach hook table signals ``omnigent-clients-changed`` on
every attach and on the last detach, and the REPL pauses rendering while
``#{session_attached}`` is zero: a turn arriving then must not repaint the
unviewed pane, and it must appear once a client attaches again.

These checks observe the pane's tmux session and the REPL process directly, so
the e2e server, runner and REPL must share this test's PID namespace and TMPDIR
(run without ``dev.repro_env exec``). The attach uses the same ``tmux -C attach``
the server's ``bridge_tmux_control_to_websocket`` runs for a web terminal client.
"""

from __future__ import annotations

import contextlib
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import psutil
import pytest

from tests.e2e_ui.conftest import (
    _CUSTOM_AGENT_MODEL,
    configure_mock_llm,
    set_fallback_mock_llm,
)

_TMUX = shutil.which("tmux")
_REPL_TERMINAL_ID_PREFIX = "terminal_tui_"
# The REPL banner renders the echo_probe agent's name with a space.
_CUSTOM_AGENT_NAME_LABEL = "echo probe"


def _tmux(socket: Path, *args: str) -> str:
    assert _TMUX is not None, "tmux is required"
    proc = subprocess.run(
        [_TMUX, "-S", str(socket), "-f", "/dev/null", *args],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"tmux {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


@dataclass(frozen=True)
class ReplPane:
    """The REPL terminal's private tmux server and session."""

    socket: Path
    session_name: str

    def _format(self, fmt: str) -> str:
        return _tmux(self.socket, "display-message", "-p", "-t", self.session_name, fmt).strip()

    def attached_clients(self) -> int:
        return int(self._format("#{session_attached}"))

    def hooks(self) -> str:
        return _tmux(self.socket, "show-hooks", "-g")

    def capture(self) -> str:
        return _tmux(self.socket, "capture-pane", "-p", "-t", self.session_name)

    def send_enter(self) -> None:
        _tmux(self.socket, "send-keys", "-t", self.session_name, "Enter")


def _hook_command(hooks: str, name: str) -> str | None:
    """The command bound to global hook *name*, or ``None`` when it is unset.

    ``show-hooks -g`` prints the whole hook table; a set hook reads
    ``name[index] <command>`` while an unset one is just the bare name.
    """
    for line in hooks.splitlines():
        stripped = line.strip()
        if stripped == name:
            return None
        if stripped.startswith(f"{name}["):
            _, _, command = stripped.partition("]")
            return command.strip()
    return None


def find_repl_pane(session_id: str, *, timeout_s: float = 60.0) -> ReplPane:
    """Locate the tmux session whose pane command is ``omnigent attach <session_id>``."""
    deadline = time.monotonic() + timeout_s
    seen: list[str] = []
    while True:
        for socket in Path(tempfile.gettempdir()).glob("omnigent-terminal-*/tmux.sock"):
            try:
                listing = _tmux(
                    socket, "list-panes", "-a", "-F", "#{session_name}\t#{pane_start_command}"
                )
            except (RuntimeError, OSError, subprocess.TimeoutExpired):
                continue
            for line in listing.splitlines():
                name, _, start_command = line.partition("\t")
                seen.append(f"{socket}: {line}")
                if f"attach {session_id}" in start_command:
                    return ReplPane(socket=socket, session_name=name)
        if time.monotonic() > deadline:
            raise TimeoutError(f"no REPL pane for {session_id}; panes seen: {seen[-10:]}")
        time.sleep(0.5)


def repl_process(session_id: str) -> psutil.Process | None:
    """The ``omnigent attach <session_id>`` process, or ``None`` while still gated."""
    for proc in psutil.process_iter(["cmdline"]):
        cmdline = proc.info["cmdline"] or []
        if "attach" in cmdline and session_id in cmdline and any("omnigent" in c for c in cmdline):
            if Path(cmdline[0]).name in ("sh", "bash", "zsh", "dash"):
                continue
            return proc
    return None


def wait_for_repl_terminal(
    base_url: str, session_id: str, *, timeout_s: float = 60.0
) -> dict[str, Any]:
    """Poll the session's resources until the runner has registered its REPL terminal."""
    deadline = time.monotonic() + timeout_s
    last: Any = None
    while time.monotonic() < deadline:
        resp = httpx.get(f"{base_url}/v1/sessions/{session_id}/resources", timeout=10.0)
        if resp.status_code == 200:
            last = resp.json()
            rows = last.get("data", last) if isinstance(last, dict) else last
            for row in rows:
                if row.get("type") == "terminal" and str(row.get("id", "")).startswith(
                    _REPL_TERMINAL_ID_PREFIX
                ):
                    return row
        time.sleep(0.5)
    raise TimeoutError(f"REPL terminal never appeared; last resources payload: {last!r}")


@contextlib.contextmanager
def control_attach(pane: ReplPane) -> Iterator[subprocess.Popen[bytes]]:
    """Attach to the REPL pane in tmux control mode, as the web terminal bridge does.

    Attaching fires the ``client-attached`` hook that releases the start gate;
    terminating the client is the "last client leaves" event a closing tab causes.
    """
    assert _TMUX is not None
    proc = subprocess.Popen(
        [
            _TMUX,
            "-S",
            str(pane.socket),
            "-f",
            "/dev/null",
            "-C",
            "attach",
            "-t",
            pane.session_name,
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        yield proc
    finally:
        with contextlib.suppress(Exception):
            proc.terminate()
        with contextlib.suppress(Exception):
            proc.wait(timeout=10)


def _wait_until(predicate: Callable[[], bool], *, timeout_s: float, what: str) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.25)
    raise TimeoutError(f"timed out waiting for {what}")


def _wait_until_settled(pane: ReplPane, *, interval_s: float, timeout_s: float, what: str) -> str:
    """Return the pane contents once two captures *interval_s* apart match."""
    deadline = time.monotonic() + timeout_s
    previous = pane.capture()
    while True:
        time.sleep(interval_s)
        current = pane.capture()
        if current == previous:
            return current
        if time.monotonic() > deadline:
            raise TimeoutError(f"timed out waiting for {what}")
        previous = current


def _accept_first_run_theme_picker(pane: ReplPane) -> bool:
    """Accept the first-run theme picker a fresh (theme-less) e2e home shows."""
    if "Choose your theme" not in pane.capture():
        return False
    pane.send_enter()
    _wait_until(
        lambda: "Choose your theme" not in pane.capture(),
        timeout_s=15.0,
        what="the first-run theme picker to close",
    )
    return True


def _post_user_turn(base_url: str, session_id: str, text: str) -> httpx.Response:
    body = {
        "type": "message",
        "data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
    }
    resp = httpx.post(f"{base_url}/v1/sessions/{session_id}/events", json=body, timeout=15.0)
    resp.raise_for_status()
    return resp


def _token_committed(base_url: str, session_id: str, token: str) -> bool:
    """Whether *token* is present in the session's committed transcript."""
    resp = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/items",
        params={"limit": 100, "order": "asc"},
        timeout=10.0,
    )
    resp.raise_for_status()
    return token in resp.text


@pytest.mark.timeout(240)
def test_detached_repl_stops_rendering_after_last_client_leaves(
    custom_agent_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """A turn arriving with no client attached is painted only once one returns.

    Journey: a terminal client attaches (REPL starts), detaches (its tab
    closes), a later turn arrives, and a client attaches again. The posted
    turn's token must never reach the unviewed pane, then must show up on
    re-attach.
    """
    base_url, session_id = custom_agent_session
    wait_for_repl_terminal(base_url, session_id)
    pane = find_repl_pane(session_id)

    # Precondition: the REPL is still gated — no client, no process yet.
    assert pane.attached_clients() == 0
    assert repl_process(session_id) is None

    # The user opens the terminal: the first client attach releases the gate.
    with control_attach(pane):
        _wait_until(
            lambda: pane.attached_clients() >= 1,
            timeout_s=20.0,
            what="first terminal client to attach",
        )
        _wait_until(
            lambda: repl_process(session_id) is not None,
            timeout_s=30.0,
            what="REPL process to start after first attach",
        )
        proc = repl_process(session_id)
        assert proc is not None
        _wait_until(
            lambda: pane.capture().strip() != "",
            timeout_s=20.0,
            what="REPL to paint its first screen",
        )
        _accept_first_run_theme_picker(pane)
        _wait_until(
            lambda: _CUSTOM_AGENT_NAME_LABEL in pane.capture(),
            timeout_s=30.0,
            what="REPL to reach its live session view",
        )

    # The user leaves: the last client detaches when the tab closes.
    _wait_until(
        lambda: pane.attached_clients() == 0,
        timeout_s=20.0,
        what="the last terminal client to detach",
    )
    assert proc.is_running(), "the REPL exited on detach; this journey needs it still alive"

    # A later turn arrives while nobody is attached. Its unique token can reach
    # the pane only if the detached REPL repaints for the turn's SSE events —
    # nothing is ever typed into it.
    pane_before = pane.capture()
    nonce = f"detached{uuid.uuid4().hex[:8]}"
    reply = f"REPLYTOKEN{nonce}"
    set_fallback_mock_llm(mock_llm_server_url, _CUSTOM_AGENT_MODEL, "")
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": reply, "stream": True}],
        key=nonce,
        match=nonce,
    )
    # The REPL pauses asynchronously after tmux reports the detach, so wait for
    # the pane to stop changing before snapshotting it. A correctly paused REPL
    # repaints nothing for the turn, so the snapshot stays byte-identical even
    # where the toolbar's context-usage ring would redraw.
    pane_detached = _wait_until_settled(
        pane, interval_s=1.0, timeout_s=15.0, what="the detached pane to settle"
    )
    # The prompt names only the nonce, never the reply token, so the token can
    # reach the transcript or the pane only as the assistant's painted response
    # -- not by echoing the user's own text.
    _post_user_turn(base_url, session_id, f"turn {nonce}. Reply with your assigned token.")

    # The turn really ran: the assistant-only token is in the committed
    # transcript, so a genuine session event occurred that a correctly
    # paused/stopped REPL would ignore.
    _wait_until(
        lambda: _token_committed(base_url, session_id, reply),
        timeout_s=60.0,
        what="the posted turn to commit server-side",
    )

    # Give an orphaned REPL ample time to repaint. A paused/stopped one never will.
    rendered = False
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        if reply in pane.capture():
            rendered = True
            break
        time.sleep(0.3)

    try:
        cpu_total: float | None = sum(proc.cpu_times()[:2])
    except psutil.NoSuchProcess:
        cpu_total = None
    pane_after = pane.capture()
    attached = pane.attached_clients()
    assert attached == 0, f"a client re-attached ({attached}); the detached condition was lost"
    assert not rendered, (
        "the detached REPL repainted the posted turn into the pane with zero clients "
        f"attached (session_attached={attached}, repl pid={proc.pid} still running, "
        f"client-detached hook={_hook_command(pane.hooks(), 'client-detached')!r}, "
        f"cpu_seconds={cpu_total}); it keeps full-rendering every session event for "
        "the rest of the session after the last web client leaves.\n"
        f"pane_before:\n{pane_before}\npane_after:\n{pane_after}"
    )
    # Nothing on the paused pane may change for the turn -- not even the toolbar
    # context-usage ring, which a bypassed invalidate would redraw while detached.
    assert pane_after == pane_detached, (
        "the detached REPL repainted the pane for a background session event with "
        f"zero clients attached (repl pid={proc.pid}); a paused pane must not change "
        f"until a viewer returns.\npane_detached:\n{pane_detached}\npane_after:\n{pane_after}"
    )

    # The user reopens the terminal: the turn queued while detached is painted.
    with control_attach(pane):
        _wait_until(
            lambda: pane.attached_clients() >= 1,
            timeout_s=20.0,
            what="a terminal client to re-attach",
        )
        _wait_until(
            lambda: reply in pane.capture(),
            timeout_s=30.0,
            what="the turn received while detached to be painted after re-attach",
        )
    assert proc.is_running()
