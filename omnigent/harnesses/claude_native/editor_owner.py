"""Read-only ownership checks for a native editor's exported POSIX input buffer."""

from __future__ import annotations

import math
import os
import re
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import psutil


@dataclass(frozen=True)
class NativeEditorProcess:
    """One native CLI incarnation observed by its hook or editor child."""

    pid: int
    created_at: float
    executable: str
    pane_id: str


@dataclass(frozen=True)
class NativeDraftOwner:
    """The bridge and native session paired with a particular CLI process."""

    bridge_session_id: str
    native_session_id: str
    process: NativeEditorProcess


def observe_native_editor_process(
    executable: Path, *, environment: Mapping[str, str] | None = None
) -> NativeEditorProcess | None:
    """Find the selected native executable among this child's live ancestors.

    An unavailable identity leaves editor-based ownership unverified. Names
    and environment-provided session IDs do not identify the executable.
    """
    pane_id = (os.environ if environment is None else environment).get("TMUX_PANE", "")
    if re.fullmatch(r"%\d+", pane_id) is None:
        return None
    try:
        selected = executable.resolve(strict=True)
        for parent in psutil.Process().parents()[:8]:
            if Path(parent.exe()).resolve(strict=True) != selected:
                continue
            created_at = parent.create_time()
            if (
                parent.pid <= 0
                or not math.isfinite(created_at)
                or created_at <= 0
                or not parent.is_running()
            ):
                return None
            return NativeEditorProcess(parent.pid, created_at, str(selected), pane_id)
    except (OSError, RuntimeError, psutil.Error):
        return None
    return None


def exported_draft_matches_owner(
    path: Path,
    *,
    expected_text: str,
    expected_owner: NativeDraftOwner,
    current_owner: Callable[[], NativeDraftOwner | None],
    cancelled: Callable[[], bool],
) -> bool:
    """Check exact exported bytes while the native CLI is parked in its editor.

    This observation neither clears the buffer nor establishes ownership
    after the editor returns. A later mutation needs its own ownership check.
    """
    if cancelled() or current_owner() != expected_owner:
        return False
    try:
        expected = expected_text.encode("utf-8")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_nlink != 1
                or info.st_size != len(expected)
            ):
                return False
            observed = bytearray()
            while len(observed) <= len(expected):
                chunk = os.read(descriptor, min(65536, len(expected) + 1 - len(observed)))
                if not chunk:
                    break
                observed.extend(chunk)
            return observed == expected and not cancelled() and current_owner() == expected_owner
        finally:
            os.close(descriptor)
    except (OSError, UnicodeError):
        return False
