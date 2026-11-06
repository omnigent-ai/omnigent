"""tmux bridge for the native Command Code (``cmd``) TUI.

The ``omnigent commandcode`` wrapper launches the Command Code interactive TUI
(bare ``cmd``) inside a runner-owned tmux pane. This module owns everything the
Omnigent harness needs to talk to that resident pane:

* the per-session **bridge directory** (a small scratch dir, hashed by session
  id) and the spawn-env var the executor reads to find it;
* the tmux **socket + target** handshake (the runner writes it after launch);
* **message injection** — bracketed-paste a web-UI turn into the TUI and press
  Enter, so the message appears in the running ``cmd`` session;
* **interrupt** (Escape) and **kill** for stop/teardown;
* the **hook/mod routing blob** (``hook_config.json``) the generated
  per-session Command Code mod reads to reach the Omnigent server;
* ``native_input_ready`` — the input-readiness probe the runner polls.

Command Code's TUI markers are not part of a published contract, so the pane
parser here is deliberately permissive: it treats the pane as ready when it has
settled and shows no known modal, and it recognises Command Code's numbered
approval menu generically. These markers are the one vendor-specific piece and
should be re-validated against a live ``cmd`` TUI (see the harness plan).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import gettempdir
from typing import TYPE_CHECKING

from omnigent._platform import stable_user_id
from omnigent.util.json_types import JsonObject

if TYPE_CHECKING:  # pragma: no cover - typing only
    from omnigent.inner.terminal import TerminalInstance

# The executor reads this env var (set by the spawn-env builder) to locate the
# session bridge dir. Mirrors the ``HARNESS_<NAME>_NATIVE_BRIDGE_DIR`` convention.
BRIDGE_DIR_ENV_VAR = "HARNESS_COMMANDCODE_NATIVE_BRIDGE_DIR"

_TMUX_FILE = "tmux.json"
_HOOK_CONFIG_FILE = "hook_config.json"
_INJECTION_CANCELLED_FILE = "injection.cancelled"

# Keys typed to answer Command Code's numbered permission menu.
APPROVE_KEY = "1"
DENY_KEY = "3"

_TMUX_TIMEOUT_S = 10.0
_READY_POLL_INTERVAL_S = 0.2


class CommandCodeTuiNotReadyError(RuntimeError):
    """The Command Code TUI pane is not accepting input."""


class CommandCodeApprovalPendingError(RuntimeError):
    """The pane is showing an approval prompt, so the message cannot be typed."""


@dataclass(frozen=True)
class _CommandCodePaneState:
    """Parsed view of a Command Code TUI pane."""

    lines: tuple[str, ...] = field(default_factory=tuple)
    editor_present: bool = False
    menu_present: bool = False
    trust_modal_present: bool = False

    @property
    def approval_menu_present(self) -> bool:
        """:returns: ``True`` for a permission menu (not the folder-trust modal)."""
        return self.menu_present and not self.trust_modal_present

    @property
    def ready(self) -> bool:
        """:returns: ``True`` when the idle editor is visible and no menu is open."""
        return self.editor_present and not self.menu_present


def bridge_root() -> Path:
    """:returns: Root directory holding every commandcode-native bridge dir."""
    return Path(gettempdir()) / f"omnigent-{stable_user_id()}" / "commandcode-native"


def bridge_dir_for_session_id(session_id: str) -> Path:
    """
    Return the deterministic bridge dir for *session_id*.

    :param session_id: Omnigent session/conversation id.
    :returns: Path under :func:`bridge_root` (hashed, so ids with path
        separators are safe).
    """
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
    return bridge_root() / digest


def build_commandcode_native_spawn_env(session_id: str) -> dict[str, str]:
    """
    Build the spawn env that lets the executor find the session bridge dir.

    :param session_id: Omnigent session/conversation id.
    :returns: ``{BRIDGE_DIR_ENV_VAR: <bridge dir>}``.
    """
    bridge_dir = bridge_dir_for_session_id(session_id)
    bridge_dir.mkdir(parents=True, exist_ok=True)
    return {BRIDGE_DIR_ENV_VAR: str(bridge_dir)}


# ---------------------------------------------------------------------------
# Routing blob (read by the generated per-session Command Code mod)
# ---------------------------------------------------------------------------


def write_hook_config(
    bridge_dir: Path,
    *,
    server_url: str,
    headers: dict[str, str],
    session_id: str,
) -> None:
    """Persist the Omnigent routing blob the bridge mod reads at load."""
    bridge_dir.mkdir(parents=True, exist_ok=True)
    payload: JsonObject = {
        "server_url": server_url,
        "headers": dict(headers),
        "session_id": session_id,
        "policy_evaluate_url": f"{server_url.rstrip('/')}/v1/sessions/{session_id}"
        "/policies/evaluate",
        "permission_url": f"{server_url.rstrip('/')}/v1/sessions/{session_id}"
        "/hooks/permission-request",
    }
    _atomic_write_json(bridge_dir / _HOOK_CONFIG_FILE, payload)


def read_hook_config(bridge_dir: Path) -> JsonObject | None:
    """:returns: The routing blob, or ``None`` when it has not been written."""
    return _read_json(bridge_dir / _HOOK_CONFIG_FILE)


def read_active_session_id(bridge_dir: Path) -> str | None:
    """:returns: The Omnigent session id from the routing blob, if present."""
    config = read_hook_config(bridge_dir)
    if config is None:
        return None
    value = config.get("session_id")
    return value if isinstance(value, str) and value else None


# ---------------------------------------------------------------------------
# tmux socket + target handshake
# ---------------------------------------------------------------------------


def write_tmux_target(
    bridge_dir: Path,
    *,
    socket_path: Path,
    tmux_target: str,
    pid: int | None = None,
) -> None:
    """Advertise the runner-owned pane's tmux socket + target to the executor."""
    bridge_dir.mkdir(parents=True, exist_ok=True)
    payload: JsonObject = {"socket_path": str(socket_path), "tmux_target": tmux_target}
    if pid is not None:
        payload["pid"] = pid
    _atomic_write_json(bridge_dir / _TMUX_FILE, payload)


def read_tmux_info(bridge_dir: Path) -> tuple[Path, str] | None:
    """:returns: ``(socket_path, tmux_target)`` or ``None`` when unavailable."""
    payload = _read_json(bridge_dir / _TMUX_FILE)
    if payload is None:
        return None
    socket_path = payload.get("socket_path")
    tmux_target = payload.get("tmux_target")
    if not isinstance(socket_path, str) or not isinstance(tmux_target, str):
        return None
    if not socket_path or not tmux_target:
        return None
    return Path(socket_path), tmux_target


def _wait_for_tmux_info(
    bridge_dir: Path, *, timeout_s: float
) -> tuple[Path, str] | None:
    """Poll for the tmux handshake file until *timeout_s* elapses."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _injection_cancelled(bridge_dir):
            return None
        info = read_tmux_info(bridge_dir)
        if info is not None:
            return info
        time.sleep(_READY_POLL_INTERVAL_S)
    return None


# ---------------------------------------------------------------------------
# tmux plumbing
# ---------------------------------------------------------------------------


def _run_tmux(
    socket_path: Path, *args: str, timeout_s: float = _TMUX_TIMEOUT_S
) -> subprocess.CompletedProcess[str]:
    """Run one tmux command against the runner-owned socket."""
    return subprocess.run(
        ["tmux", "-S", str(socket_path), *args],
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )


def _capture_pane(socket_path: Path, target: str) -> str:
    """Capture the visible contents of the target pane."""
    proc = _run_tmux(socket_path, "capture-pane", "-p", "-t", target)
    return proc.stdout if proc.returncode == 0 else ""


def _session_alive(socket_path: Path, target: str) -> bool:
    """Return whether the tmux target still exists."""
    proc = _run_tmux(socket_path, "has-session", "-t", target)
    return proc.returncode == 0


def _paste_payload_bytes(content: str) -> bytes:
    """Encode *content* for ``tmux load-buffer`` (CR-normalised multi-line)."""
    normalized = content.replace("\r\n", "\n").replace("\n", "\r")
    return normalized.encode("utf-8")


def _send_enter(socket_path: Path, target: str) -> None:
    """Press Enter in the target pane."""
    _run_tmux(socket_path, "send-keys", "-t", target, "Enter")


def _send_key(socket_path: Path, target: str, key: str) -> None:
    """Send one literal key to the target pane."""
    _run_tmux(socket_path, "send-keys", "-t", target, key)


def _paste_text(socket_path: Path, target: str, content: str) -> None:
    """Bracketed-paste *content* into the target pane's editor."""
    buffer_name = f"omnigent-{os.getpid()}-{int(time.time() * 1000)}"
    load = subprocess.run(
        ["tmux", "-S", str(socket_path), "load-buffer", "-b", buffer_name, "-"],
        input=_paste_payload_bytes(content),
        capture_output=True,
        timeout=_TMUX_TIMEOUT_S,
        check=False,
    )
    if load.returncode != 0:
        raise CommandCodeTuiNotReadyError("failed to load paste buffer into the Command Code pane")
    _run_tmux(socket_path, "paste-buffer", "-p", "-d", "-b", buffer_name, "-t", target)


# ---------------------------------------------------------------------------
# pane parsing (vendor-specific, deliberately permissive)
# ---------------------------------------------------------------------------

# Command Code renders menu options with a ❯ cursor and a leading number
# (``❯ 1. Yes, proceed`` / ``❯ 2. No, exit``); its idle prompt is the same glyph
# without a number (``❯ Ask your question...``). Validated against Command Code
# v1.88.2. The first-boot folder-trust modal is a menu whose question names the
# folder.
_MENU_OPTION_RE = re.compile(r"^\s*❯\s*\d+\.\s")
_EDITOR_LINE_RE = re.compile(r"^\s*❯(?!\s*\d+\.)\s")
_TRUST_QUESTION = "do you trust the files in this folder"


def _parse_pane(pane_text: str) -> _CommandCodePaneState:
    """
    Parse a captured pane into a small state.

    Detects the idle editor (``❯ Ask your question...``), a numbered menu
    (approval or folder-trust), and whether that menu is the trust modal (so the
    caller can auto-accept it once instead of treating it as a blocking
    approval).
    """
    lines = tuple(pane_text.splitlines())
    tail = lines[-16:] if len(lines) > 16 else lines
    menu_present = any(_MENU_OPTION_RE.match(line) for line in tail)
    editor_present = any(_EDITOR_LINE_RE.match(line) for line in tail)
    trust_modal_present = _TRUST_QUESTION in pane_text.lower()
    return _CommandCodePaneState(
        lines=lines,
        editor_present=editor_present,
        menu_present=menu_present,
        trust_modal_present=trust_modal_present,
    )


# ---------------------------------------------------------------------------
# message injection / interrupt / lifecycle
# ---------------------------------------------------------------------------


def inject_user_message(
    bridge_dir: Path,
    *,
    content: str,
    timeout_s: float = 120.0,
    cancel_event: threading.Event | None = None,
    turn_streaming: bool = False,
) -> None:
    """
    Type one web-UI turn into the running Command Code TUI and submit it.

    :param bridge_dir: Session bridge dir (carries the tmux handshake).
    :param content: The message text to inject.
    :param timeout_s: How long to wait for the TUI to become ready.
    :param cancel_event: Cooperative cancellation from the executor.
    :param turn_streaming: ``True`` when injecting mid-turn (steering).
    :raises CommandCodeTuiNotReadyError: If the pane never becomes ready.
    :raises CommandCodeApprovalPendingError: If an approval prompt is showing.
    """
    del turn_streaming  # Command Code accepts a mid-turn paste the same way.
    info = _wait_for_tmux_info(bridge_dir, timeout_s=min(timeout_s, 30.0))
    if info is None:
        raise CommandCodeTuiNotReadyError("Command Code tmux handshake was not written")
    socket_path, target = info

    deadline = time.monotonic() + timeout_s
    trust_accepted = False
    while time.monotonic() < deadline:
        if cancel_event is not None and cancel_event.is_set():
            return
        if not _session_alive(socket_path, target):
            raise CommandCodeTuiNotReadyError("Command Code tmux session is gone")
        state = _parse_pane(_capture_pane(socket_path, target))
        if state.trust_modal_present and not trust_accepted:
            # First boot in this folder: accept "Yes, proceed" (the default
            # selection) once, otherwise the message would land in the modal.
            _send_enter(socket_path, target)
            trust_accepted = True
            time.sleep(_READY_POLL_INTERVAL_S)
            continue
        if state.trust_modal_present:
            # Trust was just accepted; give the editor a moment to draw.
            time.sleep(_READY_POLL_INTERVAL_S)
            continue
        if state.approval_menu_present:
            raise CommandCodeApprovalPendingError(
                "Command Code is waiting on an approval prompt"
            )
        if state.ready:
            break
        time.sleep(_READY_POLL_INTERVAL_S)
    else:
        raise CommandCodeTuiNotReadyError("Command Code TUI did not become ready")

    if cancel_event is not None and cancel_event.is_set():
        return
    if content:
        _paste_text(socket_path, target, content)
        time.sleep(0.1)
        _send_enter(socket_path, target)


def inject_interrupt(bridge_dir: Path, *, timeout_s: float = 10.0) -> bool:
    """
    Send Escape to the pane to cancel the in-flight turn.

    :returns: ``True`` when the key was delivered.
    """
    info = _wait_for_tmux_info(bridge_dir, timeout_s=timeout_s)
    if info is None:
        return False
    socket_path, target = info
    if not _session_alive(socket_path, target):
        return False
    _send_key(socket_path, target, "Escape")
    return True


def inject_approval_keystroke(bridge_dir: Path, *, key: str, timeout_s: float = 10.0) -> bool:
    """
    Answer an on-screen Command Code approval menu with *key*.

    :returns: ``True`` when the keystroke was delivered to a visible menu.
    """
    info = _wait_for_tmux_info(bridge_dir, timeout_s=timeout_s)
    if info is None:
        return False
    socket_path, target = info
    if not _session_alive(socket_path, target):
        return False
    if not _parse_pane(_capture_pane(socket_path, target)).approval_menu_present:
        return False
    _send_key(socket_path, target, key)
    return True


def kill_session(bridge_dir: Path, *, timeout_s: float = 10.0) -> None:
    """Kill the runner-owned Command Code tmux session, if present."""
    info = _wait_for_tmux_info(bridge_dir, timeout_s=timeout_s)
    if info is None:
        return
    socket_path, target = info
    _run_tmux(socket_path, "kill-session", "-t", target)


def native_input_ready(session_id: str, instance: TerminalInstance) -> bool:
    """
    Input-readiness probe polled by the runner's terminal watcher.

    :param session_id: Omnigent session id (unused; the pane is enough).
    :param instance: The runner's terminal instance for the pane.
    :returns: ``True`` once the pane looks idle and accepts input.
    """
    del session_id
    pane_text = instance.last_pane_text()
    if not pane_text:
        return False
    return _parse_pane(pane_text).ready


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _injection_cancelled(bridge_dir: Path) -> bool:
    """Return whether a cancellation sentinel has been dropped in the bridge dir."""
    return (bridge_dir / _INJECTION_CANCELLED_FILE).exists()


def _atomic_write_json(path: Path, payload: JsonObject) -> None:
    """Write *payload* as JSON atomically."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path) -> JsonObject | None:
    """Read a JSON object from *path*, or ``None`` when absent/invalid."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None
