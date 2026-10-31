"""Crash-safe registry of runner-owned ``opencode serve`` processes.

Reuses the native Codex registry format and reconciliation with an
OpenCode-specific file, so a runner killed before it closes its servers
leaves entries the host janitor can reap. A runner killed between spawning a
server and registering it leaves that server unrecorded.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

from omnigent.harnesses.codex_native.process_registry import (
    CodexNativeProcessOwnerLock,
    acquire_codex_native_process_owner_lock,
    reconcile_codex_native_process_registry,
    register_codex_native_process,
    unregister_codex_native_process,
)
from omnigent.process_logging import data_dir

_STATE_DIR = "opencode-native-processes"
_REGISTRY_FILE = "process-registry.json"


def opencode_native_process_state_root() -> Path:
    """
    Return the directory holding the registry and owner locks.

    Kept outside the bridge root so orphaned-bridge pruning never removes
    a live runner's owner lock.

    :returns: ``<data-dir>/opencode-native-processes``.
    """
    return data_dir() / _STATE_DIR


def opencode_native_process_registry_path() -> Path:
    """
    Return the on-disk registry file path.

    :returns: Registry path under :func:`opencode_native_process_state_root`.
    """
    return opencode_native_process_state_root() / _REGISTRY_FILE


@dataclass
class OpenCodeNativeServerRegistration:
    """
    Registry entry and owner lock for one running ``opencode serve``.

    :param session_tag: Unique registry tag for the server process.
    :param owner_lock: Lock held for as long as this process owns the server.
    """

    session_tag: str
    owner_lock: CodexNativeProcessOwnerLock

    def release(self) -> None:
        """
        Drop the registry entry and its owner lock after the server exits.

        :returns: None.
        """
        unregister_codex_native_process(
            self.session_tag,
            registry_path=opencode_native_process_registry_path(),
        )
        self.owner_lock.close()


def register_opencode_native_server(pid: int) -> OpenCodeNativeServerRegistration | None:
    """
    Record a server started as its own process-group leader.

    :param pid: Server process id, which is also its process group id.
    :returns: The registration, or ``None`` when owner locks are unavailable.
    """
    owner_lock = acquire_codex_native_process_owner_lock(opencode_native_process_state_root())
    if owner_lock is None:
        return None
    session_tag = f"opencode-native-{uuid.uuid4().hex}"
    try:
        register_codex_native_process(
            pid=pid,
            pgid=pid,
            session_tag=session_tag,
            owner_lock_path=owner_lock.path,
            registry_path=opencode_native_process_registry_path(),
        )
    except BaseException:
        owner_lock.close()
        raise
    return OpenCodeNativeServerRegistration(session_tag=session_tag, owner_lock=owner_lock)


def reconcile_opencode_native_process_registry() -> None:
    """
    Terminate servers whose owning runner died without closing them.

    :returns: None.
    """
    reconcile_codex_native_process_registry(registry_path=opencode_native_process_registry_path())
