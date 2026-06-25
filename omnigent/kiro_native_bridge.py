"""Bridge utilities for native Kiro TUI sessions."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

KIRO_NATIVE_BRIDGE_DIR_ENV_VAR = "HARNESS_KIRO_NATIVE_BRIDGE_DIR"
KIRO_ACP_RECORD_PATH_ENV_VAR = "KIRO_ACP_RECORD_PATH"

_BRIDGE_ROOT = Path(os.environ.get("TMPDIR", "/tmp")) / f"omnigent-{os.getuid()}" / "kiro-native"
_TMUX_FILE = "tmux.json"
_FORWARDER_READY_FILE = "kiro_session_forwarder_ready.json"
_ACP_RECORD_FILE = "kiro_acp_record.jsonl"
_TMUX_READY_TIMEOUT_S = 30.0
_TMUX_SEND_TIMEOUT_S = 10.0
_POLL_INTERVAL_S = 0.2
_TYPE_SETTLE_S = 0.3
_TYPE_COMMIT_TIMEOUT_S = 5.0
_SUBMIT_VERIFY_TIMEOUT_S = 5.0
_SUBMIT_RETRY_INTERVAL_S = 0.5
_PERMISSION_KEY_INTERVAL_S = 0.3
_PERMISSION_ENTER_SETTLE_S = 0.5
_KIRO_SEPARATOR = "────"
_KIRO_INPUT_READY_MARKERS = (
    "ask a question or describe a task",
    "Type to steer",
)
_KIRO_PERMISSION_MARKERS = (
    "requires approval",
    "Yes, single permission",
    "Trust, always allow in this session",
    "No (Tab to edit)",
)
_SEND_KEYS_LITERAL_CHARS_PER_CALL = 1024

# Ambient provider/cloud/CI credentials that must not be inherited by Kiro.
KIRO_NATIVE_ENV_UNSET = [
    "ANTHROPIC_API_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AZURE_CLIENT_SECRET",
    "CI",
    "DATABRICKS_CLIENT_SECRET",
    "DATABRICKS_CONFIG_PROFILE",
    "DATABRICKS_HOST",
    "DATABRICKS_TOKEN",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GOOGLE_API_KEY",
    "OPENAI_API_KEY",
]

_CHILD_ENV_ALLOWLIST = [
    "COLORTERM",
    "HOME",
    "KIRO_CONFIG_HOME",
    "KIRO_HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LOGNAME",
    "NO_COLOR",
    "PATH",
    "SHELL",
    "TERM",
    "TMPDIR",
    "USER",
]


def bridge_dir_for_session_id(session_id: str) -> Path:
    """Return the per-session Kiro bridge directory."""
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
    return _BRIDGE_ROOT / digest


def prepare_bridge_dir(session_id: str) -> Path:
    """Create and return the per-session Kiro bridge directory."""
    bridge_dir = bridge_dir_for_session_id(session_id)
    bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(bridge_dir, 0o700)
    return bridge_dir


def acp_record_path(bridge_dir: Path) -> Path:
    """Return the per-session Kiro TUI ACP recorder file path."""
    return bridge_dir / _ACP_RECORD_FILE


def build_kiro_native_spawn_env(session_id: str) -> dict[str, str]:
    """Build the ``HARNESS_KIRO_NATIVE_*`` env for the harness executor."""
    bridge_dir = prepare_bridge_dir(session_id)
    return {KIRO_NATIVE_BRIDGE_DIR_ENV_VAR: str(bridge_dir)}


def build_kiro_native_terminal_env(
    session_id: str,
    *,
    source_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build the allowlisted child environment for ``kiro-cli``."""
    env = os.environ if source_env is None else source_env
    child = {key: env[key] for key in _CHILD_ENV_ALLOWLIST if env.get(key)}
    bridge_dir = prepare_bridge_dir(session_id)
    child[KIRO_NATIVE_BRIDGE_DIR_ENV_VAR] = str(bridge_dir)
    child[KIRO_ACP_RECORD_PATH_ENV_VAR] = str(acp_record_path(bridge_dir))
    return child


def write_tmux_target(
    bridge_dir: Path,
    *,
    socket_path: Path,
    tmux_target: str,
    pid: int | None = None,
    requires_forwarder_ready: bool = False,
) -> None:
    """Advertise the tmux socket + target for the running Kiro terminal."""
    bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "socket_path": str(socket_path),
        "tmux_target": tmux_target,
        "updated_at": time.time(),
    }
    if requires_forwarder_ready:
        payload["requires_forwarder_ready"] = True
    if pid is not None:
        payload["pid"] = pid
    tmp = bridge_dir / (_TMUX_FILE + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, bridge_dir / _TMUX_FILE)


def read_tmux_info(bridge_dir: Path) -> dict[str, str] | None:
    """Return ``{socket_path, tmux_target}`` from ``tmux.json``, or ``None``."""
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
    if (
        isinstance(socket_path, str)
        and socket_path
        and isinstance(tmux_target, str)
        and tmux_target
    ):
        return {"socket_path": socket_path, "tmux_target": tmux_target}
    return None


def write_forwarder_ready(bridge_dir: Path) -> None:
    """Mark the Kiro JSONL forwarder as attached and caught up."""
    bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = {"updated_at": time.time()}
    tmp = bridge_dir / (_FORWARDER_READY_FILE + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, bridge_dir / _FORWARDER_READY_FILE)


def _read_bridge_json(bridge_dir: Path, filename: str) -> dict[str, Any] | None:
    try:
        raw = (bridge_dir / filename).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _wait_for_forwarder_ready_if_required(
    bridge_dir: Path,
    *,
    tmux_info: dict[str, Any],
    timeout_s: float,
) -> None:
    if tmux_info.get("requires_forwarder_ready") is not True:
        return
    tmux_updated_at = tmux_info.get("updated_at")
    if not isinstance(tmux_updated_at, int | float):
        tmux_updated_at = 0.0
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        ready = _read_bridge_json(bridge_dir, _FORWARDER_READY_FILE)
        ready_updated_at = ready.get("updated_at") if ready is not None else None
        if isinstance(ready_updated_at, int | float) and ready_updated_at >= tmux_updated_at:
            return
        time.sleep(_POLL_INTERVAL_S)
    raise RuntimeError("kiro-native session forwarder was not ready before injection")


def _wait_for_tmux_info(bridge_dir: Path, *, timeout_s: float) -> dict[str, str]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        info = read_tmux_info(bridge_dir)
        if info is not None:
            return info
        time.sleep(_POLL_INTERVAL_S)
    raise RuntimeError(f"kiro-native tmux target was not advertised within {timeout_s:.0f}s")


def _run_tmux(socket_path: str, *args: str) -> None:
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


def _capture_pane(socket_path: str, tmux_target: str) -> str:
    """Capture visible pane contents; return empty string on failure."""
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


def _submit_needle(content: str) -> str:
    """Return a small marker used to identify the pasted draft."""
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    for line in normalized.split("\n"):
        for idx, ch in enumerate(line):
            if ord(ch) < 0x20:
                line = line[:idx]
                break
        line = line.strip()
        if line:
            return line[:24]
    return ""


def _kiro_input_region(pane: str) -> str:
    """Return Kiro's bottom input region, excluding transcript history."""
    lines = pane.splitlines()
    for index in range(len(lines) - 1, -1, -1):
        if _KIRO_SEPARATOR in lines[index]:
            return "\n".join(lines[index + 1 :])
    return "\n".join(lines[-8:])


def _draft_in_input_region(pane: str, needle: str, baseline_region: str) -> bool:
    """Return whether the draft is still visible in Kiro's input region."""
    region = _kiro_input_region(pane)
    if not needle or region == baseline_region:
        return False
    normalized_needle = needle.strip()
    if not normalized_needle:
        return False
    return any(
        line == normalized_needle or line.startswith(normalized_needle)
        for line in _kiro_draft_candidate_lines(region)
    )


def _kiro_draft_candidate_lines(region: str) -> list[str]:
    """Return input-region lines that can represent editable draft text."""
    candidates: list[str] = []
    for raw_line in region.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("kiro_default"):
            continue
        if line.startswith("/copy"):
            continue
        if line.startswith("▸ Credits:"):
            continue
        if any(marker in line for marker in _KIRO_INPUT_READY_MARKERS):
            continue
        candidates.append(line)
    return candidates


def _kiro_input_ready(pane: str) -> bool:
    """Return whether Kiro's bottom input prompt is ready to receive text."""
    region = _kiro_input_region(pane)
    return any(marker in region for marker in _KIRO_INPUT_READY_MARKERS)


def _kiro_permission_prompt_active(pane: str) -> bool:
    """Return whether Kiro's visible approval prompt is active."""
    return all(marker in pane for marker in _KIRO_PERMISSION_MARKERS)


def _kiro_permission_focus_on_one_time_allow(pane: str) -> bool:
    """Return whether Kiro's approval picker is focused on one-time allow."""
    return any(line.strip().startswith("❯ Yes, single permission") for line in pane.splitlines())


def _kiro_permission_focus_on_reject(pane: str) -> bool:
    """Return whether Kiro's approval picker is focused on one-time reject."""
    return any(line.strip().startswith("❯ No (Tab to edit)") for line in pane.splitlines())


def _kiro_active_permission_tool_line(pane: str) -> str:
    """Return the tool line associated with the active approval panel."""
    lines = pane.splitlines()
    approval_index = -1
    for index, line in enumerate(lines):
        if "requires approval" in line:
            approval_index = index
    if approval_index < 0:
        return ""
    for line in reversed(lines[:approval_index]):
        stripped = line.strip()
        if not stripped or _KIRO_SEPARATOR in stripped:
            continue
        return stripped.lstrip("↓●○✓✗ ").strip()
    return ""


def _kiro_permission_prompt_matches_title(pane: str, expected_title: str | None) -> bool:
    """Return whether the visible prompt appears to match the parsed request title."""
    if not expected_title:
        return True
    title = expected_title.strip()
    if not title:
        return True
    tool_line = _kiro_active_permission_tool_line(pane)
    if not tool_line:
        return False
    if title == tool_line:
        return True
    if title.startswith("Running:"):
        command = title.removeprefix("Running:").strip()
        return bool(command and (tool_line == command or tool_line.endswith(f" {command}")))
    return title in tool_line


def _wait_for_kiro_permission_prompt(
    socket_path: str,
    tmux_target: str,
    *,
    expected_title: str | None,
    timeout_s: float,
) -> None:
    """Wait until Kiro has rendered an approval prompt before typing a verdict."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        pane = _capture_pane(socket_path, tmux_target)
        if (
            _kiro_permission_prompt_active(pane)
            and _kiro_permission_focus_on_one_time_allow(pane)
            and _kiro_permission_prompt_matches_title(pane, expected_title)
        ):
            return
        time.sleep(_POLL_INTERVAL_S)
    raise RuntimeError(
        "kiro-native permission prompt was not safely focused before verdict delivery"
    )


def _wait_for_kiro_input_ready(
    socket_path: str,
    tmux_target: str,
    *,
    timeout_s: float,
) -> None:
    """Wait until Kiro has rendered an input prompt before typing."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _kiro_input_ready(_capture_pane(socket_path, tmux_target)):
            return
        time.sleep(_POLL_INTERVAL_S)
    raise RuntimeError("kiro-native TUI input prompt was not ready before injection")


def _type_literal_text(socket_path: str, tmux_target: str, text: str) -> None:
    """Type text into Kiro using literal tmux keystrokes."""
    for start in range(0, len(text), _SEND_KEYS_LITERAL_CHARS_PER_CALL):
        chunk = text[start : start + _SEND_KEYS_LITERAL_CHARS_PER_CALL]
        # ``--`` ends option parsing so a chunk starting with ``-`` (or a chunk
        # boundary that lands on one) is sent as literal text, not parsed as a
        # tmux flag — which would otherwise fail the send-keys call silently.
        _run_tmux(
            socket_path,
            "send-keys",
            "-l",
            "-t",
            tmux_target,
            "--",
            chunk,
        )


def inject_user_message(
    bridge_dir: Path,
    *,
    content: str,
    timeout_s: float = _TMUX_READY_TIMEOUT_S,
) -> None:
    """Deliver a web-UI user message into the Kiro TUI via tmux typing."""
    if not content:
        raise RuntimeError("kiro-native injection requires non-empty content")
    info = _wait_for_tmux_info(bridge_dir, timeout_s=timeout_s)
    raw_info = _read_bridge_json(bridge_dir, _TMUX_FILE) or {}
    _wait_for_forwarder_ready_if_required(
        bridge_dir,
        tmux_info=raw_info,
        timeout_s=timeout_s,
    )
    socket_path = info["socket_path"]
    tmux_target = info["tmux_target"]
    if not _session_alive(socket_path, tmux_target):
        raise RuntimeError(
            "kiro terminal is no longer running (the TUI exited); restart the session"
        )
    _wait_for_kiro_input_ready(socket_path, tmux_target, timeout_s=timeout_s)
    _run_tmux(socket_path, "send-keys", "-t", tmux_target, "C-a")
    _run_tmux(socket_path, "send-keys", "-t", tmux_target, "C-k")
    baseline_region = _kiro_input_region(_capture_pane(socket_path, tmux_target))
    _type_literal_text(socket_path, tmux_target, content)
    needle = _submit_needle(content)
    draft_seen = False
    if needle:
        deadline = time.monotonic() + _TYPE_COMMIT_TIMEOUT_S
        while time.monotonic() < deadline:
            if _draft_in_input_region(
                _capture_pane(socket_path, tmux_target), needle, baseline_region
            ):
                draft_seen = True
                break
            time.sleep(_POLL_INTERVAL_S)
    time.sleep(_TYPE_SETTLE_S)
    _run_tmux(socket_path, "send-keys", "-t", tmux_target, "Enter")
    if not draft_seen:
        return
    deadline = time.monotonic() + _SUBMIT_VERIFY_TIMEOUT_S
    last_enter = time.monotonic()
    while time.monotonic() < deadline:
        time.sleep(_POLL_INTERVAL_S)
        if not _draft_in_input_region(
            _capture_pane(socket_path, tmux_target), needle, baseline_region
        ):
            return
        if time.monotonic() - last_enter >= _SUBMIT_RETRY_INTERVAL_S:
            _run_tmux(socket_path, "send-keys", "-t", tmux_target, "Enter")
            last_enter = time.monotonic()
    raise RuntimeError("Kiro did not accept the submitted message; the draft is still visible")


def send_kiro_permission_verdict(
    bridge_dir: Path,
    *,
    action: str,
    expected_title: str | None = None,
    timeout_s: float = _TMUX_READY_TIMEOUT_S,
) -> None:
    """Deliver a one-time Kiro permission verdict to the active TUI prompt."""
    if action not in {"accept", "decline", "cancel"}:
        raise RuntimeError(f"unsupported Kiro permission action: {action!r}")
    info = _wait_for_tmux_info(bridge_dir, timeout_s=timeout_s)
    socket_path = info["socket_path"]
    tmux_target = info["tmux_target"]
    if not _session_alive(socket_path, tmux_target):
        raise RuntimeError(
            "kiro terminal is no longer running (the TUI exited); restart the session"
        )
    _wait_for_kiro_permission_prompt(
        socket_path, tmux_target, expected_title=expected_title, timeout_s=timeout_s
    )
    if action == "accept":
        time.sleep(_PERMISSION_ENTER_SETTLE_S)
        _run_tmux(socket_path, "send-keys", "-t", tmux_target, "Enter")
        time.sleep(_PERMISSION_KEY_INTERVAL_S)
        return
    for key in ("Down", "Down"):
        _run_tmux(socket_path, "send-keys", "-t", tmux_target, key)
        time.sleep(_PERMISSION_KEY_INTERVAL_S)
    pane = _capture_pane(socket_path, tmux_target)
    if not (
        _kiro_permission_prompt_active(pane)
        and _kiro_permission_focus_on_reject(pane)
        and _kiro_permission_prompt_matches_title(pane, expected_title)
    ):
        raise RuntimeError("kiro-native reject option was not safely focused before delivery")
    time.sleep(_PERMISSION_ENTER_SETTLE_S)
    _run_tmux(socket_path, "send-keys", "-t", tmux_target, "Enter")
    time.sleep(_PERMISSION_KEY_INTERVAL_S)
