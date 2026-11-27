"""Pair editor observations with the bridge's current native terminal owner."""

from __future__ import annotations

import math
import subprocess
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

import psutil

from omnigent.harnesses.claude_native import bridge
from omnigent.harnesses.claude_native.editor_owner import (
    NativeDraftOwner,
    observe_native_editor_process,
)

_OWNER_FILE = "editor-owner.json"


@dataclass(frozen=True)
class NativeEditorBinding:
    """One bridge/native pair on an observed pane process incarnation."""

    owner: NativeDraftOwner
    socket_path: str
    tmux_target: str
    pane_pid: int
    pane_created_at: float


def _observe_binding(bridge_dir: Path, executable: Path) -> NativeEditorBinding | None:
    session = bridge.read_active_session_id(bridge_dir)
    native_session = bridge.read_claude_session_id(bridge_dir)
    target = bridge.read_tmux_target(bridge_dir)
    if not session or not native_session or target is None:
        return None
    process = observe_native_editor_process(executable)
    if process is None:
        return None
    try:
        pane = subprocess.run(
            [
                "tmux",
                "-S",
                target["socket_path"],
                "display-message",
                "-p",
                "-t",
                target["tmux_target"],
                "#{pane_id} #{pane_pid}",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
        fields = pane.stdout.split()
        if (
            pane.returncode != 0
            or len(fields) != 2
            or fields[0] != process.pane_id
            or not fields[1].isdigit()
            or int(fields[1]) <= 0
        ):
            return None
        root = psutil.Process(int(fields[1]))
        native = psutil.Process(process.pid)
        if (
            not native.is_running()
            or native.create_time() != process.created_at
            or str(Path(native.exe()).resolve(strict=True)) != process.executable
            or not any(parent.pid == root.pid for parent in [native, *native.parents()])
            or not root.is_running()
        ):
            return None
        created_at = root.create_time()
        if not math.isfinite(created_at) or created_at <= 0:
            return None
    except (OSError, RuntimeError, subprocess.SubprocessError, psutil.Error):
        return None
    if (
        bridge.read_active_session_id(bridge_dir) != session
        or bridge.read_claude_session_id(bridge_dir) != native_session
        or bridge.read_tmux_target(bridge_dir) != target
    ):
        return None
    return NativeEditorBinding(
        NativeDraftOwner(session, native_session, process),
        target["socket_path"],
        target["tmux_target"],
        root.pid,
        created_at,
    )


def record_editor_bridge_binding(
    bridge_dir: Path, payload: Mapping[str, object], executable: Path
) -> NativeEditorBinding | None:
    """Record only a top-level hook's current, physically paired native owner."""
    event = payload.get("hook_event_name")
    if (
        not isinstance(event, str)
        or event not in {"SessionStart", "UserPromptSubmit"}
        or payload.get("agent_id")
        or (
            isinstance(transcript := payload.get("transcript_path"), str)
            and "subagents" in Path(transcript).parts
        )
    ):
        return None
    observed = _observe_binding(bridge_dir, executable)
    if observed is None or payload.get("session_id") != observed.owner.native_session_id:
        return None
    bridge._write_json_file(bridge_dir / _OWNER_FILE, {"version": 1, "binding": asdict(observed)})
    return observed


def read_editor_bridge_binding(bridge_dir: Path, executable: Path) -> NativeEditorBinding | None:
    """Reobserve the editor's live owner; stored metadata alone grants no ownership."""
    try:
        stored = bridge._read_json_file(bridge_dir / _OWNER_FILE)
        if type(stored.get("version")) is not int or stored["version"] != 1:
            return None
        observed = _observe_binding(bridge_dir, executable)
        if observed is None or stored != {"version": 1, "binding": asdict(observed)}:
            return None
        return observed
    except (OSError, RuntimeError, ValueError):
        return None
