"""Qualify process ownership before deleting shared native runtime state."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from omnigent._platform import IS_LINUX

OWNER_PID_FILENAME = "owner.pid"


@dataclass(frozen=True)
class OwnerClaim:
    """A process identifier qualified by its PID namespace and boot."""

    pid: int
    pid_ns: str
    boot_id: str | None


def current_pid_namespace() -> str | None:
    """Return a namespace identity, or None when Linux cannot identify it."""
    if not IS_LINUX:
        return "none"
    try:
        return os.readlink("/proc/self/ns/pid")
    except OSError:
        return None


def current_boot_id() -> str | None:
    """Return the kernel boot identity when available."""
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip() or None
    except (OSError, UnicodeError):
        return None


def write_owner_claim(directory: Path) -> None:
    """Record the current owner; older integer-only readers skip this format."""
    lines = [str(os.getpid()), f"pid_ns={current_pid_namespace() or ''}"]
    boot_id = current_boot_id()
    if boot_id is not None:
        lines.append(f"boot={boot_id}")
    (directory / OWNER_PID_FILENAME).write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_owner_claim(directory: Path) -> OwnerClaim | None:
    """Read qualified ownership, conservatively skipping unknown or legacy claims."""
    try:
        raw = (directory / OWNER_PID_FILENAME).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if not lines:
        return None
    try:
        pid = int(lines[0])
    except ValueError:
        return None
    fields = dict(line.split("=", 1) for line in lines[1:] if "=" in line)
    pid_ns = fields.get("pid_ns")
    if pid <= 0 or not pid_ns:
        return None
    return OwnerClaim(pid=pid, pid_ns=pid_ns, boot_id=fields.get("boot") or None)


def owner_is_gone(claim: OwnerClaim, *, process_alive: Callable[[int], bool]) -> bool:
    """Require a dead process in the same boot and known namespace."""
    boot_id = current_boot_id()
    # Different boot IDs can belong to live kernels sharing a filesystem.
    if claim.boot_id != boot_id or (IS_LINUX and boot_id is None):
        return False
    pid_ns = current_pid_namespace()
    if pid_ns is None or claim.pid_ns != pid_ns:
        return False
    return not process_alive(claim.pid)
