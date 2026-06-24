"""Filesystem bridge + tmux injection for the cursor-native terminal harness.

The runner launches the ``cursor-agent`` TUI in a private tmux pane and records
that pane's socket + target here via :func:`write_tmux_target`. The harness
executor then delivers Omnigent web-UI messages into the *same* pane via
:func:`inject_user_message` (tmux bracketed paste + Enter) — the cursor analog
of claude-native's tmux send-keys bridge. This is what wires the web-UI chat box
to the running Cursor TUI (and, since the web UI embeds that pane, the message
shows in both surfaces).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

#: Env var carrying the bridge dir into the harness executor process.
BRIDGE_DIR_ENV_VAR = "HARNESS_CURSOR_NATIVE_BRIDGE_DIR"

_BRIDGE_ROOT = Path(os.environ.get("TMPDIR", "/tmp")) / f"omnigent-{os.getuid()}" / "cursor-native"
_TMUX_FILE = "tmux.json"
_BRIDGE_CONFIG_FILE = "bridge.json"
_MCP_CONFIG_FILE = "mcp.json"
_MCP_SERVER_NAME = "omnigent"
_CURSOR_AUTO_APPROVE_TOOLS = [
    "list_comments",
    "sys_add_policy",
    "sys_agent_download",
    "sys_agent_get",
    "sys_agent_list",
    "sys_call_async",
    "sys_cancel_async",
    "sys_cancel_task",
    "sys_list_models",
    "sys_os_edit",
    "sys_os_read",
    "sys_os_shell",
    "sys_os_write",
    "sys_policy_registry",
    "sys_session_close",
    "sys_session_create",
    "sys_session_get_history",
    "sys_session_get_info",
    "sys_session_list",
    "sys_session_send",
    "sys_terminal_close",
    "sys_terminal_launch",
    "sys_terminal_list",
    "sys_terminal_read",
    "sys_terminal_send",
    "update_comment",
]
_TMUX_READY_TIMEOUT_S = 30.0
_TMUX_SEND_TIMEOUT_S = 10.0
_POLL_INTERVAL_S = 0.2
_PASTE_SETTLE_S = 0.3
_PASTE_BUFFER = "omnigent-cursor-paste"
# How long to wait for the pasted text to become visible in the pane before
# sending Enter — submitting before the TUI commits the paste folds the Enter
# into the paste as a newline and the message sits unsent.
_PASTE_COMMIT_TIMEOUT_S = 5.0
# cursor-agent TUI markers (Phase 0): idle input placeholder / running footer /
# first-run trust modal.
_IDLE_MARKERS = ("Plan, search, build", "Add a follow-up")
_TRUST_MARKER = "Trust this workspace"


def bridge_dir_for_session_id(session_id: str) -> Path:
    """Return the per-session bridge dir, e.g. ``/tmp/omnigent-<uid>/cursor-native/<hash>``."""
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
    return _BRIDGE_ROOT / digest


def bridge_root() -> Path:
    """Return the configured Cursor-native bridge root."""
    return _BRIDGE_ROOT


def _ensure_dir(path: Path) -> None:
    """Create *path* (and parents) with owner-only permissions."""
    path.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o700)


def build_cursor_native_spawn_env(session_id: str) -> dict[str, str]:
    """Build the ``HARNESS_CURSOR_NATIVE_*`` env the harness executor reads."""
    bridge_dir = bridge_dir_for_session_id(session_id)
    _ensure_dir(bridge_dir)
    return {
        BRIDGE_DIR_ENV_VAR: str(bridge_dir),
    }


def build_mcp_config(
    bridge_dir: Path,
    *,
    python_executable: str | None = None,
) -> dict[str, Any]:
    """Build Cursor's ``.cursor/mcp.json`` for the Omnigent relay server.

    Cursor prompts for MCP tool approval before it sends ``tools/call`` to the
    server. Omnigent tools already route through the Omnigent ``/mcp`` proxy,
    where TOOL_CALL policies publish ``response.elicitation_request`` events
    that the web UI can render. Auto-approving the Cursor-side MCP gate avoids a
    hidden in-terminal approval prompt blocking the call before Omnigent ever
    sees it, while preserving Omnigent's own policy/elicitation gate.
    """
    python = python_executable or sys.executable
    return {
        "mcpServers": {
            _MCP_SERVER_NAME: {
                "command": python,
                "args": [
                    "-I",
                    "-m",
                    "omnigent.claude_native_bridge",
                    "serve-mcp",
                    "--bridge-dir",
                    str(bridge_dir),
                ],
                "autoApprove": list(_CURSOR_AUTO_APPROVE_TOOLS),
                "env": {
                    "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
                },
            }
        }
    }


def write_mcp_bridge_config(bridge_dir: Path) -> None:
    """Write the token config required by the shared Omnigent MCP bridge."""
    _ensure_dir(bridge_dir)
    config_path = bridge_dir / _BRIDGE_CONFIG_FILE
    if config_path.exists():
        return
    payload = {"token": secrets.token_urlsafe(32)}
    tmp = bridge_dir / (_BRIDGE_CONFIG_FILE + ".tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, config_path)


def write_mcp_config(
    workspace: Path,
    bridge_dir: Path,
    *,
    python_executable: str | None = None,
) -> Path:
    """Write the workspace-scoped Cursor MCP config for Omnigent tools."""
    write_mcp_bridge_config(bridge_dir)
    cursor_dir = workspace / ".cursor"
    cursor_dir.mkdir(parents=True, exist_ok=True)
    path = cursor_dir / _MCP_CONFIG_FILE
    payload = build_mcp_config(bridge_dir, python_executable=python_executable)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    enable_mcp_for_workspace(workspace)
    allow_mcp_tools_in_cli_config()
    return path


def approve_mcp_server_for_workspace(workspace: Path) -> None:
    """Approve the workspace-scoped Omnigent MCP server in Cursor's state.

    Cursor stores per-workspace MCP approvals using a private hash of the
    concrete server config. Rather than duplicate that implementation here,
    ask ``cursor-agent mcp enable omnigent`` to write the exact approval entry
    for this workspace. This is best-effort: the TUI still launches if the
    installed Cursor CLI cannot run the management subcommand, but when it can,
    the hidden server-approval gate is cleared before startup.
    """
    try:
        from omnigent.cursor_native import resolve_cursor_executable

        cursor = resolve_cursor_executable()
        subprocess.run(
            [cursor, "mcp", "enable", _MCP_SERVER_NAME],
            cwd=workspace,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return


def cursor_project_key(workspace: Path) -> str:
    """Return Cursor's project-state directory key for *workspace*."""
    return str(workspace).strip("/").replace("/", "-") or "root"


def enable_mcp_for_workspace(workspace: Path) -> None:
    """Ensure Cursor does not keep the Omnigent MCP disabled for this workspace."""
    disabled_path = (
        Path.home() / ".cursor" / "projects" / cursor_project_key(workspace) / "mcp-disabled.json"
    )
    try:
        raw = json.loads(disabled_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(raw, list) or _MCP_SERVER_NAME not in raw:
        return
    updated = [item for item in raw if item != _MCP_SERVER_NAME]
    tmp = disabled_path.with_suffix(disabled_path.suffix + ".tmp")
    tmp.write_text(json.dumps(updated, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, disabled_path)


def allow_mcp_tools_in_cli_config() -> None:
    """Allow Omnigent MCP tool calls in Cursor's CLI permission config."""
    path = Path.home() / ".cursor" / "cli-config.json"
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(config, dict):
        return
    permissions = config.setdefault("permissions", {})
    if not isinstance(permissions, dict):
        return
    allow = permissions.setdefault("allow", [])
    if not isinstance(allow, list):
        return
    existing = {item for item in allow if isinstance(item, str)}
    for tool_name in _CURSOR_AUTO_APPROVE_TOOLS:
        entry = f"Mcp({_MCP_SERVER_NAME}:{tool_name})"
        if entry not in existing:
            allow.append(entry)
            existing.add(entry)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def write_tmux_target(
    bridge_dir: Path,
    *,
    socket_path: Path,
    tmux_target: str,
    pid: int | None = None,
) -> None:
    """Advertise the tmux socket + target for the running Cursor terminal."""
    _ensure_dir(bridge_dir)
    payload: dict[str, Any] = {
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


def _wait_for_tmux_info(bridge_dir: Path, *, timeout_s: float) -> dict[str, str]:
    """Block until ``tmux.json`` is advertised, or raise on timeout."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        info = read_tmux_info(bridge_dir)
        if info is not None:
            return info
        time.sleep(_POLL_INTERVAL_S)
    raise RuntimeError(f"cursor-native tmux target was not advertised within {timeout_s:.0f}s")


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


def _capture_pane(socket_path: str, tmux_target: str) -> str:
    """Capture the visible pane contents; ``""`` on any failure (treat as not-ready)."""
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


def _paste_payload_bytes(text: str) -> bytes:
    r"""Encode text for ``tmux load-buffer``: line breaks → CR, tabs kept, other
    control bytes dropped (a stray ESC would close the bracketed-paste early)."""
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


def _session_alive(socket_path: str, tmux_target: str) -> bool:
    """Return whether the tmux session/pane still exists (the TUI is running)."""
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


def capture_cursor_pane(bridge_dir: Path) -> str | None:
    """
    Return the visible Cursor pane text, or ``None`` if the TUI is not running.

    Reused by the runner-side approval mirror
    (:mod:`omnigent.cursor_native_permissions`) to detect cursor-agent's native
    tool-approval prompts. ``None`` (no advertised tmux target, or a dead pane)
    is distinct from ``""`` (a live but empty capture) so the caller can skip
    polling a TUI that has not started yet or has exited.

    :param bridge_dir: The cursor-native bridge dir holding ``tmux.json``.
    :returns: The captured pane text, or ``None`` when no live pane exists.
    """
    info = read_tmux_info(bridge_dir)
    if info is None:
        return None
    socket_path, tmux_target = info["socket_path"], info["tmux_target"]
    if not _session_alive(socket_path, tmux_target):
        return None
    return _capture_pane(socket_path, tmux_target)


def send_cursor_pane_keys(bridge_dir: Path, *keys: str) -> None:
    """
    Send one or more keys to the Cursor pane (tmux ``send-keys``).

    Used by the approval mirror to answer cursor-agent's native prompt from a
    web verdict, e.g. ``"y"`` to approve or ``"Escape"`` to reject. Each key is
    a tmux key name/argument (not bracketed-paste data), so multi-byte keys like
    ``"Escape"`` are interpreted, not typed literally.

    :param bridge_dir: The cursor-native bridge dir holding ``tmux.json``.
    :param keys: tmux key arguments, e.g. ``"y"`` or ``"Escape"``.
    :raises RuntimeError: If the tmux target is not advertised or the
        ``send-keys`` invocation fails.
    """
    info = read_tmux_info(bridge_dir)
    if info is None:
        raise RuntimeError("cursor-native tmux target not advertised")
    _run_tmux(info["socket_path"], "send-keys", "-t", info["tmux_target"], *keys)


def _submit_needle(content: str) -> str:
    """A stable single-line substring used to confirm the paste rendered in the pane."""
    for line in content.splitlines():
        stripped = line.strip()
        if len(stripped) >= 4:
            return stripped[:24]
    stripped = content.strip()
    return stripped[:24] if len(stripped) >= 4 else ""


def _settle_pane(socket_path: str, tmux_target: str, *, timeout_s: float) -> None:
    """Best-effort wait until the Cursor input box is ready to receive a paste.

    Accepts the first-run "Trust this workspace" modal (sends ``a`` at most once)
    so the input box can mount, then waits for an idle/running input marker. Falls
    through after the timeout (mid-turn steering has no idle placeholder) rather
    than raising.
    """
    deadline = time.monotonic() + timeout_s
    trust_accepted = False
    while time.monotonic() < deadline:
        pane = _capture_pane(socket_path, tmux_target)
        if any(marker in pane for marker in _IDLE_MARKERS):
            return
        # One-shot, only when no input marker is up (so a later transcript that
        # merely echoes the phrase can't spray repeated keystrokes into the TUI).
        if not trust_accepted and _TRUST_MARKER in pane:
            trust_accepted = True
            with contextlib.suppress(RuntimeError):
                _run_tmux(socket_path, "send-keys", "-t", tmux_target, "a")
        time.sleep(_POLL_INTERVAL_S)


def inject_user_message(
    bridge_dir: Path,
    *,
    content: str,
    timeout_s: float = _TMUX_READY_TIMEOUT_S,
) -> None:
    """Deliver a web-UI user message into the Cursor TUI via a tmux bracketed paste.

    Clears any leftover draft, pastes *content* (multi-line safe via
    ``load-buffer``/``paste-buffer -p`` so interior newlines stay data, not
    submits), settles, then submits with Enter.

    :param bridge_dir: The cursor-native bridge dir holding ``tmux.json``.
    :param content: User text (non-empty).
    :param timeout_s: Per-readiness-gate timeout.
    :raises RuntimeError: If the tmux target is never advertised or a tmux
        command fails.
    """
    if not content:
        raise RuntimeError("cursor-native injection requires non-empty content")
    info = _wait_for_tmux_info(bridge_dir, timeout_s=timeout_s)
    socket_path = info["socket_path"]
    tmux_target = info["tmux_target"]
    # Fast-fail if the TUI already exited: otherwise _settle_pane polls a dead
    # pane for the full timeout and the web message is silently lost. A clear
    # error lets run_turn surface ExecutorError so the UI can say "restart".
    if not _session_alive(socket_path, tmux_target):
        raise RuntimeError(
            "cursor terminal is no longer running (the TUI exited); restart the session"
        )
    _settle_pane(socket_path, tmux_target, timeout_s=timeout_s)
    # Clear any leftover draft: Home (C-a) + kill-to-end (C-k).
    _run_tmux(socket_path, "send-keys", "-t", tmux_target, "C-a")
    _run_tmux(socket_path, "send-keys", "-t", tmux_target, "C-k")
    with tempfile.NamedTemporaryFile(
        dir=bridge_dir, prefix="paste_", suffix=".bin", delete=False
    ) as paste_file:
        # Trailing newline absorbs any trailing backslash so it can't escape Enter.
        paste_file.write(_paste_payload_bytes(content + "\n"))
        paste_path = paste_file.name
    try:
        _run_tmux(socket_path, "load-buffer", "-b", _PASTE_BUFFER, paste_path)
        _run_tmux(
            socket_path,
            "paste-buffer",
            "-p",  # bracketed-paste markers — the TUI keeps newlines as data
            "-d",  # drop the buffer after pasting
            "-b",
            _PASTE_BUFFER,
            "-t",
            tmux_target,
        )
    finally:
        with contextlib.suppress(OSError):
            os.unlink(paste_path)
    # Wait until the paste is visibly committed to the input box before Enter.
    # Submitting mid-paste folds the Enter in as a newline (the cursor TUI
    # coalesces rapid stdin bursts), leaving the message unsent. Poll for the
    # text, then submit; fall through to a blind submit if no needle is usable.
    needle = _submit_needle(content)
    if needle:
        deadline = time.monotonic() + _PASTE_COMMIT_TIMEOUT_S
        while time.monotonic() < deadline:
            if needle in _capture_pane(socket_path, tmux_target):
                break
            time.sleep(_POLL_INTERVAL_S)
    time.sleep(_PASTE_SETTLE_S)
    _run_tmux(socket_path, "send-keys", "-t", tmux_target, "Enter")


def inject_interrupt(bridge_dir: Path, *, timeout_s: float = _TMUX_READY_TIMEOUT_S) -> None:
    """Cancel the in-flight Cursor turn by sending ``Escape`` to the pane.

    cursor-agent stops a running turn on a single ``Escape`` (verified live).
    The harness ``run_turn`` returns right after the paste, so the runner's
    in-process cancel floor can't reach the turn — this is the analog of
    :func:`inject_user_message` for the web UI's Stop button.

    :raises RuntimeError: If the tmux target is not advertised or send-keys fails.
    """
    info = _wait_for_tmux_info(bridge_dir, timeout_s=timeout_s)
    # No ``-l``: tmux must interpret ``Escape`` as a key name.
    _run_tmux(info["socket_path"], "send-keys", "-t", info["tmux_target"], "Escape")


def kill_session(bridge_dir: Path, *, timeout_s: float = _TMUX_READY_TIMEOUT_S) -> None:
    """Hard-stop the Cursor session by killing its tmux session.

    Terminates ``cursor-agent`` and the pane outright — the analog of the
    user manually exiting the attached TUI, for the web UI's "Stop session"
    affordance. Mirrors :func:`omnigent.claude_native_bridge.kill_session`.

    :raises RuntimeError: If the tmux target is not advertised or kill-session fails.
    """
    info = _wait_for_tmux_info(bridge_dir, timeout_s=timeout_s)
    _run_tmux(info["socket_path"], "kill-session", "-t", info["tmux_target"])
