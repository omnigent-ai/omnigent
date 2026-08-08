"""Persistent server-side state that survives process restarts.

Stores lightweight transition markers (auth source, single-user flag) in a
JSON sidecar in the data dir. This lets startup detect single-user ↔
multi-user auth-mode flips and migrate session ownership accordingly, without
querying the whole permissions table on every boot.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


def _server_state_path() -> Path:
    """Location of ``server-state.json`` in the runtime data dir.

    Mirrors :func:`omnigent.host.local_server._local_data_dir` so the file
    always lives next to ``local_server.pid`` and the default SQLite DB.
    """
    value = os.environ.get("OMNIGENT_DATA_DIR")
    if value:
        return Path(value).expanduser() / "server-state.json"
    return Path.home() / ".omnigent" / "server-state.json"


@dataclass(frozen=True)
class ServerState:
    """Last-known auth posture written by the previous server process."""

    last_auth_source: str | None = None
    last_local_single_user: bool = False


def load_server_state() -> ServerState:
    """Read the sidecar file, returning a blank state if it is missing."""
    path = _server_state_path()
    if not path.exists():
        return ServerState()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ServerState()
    return ServerState(
        last_auth_source=data.get("last_auth_source"),
        last_local_single_user=bool(data.get("last_local_single_user")),
    )


def save_server_state(state: ServerState) -> None:
    """Persist the current auth posture for the next process."""
    path = _server_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "last_auth_source": state.last_auth_source,
                "last_local_single_user": state.last_local_single_user,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
