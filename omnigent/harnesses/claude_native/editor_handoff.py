"""Pair editor observations with the bridge's current native terminal owner."""

from __future__ import annotations

import math
import re
import subprocess
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import cast

import psutil

from omnigent.harnesses.claude_native import bridge
from omnigent.harnesses.claude_native.editor_owner import (
    NativeDraftOwner,
    NativeEditorProcess,
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


def _observe_binding(
    bridge_dir: Path,
    executable: Path,
    *,
    native_process: NativeEditorProcess | None = None,
) -> NativeEditorBinding | None:
    session = bridge.read_active_session_id(bridge_dir)
    native_session = bridge.read_claude_session_id(bridge_dir)
    target = bridge.read_tmux_target(bridge_dir)
    if not session or not native_session or target is None:
        return None
    process = native_process or observe_native_editor_process(executable)
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
            native.pid != process.pid
            or not native.is_running()
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


def _stored_binding(bridge_dir: Path) -> NativeEditorBinding | None:
    try:
        stored = bridge._read_json_file(bridge_dir / _OWNER_FILE)
        if (
            set(stored) != {"version", "binding"}
            or type(stored.get("version")) is not int
            or stored["version"] != 1
        ):
            return None
        value = stored.get("binding")
        if not isinstance(value, dict) or set(value) != {
            "owner",
            "socket_path",
            "tmux_target",
            "pane_pid",
            "pane_created_at",
        }:
            return None
        owner = value["owner"]
        if not isinstance(owner, dict) or set(owner) != {
            "bridge_session_id",
            "native_session_id",
            "process",
        }:
            return None
        process = owner["process"]
        if not isinstance(process, dict) or set(process) != {
            "pid",
            "created_at",
            "executable",
            "pane_id",
        }:
            return None
        strings = [
            owner["bridge_session_id"],
            owner["native_session_id"],
            process["executable"],
            process["pane_id"],
            value["socket_path"],
            value["tmux_target"],
        ]
        if not all(isinstance(item, str) and item and "\x00" not in item for item in strings):
            return None
        if not all(
            type(item) is int and 0 < item <= 2**31 - 1
            for item in (process["pid"], value["pane_pid"])
        ):
            return None
        if not all(
            type(item) in {int, float} and math.isfinite(item) and item > 0
            for item in (process["created_at"], value["pane_created_at"])
        ):
            return None
        executable = cast("str", process["executable"])
        socket_path = cast("str", value["socket_path"])
        pane_id = cast("str", process["pane_id"])
        if (
            not Path(executable).is_absolute()
            or not Path(socket_path).is_absolute()
            or re.fullmatch(r"%\d+", pane_id) is None
        ):
            return None
        return NativeEditorBinding(
            NativeDraftOwner(
                cast("str", owner["bridge_session_id"]),
                cast("str", owner["native_session_id"]),
                NativeEditorProcess(
                    cast("int", process["pid"]),
                    float(process["created_at"]),
                    executable,
                    pane_id,
                ),
            ),
            socket_path,
            cast("str", value["tmux_target"]),
            cast("int", value["pane_pid"]),
            float(value["pane_created_at"]),
        )
    except (OSError, RuntimeError, ValueError, OverflowError):
        return None


def read_editor_bridge_binding(bridge_dir: Path, executable: Path) -> NativeEditorBinding | None:
    """Reobserve the editor's live owner; stored metadata alone grants no ownership."""
    try:
        stored = _stored_binding(bridge_dir)
        observed = _observe_binding(bridge_dir, executable) if stored is not None else None
        return observed if observed == stored else None
    except (OSError, RuntimeError, ValueError):
        return None


def read_controller_editor_binding(bridge_dir: Path) -> NativeEditorBinding | None:
    """Revalidate a hook's recorded owner from the controller outside the CLI tree."""
    try:
        stored = _stored_binding(bridge_dir)
        if stored is None:
            return None
        observed = _observe_binding(
            bridge_dir,
            Path(stored.owner.process.executable),
            native_process=stored.owner.process,
        )
        return observed if observed == stored else None
    except (OSError, RuntimeError, ValueError):
        return None
