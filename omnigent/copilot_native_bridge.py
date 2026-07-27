"""Filesystem bridge + tmux injection for the Copilot-native terminal harness."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from omnigent._platform import stable_user_id

BRIDGE_DIR_ENV_VAR = "HARNESS_COPILOT_NATIVE_BRIDGE_DIR"
REQUEST_SESSION_ID_ENV_VAR = "HARNESS_COPILOT_NATIVE_REQUEST_SESSION_ID"

_BRIDGE_ROOT = Path(tempfile.gettempdir()) / f"omnigent-{stable_user_id()}" / "copilot-native"
_TMUX_FILE = "tmux.json"
_SESSION_STATE_DIR = "session-state"
_EVENTS_FILE = "events.jsonl"
_POLL_INTERVAL_S = 0.2
_TMUX_SEND_TIMEOUT_S = 10.0
_PASTE_BUFFER = "omnigent-copilot-paste"


def bridge_root() -> Path:
    """Return the configured Copilot-native bridge root."""
    return _BRIDGE_ROOT


def copilot_home() -> Path:
    """Return the Copilot CLI's state directory (``COPILOT_HOME`` or ``~/.copilot``)."""
    raw = os.environ.get("COPILOT_HOME", "").strip()
    return Path(raw) if raw else Path.home() / ".copilot"


def session_events_path(copilot_session_id: str) -> Path:
    """Return the ``events.jsonl`` the CLI appends for *copilot_session_id*.

    The Copilot CLI records every session under
    ``<copilot-home>/session-state/<uuid>/`` and appends one JSON event per line
    to ``events.jsonl``. ``--session-id`` pins that uuid at launch, so the
    forwarder addresses this session's stream directly instead of guessing from
    recency (verified against Copilot CLI 1.0.63).
    """
    return copilot_home() / _SESSION_STATE_DIR / copilot_session_id / _EVENTS_FILE


def bridge_dir_for_session_id(session_id: str) -> Path:
    """Return the per-session bridge dir for a Copilot-native conversation."""
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
    return _BRIDGE_ROOT / digest


def prepare_bridge_dir(session_id: str) -> Path:
    """Create the bridge directory for *session_id*."""
    bridge_dir = bridge_dir_for_session_id(session_id)
    bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(bridge_dir, 0o700)
    return bridge_dir


def build_copilot_native_spawn_env(conversation_id: str) -> dict[str, str]:
    """Build the ``HARNESS_COPILOT_NATIVE_*`` env the harness executor reads."""
    return {
        BRIDGE_DIR_ENV_VAR: str(bridge_dir_for_session_id(conversation_id)),
        REQUEST_SESSION_ID_ENV_VAR: conversation_id,
    }


def write_tmux_target(
    bridge_dir: Path,
    *,
    session_id: str,
    socket_path: Path,
    tmux_target: str,
    pid: int | None = None,
) -> None:
    """Advertise the tmux socket and target for the running Copilot terminal."""
    bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "session_id": session_id,
        "socket_path": str(socket_path),
        "tmux_target": tmux_target,
        "updated_at": time.time(),
    }
    if pid is not None:
        payload["pid"] = pid
    tmp = bridge_dir / (_TMUX_FILE + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, bridge_dir / _TMUX_FILE)


def read_tmux_info(bridge_dir: Path) -> dict[str, str] | None:
    """Return ``{session_id, socket_path, tmux_target}`` from ``tmux.json``."""
    try:
        raw = (bridge_dir / _TMUX_FILE).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    socket_path = data.get("socket_path")
    tmux_target = data.get("tmux_target")
    session_id = data.get("session_id")
    if (
        isinstance(session_id, str)
        and session_id
        and isinstance(socket_path, str)
        and socket_path
        and isinstance(tmux_target, str)
        and tmux_target
    ):
        return {"session_id": session_id, "socket_path": socket_path, "tmux_target": tmux_target}
    return None


def _wait_for_tmux_info(bridge_dir: Path, *, timeout_s: float) -> dict[str, str]:
    """Block until ``tmux.json`` is advertised, or raise on timeout."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        info = read_tmux_info(bridge_dir)
        if info is not None:
            return info
        time.sleep(_POLL_INTERVAL_S)
    raise RuntimeError(f"copilot-native tmux target was not advertised within {timeout_s:.0f}s")


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
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"tmux command timed out after {_TMUX_SEND_TIMEOUT_S}s") from exc
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "<no output>"
        raise RuntimeError(f"tmux command failed (rc={proc.returncode}): {detail}")


def _session_alive(socket_path: str, tmux_target: str) -> bool:
    """Return whether the tmux session/pane still exists."""
    try:
        proc = subprocess.run(
            ["tmux", "-S", socket_path, "has-session", "-t", tmux_target],
            check=False,
            capture_output=True,
            text=True,
            timeout=_TMUX_SEND_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return proc.returncode == 0


def _paste_payload_bytes(text: str) -> bytes:
    """Encode text for ``tmux load-buffer``."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    body = bytearray()
    for ch in normalized:
        if ch == "\n":
            body.append(0x0D)
            continue
        if ch == "\t":
            body.append(0x09)
            continue
        if ord(ch) < 0x20:
            continue
        body.extend(ch.encode("utf-8"))
    return bytes(body)


def inject_user_message(
    bridge_dir: Path,
    *,
    content: str,
    timeout_s: float = 30.0,
) -> None:
    """Deliver a web-UI user message into the Copilot TUI via tmux paste."""
    if not content:
        raise RuntimeError("copilot-native injection requires non-empty content")
    info = _wait_for_tmux_info(bridge_dir, timeout_s=timeout_s)
    socket_path = info["socket_path"]
    tmux_target = info["tmux_target"]
    if not _session_alive(socket_path, tmux_target):
        raise RuntimeError(
            "copilot terminal is no longer running (the TUI exited); restart the session"
        )
    _run_tmux(socket_path, "send-keys", "-t", tmux_target, "C-u")
    with tempfile.NamedTemporaryFile(
        dir=bridge_dir, prefix="paste_", suffix=".bin", delete=False
    ) as paste_file:
        paste_file.write(_paste_payload_bytes(content + "\n"))
        paste_path = paste_file.name
    try:
        _run_tmux(socket_path, "load-buffer", "-b", _PASTE_BUFFER, paste_path)
        _run_tmux(
            socket_path,
            "paste-buffer",
            "-p",
            "-d",
            "-b",
            _PASTE_BUFFER,
            "-t",
            tmux_target,
        )
    finally:
        with contextlib.suppress(OSError):
            os.unlink(paste_path)
    _run_tmux(socket_path, "send-keys", "-t", tmux_target, "Enter")


def inject_model_command(
    bridge_dir: Path,
    *,
    model: str,
    timeout_s: float = 30.0,
) -> None:
    """Apply a live Copilot model switch using the CLI's ``/model`` command."""
    model = model.strip()
    if not model:
        raise RuntimeError("copilot-native model switch requires a non-empty model id")
    info = _wait_for_tmux_info(bridge_dir, timeout_s=timeout_s)
    socket_path = info["socket_path"]
    tmux_target = info["tmux_target"]
    if not _session_alive(socket_path, tmux_target):
        raise RuntimeError(
            "copilot terminal is no longer running (the TUI exited); restart the session"
        )
    _run_tmux(socket_path, "send-keys", "-t", tmux_target, "C-u")
    _run_tmux(socket_path, "send-keys", "-t", tmux_target, "-l", f"/model {model}")
    _run_tmux(socket_path, "send-keys", "-t", tmux_target, "Enter")


def inject_interrupt(bridge_dir: Path, *, timeout_s: float = 30.0) -> None:
    """Cancel the in-flight Copilot turn by sending Escape to the pane."""
    info = _wait_for_tmux_info(bridge_dir, timeout_s=timeout_s)
    _run_tmux(info["socket_path"], "send-keys", "-t", info["tmux_target"], "Escape")


def kill_session(bridge_dir: Path, *, timeout_s: float = 30.0) -> None:
    """Hard-stop the Copilot session by killing its tmux session."""
    info = _wait_for_tmux_info(bridge_dir, timeout_s=timeout_s)
    _run_tmux(info["socket_path"], "kill-session", "-t", info["tmux_target"])
