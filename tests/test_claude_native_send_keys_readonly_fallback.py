"""Keystroke delivery must survive a read-only-only tmux client set.

tmux >= 3.5 rejects ``send-keys`` with ``client is read-only`` while every
attached client is read-only — the state the web Terminal viewer creates by
attaching a read-only control-mode client (``tmux -C attach -r``). The
bridge's keystroke sender falls back to ``load-buffer`` + ``paste-buffer``
(no client-writability gate) so web-driven input keeps working regardless
of who is watching the pane.

CI ships tmux 3.4, which does not enforce the rejection, so a shim on
``PATH`` enforces the newer tmux's documented behaviour on top of the real
binary (same approach as the e2e reproduction in
``tests/e2e/test_claude_native_readonly_client_e2e.py``).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
import unittest.mock
from collections.abc import Iterator
from pathlib import Path

import pytest

from omnigent import claude_native_bridge
from omnigent.claude_native_bridge import (
    _key_fallback_bytes,
    _send_keys,
    inject_interrupt,
)

_REAL_TMUX = shutil.which("tmux")

pytestmark = pytest.mark.skipif(_REAL_TMUX is None, reason="tmux is required")

# Reject ``send-keys`` while the socket's attached clients are all read-only;
# pass every other command through to the real tmux (tmux >= 3.5 semantics).
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

_TMUX_TARGET = "main"


def _attach_readonly_client(sock: str) -> subprocess.Popen[bytes]:
    """Attach a read-only control-mode client the way the web viewer does.

    :param sock: The tmux server socket path.
    :returns: The attached client process; the caller must terminate it.
    """
    assert _REAL_TMUX is not None
    proc = subprocess.Popen(
        [_REAL_TMUX, "-S", sock, "-f", "/dev/null", "-C", "attach", "-r", "-t", _TMUX_TARGET],
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
def readonly_only_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[str, subprocess.Popen[bytes], Path]]:
    """A tmux session whose sole client is read-only, under 3.5+ rules.

    Yields ``(socket_path, viewer, sink)``: the pane runs ``cat`` into
    *sink*, so every keystroke that reaches the pane is observable as
    file content.
    """
    assert _REAL_TMUX is not None
    sock = str(tmp_path / "tmux.sock")
    sink = tmp_path / "pane-input.txt"

    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    shim = shim_dir / "tmux"
    shim.write_text(_TMUX_SHIM.format(real_tmux=_REAL_TMUX))
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim_dir}:{Path(_REAL_TMUX).parent}")

    subprocess.run(
        [_REAL_TMUX, "-S", sock, "new-session", "-d", "-s", _TMUX_TARGET, f"cat > {sink}"],
        check=True,
    )
    viewer = _attach_readonly_client(sock)
    try:
        yield sock, viewer, sink
    finally:
        viewer.terminate()
        viewer.wait(timeout=5)
        subprocess.run([_REAL_TMUX, "-S", sock, "kill-server"], check=False)


def _wait_for_sink(sink: Path, expected: str, timeout_s: float = 5.0) -> str:
    """Poll *sink* until it contains *expected* or the timeout lapses."""
    deadline = time.monotonic() + timeout_s
    content = ""
    while time.monotonic() < deadline:
        if sink.exists():
            content = sink.read_text()
            if expected in content:
                return content
        time.sleep(0.05)
    return content


def test_keystrokes_reach_pane_despite_sole_readonly_client(
    readonly_only_session: tuple[str, subprocess.Popen[bytes], Path],
) -> None:
    """Literal text + Enter must land in the pane via the buffer fallback.

    ``send-keys`` is rejected (``client is read-only``); the fallback
    must deliver the same bytes, observable as the pane program's input.
    """
    sock, _viewer, sink = readonly_only_session
    _send_keys(sock, _TMUX_TARGET, "hello fallback", literal=True)
    _send_keys(sock, _TMUX_TARGET, "Enter")
    # The pane's tty maps the Enter CR to NL on input, so a trailing
    # newline proves the separate Enter send arrived too.
    content = _wait_for_sink(sink, "hello fallback\n")
    assert "hello fallback\n" in content


def test_interrupt_survives_sole_readonly_viewer(
    readonly_only_session: tuple[str, subprocess.Popen[bytes], Path],
    tmp_path: Path,
) -> None:
    """The web stop button (Escape injection) must not require a writable client."""
    sock, _viewer, _sink = readonly_only_session
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    (bridge_dir / "tmux.json").write_text(
        json.dumps({"socket_path": sock, "tmux_target": _TMUX_TARGET})
    )
    inject_interrupt(bridge_dir, timeout_s=5.0)


def test_non_readonly_tmux_errors_still_raise(
    readonly_only_session: tuple[str, subprocess.Popen[bytes], Path],
) -> None:
    """Only the read-only rejection triggers the fallback; real errors stay loud."""
    sock, _viewer, _sink = readonly_only_session
    with pytest.raises(RuntimeError, match="tmux command failed"):
        _send_keys(sock, "no-such-session", "Enter")


def test_unknown_key_name_fails_loud() -> None:
    """A key name without a byte fallback must raise, not deliver wrong bytes."""
    with pytest.raises(RuntimeError, match="no raw-byte fallback"):
        _key_fallback_bytes(("F12",), literal=False)


def test_fallback_buffer_names_are_unique_per_send(
    readonly_only_session: tuple[str, subprocess.Popen[bytes], Path],
) -> None:
    """Each fallback delivery must use its own tmux buffer name.

    Interrupts, dialog confirms, and the model-change endpoint can
    inject concurrently on one session; a shared buffer name would let
    one ``load-buffer`` overwrite another's bytes between its load and
    paste, delivering the wrong keystroke.
    """
    sock, _viewer, _sink = readonly_only_session
    seen: list[str] = []
    real_run_tmux = claude_native_bridge._run_tmux

    def spy(socket_path: str, *args: str) -> None:
        if args and args[0] == "load-buffer":
            seen.append(args[2])
        real_run_tmux(socket_path, *args)

    with unittest.mock.patch.object(claude_native_bridge, "_run_tmux", spy):
        _send_keys(sock, _TMUX_TARGET, "Enter")
        _send_keys(sock, _TMUX_TARGET, "Enter")
    assert len(seen) == 2
    assert seen[0] != seen[1]


def test_key_fallback_bytes_match_terminal_sequences() -> None:
    """The fallback bytes are exactly what send-keys would have written."""
    assert _key_fallback_bytes(("C-a", "C-k"), literal=False) == b"\x01\x0b"
    assert _key_fallback_bytes(("Enter",), literal=False) == b"\r"
    assert _key_fallback_bytes(("Escape",), literal=False) == b"\x1b"
    assert _key_fallback_bytes(("BTab",), literal=False) == b"\x1b[Z"
    assert _key_fallback_bytes(("/effort high",), literal=True) == b"/effort high"
