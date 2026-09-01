"""End-to-end regression for read-only-client chat delivery.

On the claude-native harness, web-UI chat messages are delivered by
injecting keystrokes into the harness tmux pane
(:func:`omnigent.claude_native_bridge.inject_user_message`). tmux 3.5+
(the reporter ran 3.7b) rejects every ``send-keys`` invocation with
``client is read-only`` while the session's attached clients are all
read-only — exactly the state the web Terminal viewer creates by
attaching a read-only control-mode client (``tmux -C attach -r``, see
``omnigent.terminals.control_bridge``). The message text lands (``load-buffer``
/ ``paste-buffer`` are unaffected) but the clear keystrokes and the
submit ``Enter`` go through ``send-keys`` and fail, surfacing in chat as::

    Error · execution · RuntimeError
    inner executor error: tmux command failed (rc=1): client is read-only

CI ships tmux 3.4, which does not enforce the rejection, so a shim on
``PATH`` enforces the newer tmux's documented behaviour on top of the
real binary: ``send-keys`` exits 1 with ``client is read-only`` on
stderr whenever the target socket has attached clients and none of them
is writable; every other command passes through unchanged. Everything
else is real: a real tmux server, a real pane rendering Claude Code's
composer chrome, and a real read-only control-mode client attached the
same way the web attach route does.

The delivery tests assert the FIXED behaviour (the message is delivered
despite the read-only viewer), so they fail on the current build — that
failing run is the reproduction — and flip to passing once delivery no
longer depends on the attached client's writability. The workaround
test documents the asymmetry from the report (Terminal view works while
Chat fails): with a writable client attached, delivery succeeds today.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from omnigent.claude_native_bridge import inject_user_message

_REAL_TMUX = shutil.which("tmux")

pytestmark = pytest.mark.skipif(_REAL_TMUX is None, reason="tmux is required")

# Enforce tmux >= 3.5 semantics (the reporter's 3.7b): reject ``send-keys``
# while the socket's attached clients are all read-only. Passes every other
# command — and ``send-keys`` with a writable client attached or no client at
# all — through to the real tmux, matching the tmux-level matrix in the
# reported failure (load-buffer/paste-buffer ok, send-keys rejected).
_TMUX_SHIM = """#!/usr/bin/env bash
REAL="{real_tmux}"
sock=""
prev=""
for a in "$@"; do
  if [ "$prev" = "-S" ]; then sock="$a"; fi
  prev="$a"
done
for a in "$@"; do
  if [ "$a" = "send-keys" ] && [ -n "$sock" ]; then
    clients=$("$REAL" -S "$sock" list-clients -F '#{{client_readonly}}' 2>/dev/null)
    if [ -n "$clients" ] && ! printf '%s\\n' "$clients" | grep -q '^0$'; then
      echo "client is read-only" >&2
      exit 1
    fi
  fi
done
exec "$REAL" "$@"
"""

# Paints the chrome ``inject_user_message``'s readiness gate looks for: a
# box rule directly above a row led by the composer glyph. The pane then
# stays alive so the tmux session (and its attached clients) persist.
_BOX_RULE = "\\u2500" * 10
_FAKE_CLAUDE_TUI = f"printf '{_BOX_RULE}\\n\\u276f \\n{_BOX_RULE}\\n'; sleep 600"

_TMUX_TARGET = "main"

# The delivery path polls for a paste-commit and a submit-clear it can never
# observe against the static fake pane, then falls through to the blind
# submit; keep the overall gate budget above those internal timeouts.
_DELIVERY_TIMEOUT_S = 20.0


def _attach_control_client(sock: str, *, read_only: bool) -> subprocess.Popen[bytes]:
    """Attach a control-mode client exactly as the web terminal attach does.

    Mirrors ``bridge_tmux_control_to_websocket``'s argv:
    ``tmux -S <sock> -f /dev/null -C attach [-r] -t <target>``.

    :param sock: The tmux server socket path.
    :param read_only: Attach with ``-r`` (the web viewer's mode).
    :returns: The attached client process; the caller must terminate it.
    """
    assert _REAL_TMUX is not None
    argv = [_REAL_TMUX, "-S", sock, "-f", "/dev/null", "-C", "attach"]
    if read_only:
        argv.append("-r")
    argv += ["-t", _TMUX_TARGET]
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        listing = subprocess.run(
            [_REAL_TMUX, "-S", sock, "list-clients", "-F", "#{client_readonly}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if listing.stdout.strip():
            return proc
        time.sleep(0.05)
    proc.terminate()
    raise RuntimeError("control-mode client never attached")


@pytest.fixture
def claude_native_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Path, str]]:
    """A live claude-native-shaped tmux session under 3.7b send-keys rules.

    Yields ``(bridge_dir, socket_path)``: a real tmux server whose ``main``
    session renders Claude Code's composer chrome, advertised via
    ``tmux.json`` the way the runner does, with the send-keys-rejecting
    shim first on ``PATH`` for the bridge's subprocess calls.
    """
    assert _REAL_TMUX is not None
    sock = str(tmp_path / "tmux.sock")
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    (bridge_dir / "tmux.json").write_text(
        json.dumps({"socket_path": sock, "tmux_target": _TMUX_TARGET})
    )

    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    shim = shim_dir / "tmux"
    shim.write_text(_TMUX_SHIM.format(real_tmux=_REAL_TMUX))
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim_dir}:{Path(_REAL_TMUX).parent}")

    subprocess.run(
        [_REAL_TMUX, "-S", sock, "new-session", "-d", "-s", _TMUX_TARGET, _FAKE_CLAUDE_TUI],
        check=True,
    )
    try:
        yield bridge_dir, sock
    finally:
        subprocess.run([_REAL_TMUX, "-S", sock, "kill-server"], check=False)


def test_chat_delivery_survives_sole_readonly_viewer(
    claude_native_session: tuple[Path, str],
) -> None:
    """A web-UI chat send must deliver while only a read-only viewer is attached.

    This is the reported journey: the session's sole attached tmux client
    is the web Terminal viewer's read-only control-mode client, and the
    user sends a chat message. On the current build the delivery path's
    first ``send-keys`` (the input-clear ``C-a``) is rejected and the send
    raises ``tmux command failed (rc=1): client is read-only``.
    """
    bridge_dir, sock = claude_native_session
    viewer = _attach_control_client(sock, read_only=True)
    try:
        inject_user_message(
            bridge_dir,
            content="hello from the web UI",
            timeout_s=_DELIVERY_TIMEOUT_S,
        )
    finally:
        viewer.terminate()
        viewer.wait(timeout=5)


def test_chat_delivery_survives_readonly_viewer_after_reload(
    claude_native_session: tuple[Path, str],
) -> None:
    """A browser reload (fresh read-only viewer) must not wedge chat delivery.

    The report's second symptom: reloading the tab re-attaches as another
    read-only control-mode client (the attach route recomputes ``read_only``
    from the request; no write role is reassigned), so the retry send fails
    identically. Detach the viewer, attach a fresh one, and send — the
    message must deliver.
    """
    bridge_dir, sock = claude_native_session
    first_tab = _attach_control_client(sock, read_only=True)
    first_tab.terminate()
    first_tab.wait(timeout=5)
    reloaded_tab = _attach_control_client(sock, read_only=True)
    try:
        inject_user_message(
            bridge_dir,
            content="resend after reload",
            timeout_s=_DELIVERY_TIMEOUT_S,
        )
    finally:
        reloaded_tab.terminate()
        reloaded_tab.wait(timeout=5)


def test_chat_delivers_when_writable_terminal_client_attached(
    claude_native_session: tuple[Path, str],
) -> None:
    """With a writable client attached, chat delivery works on today's build.

    The report's asymmetry (Terminal view works while Chat fails): opening
    the Terminal view in write mode attaches a read-write client, which
    satisfies tmux's ``send-keys`` writability check, so the same chat send
    goes through. Guards the workaround while the fix lands and pins the
    tmux-level cause (client writability, not the delivery payload).
    """
    bridge_dir, sock = claude_native_session
    viewer = _attach_control_client(sock, read_only=True)
    driver = _attach_control_client(sock, read_only=False)
    try:
        inject_user_message(
            bridge_dir,
            content="send with a writable terminal attached",
            timeout_s=_DELIVERY_TIMEOUT_S,
        )
    finally:
        driver.terminate()
        driver.wait(timeout=5)
        viewer.terminate()
        viewer.wait(timeout=5)
