"""Bridge state for the cline-native hub endpoint.

The runner-side launcher ensures ``cline hub`` is running and persists the
coordinates the harness-side executor needs: the WS URL, the hub auth token,
and the hub session id once ``session.create`` completes. State lives in
``$CLINE_NATIVE_BRIDGE_DIR/bridge_state.json`` so the inner harness process
(a different address space) can drive ``session.send_input`` into the same
running hub.
"""

from __future__ import annotations

import contextlib
import json
import os
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path

CLINE_NATIVE_BRIDGE_DIR_ENV_VAR = "CLINE_NATIVE_BRIDGE_DIR"
_STATE_FILENAME = "bridge_state.json"


@dataclass(frozen=True)
class ClineBridgeState:
    """Persisted hub endpoint coordinates.

    :param ws_url: ``ws://host:port/hub`` for the session's hub daemon.
    :param auth_token: Hub auth token sent as the ``cline-hub-auth.``
        WebSocket subprotocol.
    :param session_id: Hub session id returned by ``session.create`` (empty
        until the first session is created).
    :param pid: Hub daemon pid (informational; ownership stays with the
        runner resource).
    :param server_url: Omnigent server base URL for elicitation calls.
    :param headers: Auth headers the runner carries, for those calls.
    """

    ws_url: str
    auth_token: str = ""
    session_id: str = ""
    pid: int = 0
    server_url: str = ""
    headers: dict[str, str] = dataclass_field(default_factory=dict)


def bridge_state_path(bridge_dir: Path) -> Path:
    """Return the state file path under the bridge dir."""
    return Path(bridge_dir) / _STATE_FILENAME


def write_bridge_state(bridge_dir: Path, state: ClineBridgeState) -> None:
    """Persist :class:`ClineBridgeState` atomically enough for consumers."""
    path = bridge_state_path(bridge_dir)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(
            {
                "ws_url": state.ws_url,
                "auth_token": state.auth_token,
                "session_id": state.session_id,
                "pid": state.pid,
                "server_url": state.server_url,
                "headers": state.headers,
            }
        )
    )
    tmp.replace(path)


def read_bridge_state(bridge_dir: Path) -> ClineBridgeState | None:
    """Load persisted state; ``None`` when absent or malformed."""
    try:
        raw = json.loads(bridge_state_path(bridge_dir).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict) or not raw.get("ws_url"):
        return None
    headers = raw.get("headers")
    return ClineBridgeState(
        ws_url=str(raw["ws_url"]),
        auth_token=str(raw.get("auth_token") or ""),
        session_id=str(raw.get("session_id") or ""),
        pid=int(raw.get("pid") or 0),
        server_url=str(raw.get("server_url") or ""),
        headers={str(k): str(v) for k, v in headers.items()} if isinstance(headers, dict) else {},
    )


def clear_bridge_state(bridge_dir: Path) -> None:
    """Drop persisted state so a fresh launch can't be mistaken for ready."""
    with contextlib.suppress(FileNotFoundError):
        bridge_state_path(bridge_dir).unlink()


def bridge_dir_from_env() -> Path:
    """Resolve the bridge dir from the harness spawn environment."""
    raw = os.environ.get(CLINE_NATIVE_BRIDGE_DIR_ENV_VAR, "").strip()
    if not raw:
        raise RuntimeError(f"{CLINE_NATIVE_BRIDGE_DIR_ENV_VAR} is required for cline-native")
    return Path(raw)


def bridge_dir_for_session_id(session_id: str) -> Path:
    """Return the per-session bridge dir under the temp dir."""
    import tempfile

    base = Path(tempfile.gettempdir()) / "omnigent-cline-native"
    return base / session_id


def prepare_bridge_dir(session_id: str) -> Path:
    """Create and return the per-session bridge dir."""
    bridge_dir = bridge_dir_for_session_id(session_id)
    bridge_dir.mkdir(parents=True, exist_ok=True)
    return bridge_dir


def build_cline_native_spawn_env(
    session_id: str,
    *,
    provider: str | None = None,
    model: str | None = None,
) -> dict[str, str]:
    """Build the ``CLINE_NATIVE_*`` env the harness executor reads.

    Cline owns its own auth and model selection, so ``provider``/``model``
    are accepted for signature parity and ignored — a ``/model`` pick is
    rejected up front by the OWN_AUTH gate, matching the ACP rows.

    :param session_id: The Omnigent session id (keys the bridge dir).
    :returns: Env-var overrides for the harness spawn.
    """
    del provider, model
    return {CLINE_NATIVE_BRIDGE_DIR_ENV_VAR: str(bridge_dir_for_session_id(session_id))}


def native_input_ready(session_id: str, instance: object) -> bool:
    """Provider ``input_ready_probe``: the hub session is dialable.

    The runner-side launch writes bridge state once the hub is up and
    ``session.create`` returns, so a state file carrying a ``session_id``
    is the readiness signal for web-originated prompts.

    :param session_id: Omnigent conversation id (keys the bridge dir).
    :param instance: The live terminal resource (unused; state is on disk).
    :returns: Whether a usable hub session is published.
    """
    del instance
    state = read_bridge_state(bridge_dir_for_session_id(session_id))
    return state is not None and bool(state.session_id)


__all__ = [
    "CLINE_NATIVE_BRIDGE_DIR_ENV_VAR",
    "ClineBridgeState",
    "bridge_dir_for_session_id",
    "bridge_dir_from_env",
    "bridge_state_path",
    "build_cline_native_spawn_env",
    "clear_bridge_state",
    "native_input_ready",
    "prepare_bridge_dir",
    "read_bridge_state",
    "write_bridge_state",
]
