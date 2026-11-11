"""Bridge state for the grok-native serve endpoint.

The runner-side launcher spawns ``grok agent serve`` and persists the
endpoint coordinates the harness-side executor needs: the WS URL (with the
per-session secret), and the grok session id once ``session/new`` completes.
State lives in ``$GROK_NATIVE_BRIDGE_DIR/bridge_state.json`` so the inner
harness process (a different address space) can drive ``session/prompt``
into the same running server.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

GROK_NATIVE_BRIDGE_DIR_ENV_VAR = "GROK_NATIVE_BRIDGE_DIR"
_STATE_FILENAME = "bridge_state.json"


@dataclass(frozen=True)
class GrokBridgeState:
    """Persisted serve endpoint coordinates.

    :param ws_url: ``ws://host:port/ws?server-key=...`` for the session's server.
    :param session_id: Grok session id returned by ``session/new`` (empty
        until the first session is created).
    :param pid: Serve process pid (informational; ownership stays with the
        runner resource).
    """

    ws_url: str
    session_id: str = ""
    pid: int = 0


def bridge_state_path(bridge_dir: Path) -> Path:
    """Return the state file path under the bridge dir."""
    return Path(bridge_dir) / _STATE_FILENAME


def write_bridge_state(bridge_dir: Path, state: GrokBridgeState) -> None:
    """Persist :class:`GrokBridgeState` atomically enough for consumers."""
    path = bridge_state_path(bridge_dir)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"ws_url": state.ws_url, "session_id": state.session_id, "pid": state.pid})
    )
    tmp.replace(path)


def read_bridge_state(bridge_dir: Path) -> GrokBridgeState | None:
    """Load persisted state; ``None`` when absent or malformed."""
    try:
        raw = json.loads(bridge_state_path(bridge_dir).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict) or not raw.get("ws_url"):
        return None
    return GrokBridgeState(
        ws_url=str(raw["ws_url"]),
        session_id=str(raw.get("session_id") or ""),
        pid=int(raw.get("pid") or 0),
    )


def bridge_dir_from_env() -> Path:
    """Resolve the bridge dir from the harness spawn environment."""
    raw = os.environ.get(GROK_NATIVE_BRIDGE_DIR_ENV_VAR, "").strip()
    if not raw:
        raise RuntimeError(f"{GROK_NATIVE_BRIDGE_DIR_ENV_VAR} is required for grok-native")
    return Path(raw)


__all__ = [
    "GROK_NATIVE_BRIDGE_DIR_ENV_VAR",
    "GrokBridgeState",
    "bridge_dir_from_env",
    "bridge_state_path",
    "read_bridge_state",
    "write_bridge_state",
]
