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
from dataclasses import field as dataclass_field
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
    :param server_url: Omnigent server base URL for hook/elicitation calls.
    :param headers: Auth headers the runner carries, for those calls.
    """

    ws_url: str
    session_id: str = ""
    pid: int = 0
    server_url: str = ""
    headers: dict[str, str] = dataclass_field(default_factory=dict)


def bridge_state_path(bridge_dir: Path) -> Path:
    """Return the state file path under the bridge dir."""
    return Path(bridge_dir) / _STATE_FILENAME


def write_bridge_state(bridge_dir: Path, state: GrokBridgeState) -> None:
    """Persist :class:`GrokBridgeState` atomically enough for consumers."""
    path = bridge_state_path(bridge_dir)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(
            {
                "ws_url": state.ws_url,
                "session_id": state.session_id,
                "pid": state.pid,
                "server_url": state.server_url,
                "headers": state.headers,
            }
        )
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
    headers = raw.get("headers")
    return GrokBridgeState(
        ws_url=str(raw["ws_url"]),
        session_id=str(raw.get("session_id") or ""),
        pid=int(raw.get("pid") or 0),
        server_url=str(raw.get("server_url") or ""),
        headers={str(k): str(v) for k, v in headers.items()} if isinstance(headers, dict) else {},
    )


def bridge_dir_from_env() -> Path:
    """Resolve the bridge dir from the harness spawn environment."""
    raw = os.environ.get(GROK_NATIVE_BRIDGE_DIR_ENV_VAR, "").strip()
    if not raw:
        raise RuntimeError(f"{GROK_NATIVE_BRIDGE_DIR_ENV_VAR} is required for grok-native")
    return Path(raw)


def bridge_dir_for_session_id(session_id: str) -> Path:
    """Return the per-session bridge dir under the Omnigent data dir."""
    import tempfile

    base = Path(tempfile.gettempdir()) / "omnigent-grok-native"
    return base / session_id


def prepare_bridge_dir(session_id: str) -> Path:
    """Create and return the per-session bridge dir."""
    bridge_dir = bridge_dir_for_session_id(session_id)
    bridge_dir.mkdir(parents=True, exist_ok=True)
    return bridge_dir


def build_grok_native_spawn_env(
    session_id: str,
    *,
    provider: str | None = None,
    model: str | None = None,
) -> dict[str, str]:
    """Build the ``GROK_NATIVE_*`` env the harness executor reads.

    grok owns its own auth and model selection, so ``provider``/``model`` are
    accepted for signature parity and ignored — a ``/model`` pick is rejected
    up front by the OWN_AUTH gate, matching the ACP rows.

    :param session_id: The Omnigent session id (keys the bridge dir).
    :returns: Env-var overrides for the harness spawn.
    """
    del provider, model
    return {GROK_NATIVE_BRIDGE_DIR_ENV_VAR: str(bridge_dir_for_session_id(session_id))}


def native_input_ready(session_id: str, instance: object) -> bool:
    """Provider ``input_ready_probe``: the serve endpoint is dialable.

    The runner-side launch writes bridge state once the serve port binds and
    ``session/new`` returns, so a state file carrying a ``ws_url`` is the
    readiness signal for web-originated prompts.

    :param session_id: Omnigent conversation id (keys the bridge dir).
    :param instance: The live terminal resource (unused; state is on disk).
    :returns: Whether a usable serve endpoint is published.
    """
    del instance
    state = read_bridge_state(bridge_dir_for_session_id(session_id))
    return state is not None and bool(state.ws_url)


__all__ = [
    "GROK_NATIVE_BRIDGE_DIR_ENV_VAR",
    "GrokBridgeState",
    "bridge_dir_for_session_id",
    "bridge_dir_from_env",
    "bridge_state_path",
    "build_grok_native_spawn_env",
    "native_input_ready",
    "prepare_bridge_dir",
    "read_bridge_state",
    "write_bridge_state",
]
