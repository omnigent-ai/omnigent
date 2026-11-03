"""Filesystem bridge + tmux delivery for the bob-native terminal harness.

The runner launches ``bob chat`` in a private tmux pane and records that pane's
socket + target here (:func:`write_tmux_target`). The harness executor delivers
web-UI messages into the same pane with a bracketed paste and one Enter.

Bob's folder-trust, license, team-picker and tool-approval prompts are
selection dialogs whose highlighted option is accepted by Enter (e.g. "Trust
folder", "Approve Once"), and Escape on the trust dialog exits Bob. So every
keystroke this module sends is gated on :func:`classify_bob_pane` reporting the
composer, never a dialog: the user answers Bob's own prompts in its terminal.
"""

from __future__ import annotations

import contextlib
import enum
import hashlib
import itertools
import json
import os
import re
import subprocess
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from omnigent._platform import stable_user_id

if TYPE_CHECKING:
    from omnigent.inner.terminal import TerminalInstance

#: Env var carrying the bridge dir into the harness executor process.
BRIDGE_DIR_ENV_VAR = "HARNESS_BOB_NATIVE_BRIDGE_DIR"

_BRIDGE_ROOT = Path(tempfile.gettempdir()) / f"omnigent-{stable_user_id()}" / "bob-native"
_TMUX_FILE = "tmux.json"
_TMUX_READY_TIMEOUT_S = 30.0
_TMUX_SEND_TIMEOUT_S = 10.0
_POLL_INTERVAL_S = 0.2
_PASTE_SETTLE_S = 0.3
_PASTE_COMMIT_TIMEOUT_S = 5.0
_PASTE_BUFFER = "omnigent-bob-paste"
# Only the bottom of the pane holds the live composer or dialog; older
# scrollback can contain a finished dialog's selector line.
_PANE_TAIL_LINES = 40

# Locale-independent structure of Bob Shell 2.x's Ink TUI (verified on 2.0.5):
# the composer is a ``❯`` row directly under a horizontal rule, and every
# selection dialog renders a ``↑↓ (n/m)`` position indicator.
_RULE_RE = re.compile(r"^\s*─{8,}\s*$")
_COMPOSER_RE = re.compile(r"^\s*❯(?:\s|$)")
_SELECTOR_RE = re.compile(r"↑↓\s*\(\d+/\d+\)")


# Environment the ``bob chat`` child receives (the terminal does not inherit the
# runner env). Bob is a Node CLI: it needs PATH/HOME/locale/terminal basics plus
# the proxy and corporate-CA variables IBM documents for restricted networks.
# Bob's own ``BOB_API_KEY`` is deliberately absent: through Omnigent, Bob uses
# the IBMid login it stores under ``~/.bob``, so no credential is exposed to the
# long-lived pane environment.
_CHILD_ENV_ALLOWLIST: tuple[str, ...] = (
    "BOB_LOG_LEVEL",
    "COLORTERM",
    "HOME",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LOGNAME",
    "NODE_EXTRA_CA_CERTS",
    "NO_COLOR",
    "NO_PROXY",
    "PATH",
    "SHELL",
    "TERM",
    "TMPDIR",
    "USER",
    "https_proxy",
    "http_proxy",
    "no_proxy",
)


class BobPaneState(enum.Enum):
    """What the visible Bob pane is waiting for."""

    READY = "ready"  # composer accepts input (idle, or mid-turn steering)
    DIALOG = "dialog"  # trust / license / team / tool-approval selection
    STARTING = "starting"  # splash, browser sign-in, or no pane yet


_DIALOG_MESSAGE = (
    "Bob is waiting on a prompt in its terminal (folder trust, license, team, "
    "or a tool approval). Answer it in the Bob terminal, then send again."
)
_NOT_READY_MESSAGE = (
    "Bob's input box is not ready yet (still starting, or waiting for sign-in "
    "in the browser). Finish setup in the Bob terminal, then send again."
)


def classify_bob_pane(pane: str | None) -> BobPaneState:
    """Classify a captured Bob pane.

    :param pane: Plain ``capture-pane -p`` text, or ``None``/``""``.
    :returns: :attr:`BobPaneState.DIALOG` when a selection dialog is visible,
        :attr:`BobPaneState.READY` when the composer is visible, else
        :attr:`BobPaneState.STARTING`.
    """
    if not pane:
        return BobPaneState.STARTING
    lines = [line for line in pane.splitlines() if line.strip()][-_PANE_TAIL_LINES:]
    if any(_SELECTOR_RE.search(line) for line in lines):
        return BobPaneState.DIALOG
    for previous, line in itertools.pairwise(lines):
        if _RULE_RE.match(previous) and _COMPOSER_RE.match(line):
            return BobPaneState.READY
    return BobPaneState.STARTING


def bridge_dir_for_session_id(session_id: str) -> Path:
    """Return the per-session bridge dir, e.g. ``/tmp/omnigent-<uid>/bob-native/<hash>``."""
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
    return _BRIDGE_ROOT / digest


def bridge_root() -> Path:
    """Return the bob-native bridge root."""
    return _BRIDGE_ROOT


def _ensure_dir(path: Path) -> None:
    """Create *path* (and parents) with owner-only permissions."""
    path.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o700)


def build_bob_native_spawn_env(session_id: str) -> dict[str, str]:
    """Build the harness spawn env: only the bridge dir.

    Bob owns its auth (IBMid sign-in in the TUI, stored under ``~/.bob``), so
    Omnigent adds no credential or vendor setting.

    :param session_id: The Omnigent session id (keys the bridge dir).
    :returns: ``{BRIDGE_DIR_ENV_VAR: <dir>}``.
    """
    bridge_dir = bridge_dir_for_session_id(session_id)
    _ensure_dir(bridge_dir)
    return {BRIDGE_DIR_ENV_VAR: str(bridge_dir)}


def build_bob_native_terminal_env(source_env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return the allowlisted environment for the ``bob chat`` terminal.

    :param source_env: Environment to filter; defaults to ``os.environ``.
    :returns: Only the :data:`_CHILD_ENV_ALLOWLIST` entries that are set.
    """
    env = os.environ if source_env is None else source_env
    return {key: env[key] for key in _CHILD_ENV_ALLOWLIST if env.get(key)}


def write_tmux_target(bridge_dir: Path, *, socket_path: Path, tmux_target: str) -> None:
    """Advertise the tmux socket + target for the running Bob terminal."""
    _ensure_dir(bridge_dir)
    payload = {
        "socket_path": str(socket_path),
        "tmux_target": tmux_target,
        "updated_at": time.time(),
    }
    tmp = bridge_dir / (_TMUX_FILE + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, bridge_dir / _TMUX_FILE)


def read_tmux_info(bridge_dir: Path) -> dict[str, str] | None:
    """Return ``{socket_path, tmux_target}`` from ``tmux.json``, or ``None``."""
    try:
        data = json.loads((bridge_dir / _TMUX_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    socket_path = data.get("socket_path")
    tmux_target = data.get("tmux_target")
    if (
        isinstance(socket_path, str)
        and socket_path
        and isinstance(tmux_target, str)
        and tmux_target
    ):
        return {"socket_path": socket_path, "tmux_target": tmux_target}
    return None


def _wait_for_tmux_info(bridge_dir: Path, *, timeout_s: float) -> dict[str, str]:
    """Block until ``tmux.json`` is advertised, or raise on timeout."""
    deadline = time.monotonic() + timeout_s
    while True:
        info = read_tmux_info(bridge_dir)
        if info is not None:
            return info
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"bob-native tmux target was not advertised within {timeout_s:.0f}s"
            )
        time.sleep(_POLL_INTERVAL_S)


def _run_tmux(socket_path: str, *args: str) -> None:
    """Invoke ``tmux -S <socket> <args...>`` and raise on failure."""
    try:
        proc = subprocess.run(
            ["tmux", "-S", socket_path, *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=_TMUX_SEND_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise RuntimeError(f"tmux command failed: {exc}") from exc
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "<no output>"
        raise RuntimeError(f"tmux command failed (rc={proc.returncode}): {detail}")


def _capture_pane(socket_path: str, tmux_target: str) -> str:
    """Capture the visible pane; ``""`` on any failure (treated as not ready)."""
    try:
        proc = subprocess.run(
            ["tmux", "-S", socket_path, "capture-pane", "-p", "-t", tmux_target],
            check=False,
            capture_output=True,
            text=True,
            timeout=_TMUX_SEND_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, OSError):
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def _session_alive(socket_path: str, tmux_target: str) -> bool:
    """Return whether the tmux pane still exists (Bob is running)."""
    try:
        proc = subprocess.run(
            ["tmux", "-S", socket_path, "has-session", "-t", tmux_target],
            check=False,
            capture_output=True,
            timeout=_TMUX_SEND_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return proc.returncode == 0


def _live_tmux_info(bridge_dir: Path, *, timeout_s: float) -> tuple[str, str]:
    """Return the live ``(socket_path, tmux_target)`` or raise if Bob exited."""
    info = _wait_for_tmux_info(bridge_dir, timeout_s=timeout_s)
    socket_path, tmux_target = info["socket_path"], info["tmux_target"]
    if not _session_alive(socket_path, tmux_target):
        raise RuntimeError("The Bob terminal is no longer running; restart the session.")
    return socket_path, tmux_target


def _wait_for_composer(socket_path: str, tmux_target: str, *, timeout_s: float) -> None:
    """Block until the composer is visible; raise if a dialog or startup persists."""
    deadline = time.monotonic() + timeout_s
    while True:
        state = classify_bob_pane(_capture_pane(socket_path, tmux_target))
        if state is BobPaneState.READY:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(
                _DIALOG_MESSAGE if state is BobPaneState.DIALOG else _NOT_READY_MESSAGE
            )
        time.sleep(_POLL_INTERVAL_S)


def _paste_payload_bytes(text: str) -> bytes:
    """Encode text for ``tmux load-buffer``: newlines → CR, other control bytes dropped.

    A stray ESC would end the bracketed paste early and reach Bob as a key.
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    body = bytearray()
    for ch in normalized:
        if ch == "\n":
            body.append(0x0D)
        elif ch == "\t":
            body.append(0x09)
        elif ord(ch) >= 0x20:
            body.extend(ch.encode("utf-8"))
    return bytes(body)


def _submit_needle(content: str) -> str:
    """Return a short tail substring that confirms the paste rendered."""
    for line in reversed(content.splitlines()):
        stripped = line.strip()
        if len(stripped) >= 4:
            return stripped[-24:]
    return ""


def inject_user_message(
    bridge_dir: Path,
    *,
    content: str,
    timeout_s: float = _TMUX_READY_TIMEOUT_S,
) -> None:
    """Deliver a web-UI message into Bob's composer and submit it.

    Stages *content* in a tmux buffer, waits for the composer (refusing while
    any Bob dialog is up), re-checks it immediately before one bracketed paste,
    then re-checks again before the single Enter. Mid-turn this is Bob's own
    "Enter to steer". tmux cannot make check-and-paste atomic, so a dialog that
    opens in the few milliseconds between the last capture and the paste can
    still receive the pasted text; Enter is never sent in that case. An unsent
    draft already in the composer is kept: Bob's clear key (Ctrl+C) also
    interrupts a running turn.

    :param bridge_dir: The bob-native bridge dir holding ``tmux.json``.
    :param content: Non-empty user text.
    :param timeout_s: Readiness timeout for the tmux target and the composer.
    :raises RuntimeError: If Bob exited, a dialog or startup screen persists,
        or a tmux command fails.
    """
    if not content.strip():
        raise RuntimeError("bob-native delivery requires non-empty content")
    socket_path, tmux_target = _live_tmux_info(bridge_dir, timeout_s=timeout_s)
    with tempfile.NamedTemporaryFile(
        dir=bridge_dir, prefix="paste_", suffix=".bin", delete=False
    ) as paste_file:
        paste_file.write(_paste_payload_bytes(content))
        paste_path = paste_file.name
    try:
        _run_tmux(socket_path, "load-buffer", "-b", _PASTE_BUFFER, paste_path)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(paste_path)
    try:
        _wait_for_composer(socket_path, tmux_target, timeout_s=timeout_s)
        # Last capture before input: keep the check-to-paste window minimal.
        if classify_bob_pane(_capture_pane(socket_path, tmux_target)) is not BobPaneState.READY:
            raise RuntimeError(_DIALOG_MESSAGE)
    except RuntimeError:
        with contextlib.suppress(RuntimeError):
            _run_tmux(socket_path, "delete-buffer", "-b", _PASTE_BUFFER)
        raise
    _run_tmux(socket_path, "paste-buffer", "-p", "-d", "-b", _PASTE_BUFFER, "-t", tmux_target)
    needle = _submit_needle(content)
    if needle:
        deadline = time.monotonic() + _PASTE_COMMIT_TIMEOUT_S
        while time.monotonic() < deadline:
            if needle in _capture_pane(socket_path, tmux_target):
                break
            time.sleep(_POLL_INTERVAL_S)
    time.sleep(_PASTE_SETTLE_S)
    # Re-check right before Enter: a tool approval can open while pasting.
    if classify_bob_pane(_capture_pane(socket_path, tmux_target)) is not BobPaneState.READY:
        raise RuntimeError(_DIALOG_MESSAGE)
    _run_tmux(socket_path, "send-keys", "-t", tmux_target, "Enter")


def inject_interrupt(bridge_dir: Path, *, timeout_s: float = _TMUX_READY_TIMEOUT_S) -> None:
    """Interrupt Bob's running response with Escape (Bob's documented key).

    Sent only while the composer is positively visible: on a dialog or the
    startup/sign-in screen, Escape exits Bob or dismisses an unanswered prompt.

    :raises RuntimeError: If Bob exited, the composer is not visible, or tmux fails.
    """
    socket_path, tmux_target = _live_tmux_info(bridge_dir, timeout_s=timeout_s)
    state = classify_bob_pane(_capture_pane(socket_path, tmux_target))
    if state is not BobPaneState.READY:
        raise RuntimeError(_DIALOG_MESSAGE if state is BobPaneState.DIALOG else _NOT_READY_MESSAGE)
    _run_tmux(socket_path, "send-keys", "-t", tmux_target, "Escape")


def kill_session(bridge_dir: Path, *, timeout_s: float = _TMUX_READY_TIMEOUT_S) -> None:
    """Hard-stop Bob by killing its tmux session (the web "Stop session").

    :raises RuntimeError: If the tmux target is not advertised or tmux fails.
    """
    info = _wait_for_tmux_info(bridge_dir, timeout_s=timeout_s)
    _run_tmux(info["socket_path"], "kill-session", "-t", info["tmux_target"])


def native_input_ready(session_id: str, instance: TerminalInstance) -> bool:
    """Provider ``input_ready_probe``: ready once Bob's composer is visible."""
    del session_id
    return classify_bob_pane(instance.last_pane_text()) is BobPaneState.READY
