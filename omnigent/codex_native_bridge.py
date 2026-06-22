"""Bridge state for native Codex TUI sessions."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import tomllib

CODEX_NATIVE_BRIDGE_ID_LABEL_KEY = "omnigent.codex_native.bridge_id"
CODEX_NATIVE_BRIDGE_DIR_ENV_VAR = "HARNESS_CODEX_NATIVE_BRIDGE_DIR"
CODEX_NATIVE_REQUEST_SESSION_ID_ENV_VAR = "HARNESS_CODEX_NATIVE_REQUEST_SESSION_ID"

_STATE_FILE = "state.json"
_STARTUP_ERROR_FILE = "startup_error.json"
# Must match ``_CONFIG_FILE`` in ``claude_native_bridge.py`` because
# ``serve-mcp`` reads this filename for the token.
_MCP_CONFIG_FILE = "bridge.json"
# Config the codex-native PreToolUse/PostToolUse policy hook subprocess
# reads to reach the Omnigent server. Mirrors Claude-native's
# ``permission_hook.json`` (see ``claude_native_bridge``). Kept in a
# separate file from ``state.json`` because it is written once at bridge
# prep time (the Omnigent URL + auth do not change across thread rotations),
# whereas ``state.json`` mutates on every turn/thread change.
_POLICY_HOOK_FILE = "policy_hook.json"
_BRIDGE_ROOT = Path.home() / ".omnigent" / "codex-native"


def bridge_root() -> Path:
    """
    Return the configured Codex-native bridge root.

    Tests may monkeypatch :data:`_BRIDGE_ROOT` to isolate bridge files.

    :returns: Absolute root for Codex-native bridge directories, e.g.
        ``Path("~/.omnigent/codex-native")``.
    """
    return _BRIDGE_ROOT


@dataclass(frozen=True)
class CodexNativeBridgeState:
    """
    Runtime state shared by the native Codex wrapper and harness.

    :param session_id: Omnigent conversation id, e.g.
        ``"conv_abc123"``.
    :param socket_path: Unix socket path for the Codex app-server,
        e.g. ``"/home/user/.omnigent/codex-native/x/app-server.sock"``.
    :param thread_id: Codex app-server thread id, e.g.
        ``"0196..."``.
    :param codex_home: Private per-session ``CODEX_HOME`` path, e.g.
        ``"/home/user/.omnigent/codex-native/x/codex-home"``.
    :param active_turn_id: Current Codex turn id, if one is running,
        e.g. ``"turn_abc123"``.
    """

    session_id: str
    socket_path: str
    thread_id: str
    codex_home: str
    active_turn_id: str | None = None


def bridge_dir_for_bridge_id(bridge_id: str) -> Path:
    """
    Return the bridge directory for a native Codex bridge id.

    :param bridge_id: Opaque bridge id, e.g. ``"bridge_abc123"``.
    :returns: Absolute bridge directory under
        ``~/.omnigent/codex-native``.
    """
    digest = hashlib.sha256(bridge_id.encode("utf-8")).hexdigest()[:32]
    return _BRIDGE_ROOT / digest


def build_codex_native_spawn_env(
    conversation_id: str,
    *,
    bridge_id: str | None = None,
) -> dict[str, str]:
    """
    Build spawn env for the ``codex-native`` harness process.

    :param conversation_id: Omnigent conversation id, e.g.
        ``"conv_abc123"``.
    :param bridge_id: Opaque bridge id from
        :data:`CODEX_NATIVE_BRIDGE_ID_LABEL_KEY`, e.g.
        ``"bridge_abc123"``. ``None`` uses *conversation_id*.
    :returns: Environment variables needed by the Codex-native
        harness executor.
    """
    resolved_bridge_id = bridge_id or conversation_id
    return {
        CODEX_NATIVE_BRIDGE_DIR_ENV_VAR: str(bridge_dir_for_bridge_id(resolved_bridge_id)),
        CODEX_NATIVE_REQUEST_SESSION_ID_ENV_VAR: conversation_id,
    }


def prepare_bridge_dir(bridge_id: str) -> Path:
    """
    Create the bridge directory for *bridge_id*.

    :param bridge_id: Opaque bridge id, e.g. ``"bridge_abc123"``.
    :returns: Prepared absolute bridge directory.
    """
    bridge_dir = bridge_dir_for_bridge_id(bridge_id)
    bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(bridge_dir, 0o700)
    return bridge_dir


def write_mcp_bridge_config(bridge_dir: Path) -> None:
    """
    Write a minimal ``bridge.json`` so ``serve-mcp`` can boot.

    The config contains only an authentication token (no ``workspace``
    key), so the MCP server serves **relay tools only** (from
    ``tool_relay.json``) — no ``sys_os_*`` tools. This is correct for
    codex-native: Codex owns its own filesystem tools.

    Idempotent: skips if a config already exists (avoids overwriting
    a token that the relay HTTP server was started with).

    :param bridge_dir: Codex bridge directory, e.g.
        ``Path("~/.omnigent/codex-native/<hash>")``.
    """
    config_path = bridge_dir / _MCP_CONFIG_FILE
    if config_path.exists():
        return
    bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = {"token": secrets.token_urlsafe(32)}
    fd, tmp_name = tempfile.mkstemp(prefix=f"{_MCP_CONFIG_FILE}.", dir=str(bridge_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_name, config_path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def codex_mcp_config_overrides(
    bridge_dir: Path,
    *,
    python_executable: str | None = None,
) -> list[str]:
    """
    Return ``-c`` config overrides that register the Omnigent MCP server.

    The overrides configure codex to launch ``serve-mcp`` from
    :mod:`omnigent.claude_native_bridge` as a stdio MCP server.
    ``serve-mcp`` reads ``tool_relay.json`` from *bridge_dir*
    dynamically on every ``tools/list`` call, so relay tools appear
    as soon as the runner writes the file.

    :param bridge_dir: Codex bridge directory containing
        ``bridge.json`` and (eventually) ``tool_relay.json``.
    :param python_executable: Python executable to run, e.g.
        ``"/path/to/python"``. ``None`` uses :data:`sys.executable`.
    :returns: Codex ``-c`` config override strings, e.g.
        ``['mcp_servers.omnigent.command="python"', ...]``.
    """
    python = python_executable or sys.executable
    args_toml = json.dumps(
        ["-m", "omnigent.claude_native_bridge", "serve-mcp", "--bridge-dir", str(bridge_dir)]
    )
    return [
        f'mcp_servers.omnigent.command="{python}"',
        f"mcp_servers.omnigent.args={args_toml}",
    ]


def write_policy_hook_config(
    bridge_dir: Path,
    *,
    ap_server_url: str,
    ap_auth_headers: dict[str, str],
) -> None:
    """
    Write the Omnigent coordinates the codex-native policy hook needs.

    The ``PreToolUse`` / ``PostToolUse`` command hook runs as a short
    subprocess that must POST to ``/v1/sessions/{id}/policies/evaluate``
    on the Omnigent server. It cannot inherit the long-lived forwarder's
    in-memory client, so the Omnigent base URL and auth headers are persisted
    here and read by :func:`read_policy_hook_config` at hook time.

    :param bridge_dir: Native Codex bridge directory, e.g.
        ``Path("~/.omnigent/codex-native/<hash>")``.
    :param ap_server_url: Omnigent server base URL the hook POSTs to, e.g.
        ``"http://127.0.0.1:8787"``.
    :param ap_auth_headers: Outbound auth headers for Omnigent requests, e.g.
        ``{"Authorization": "Bearer <token>"}``. Empty dict for
        local-server mode with no auth provider.
    :returns: None.
    """
    bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = bridge_dir / _POLICY_HOOK_FILE
    payload = {"ap_server_url": ap_server_url, "ap_auth_headers": ap_auth_headers}
    fd, tmp_name = tempfile.mkstemp(prefix=f"{_POLICY_HOOK_FILE}.", dir=str(bridge_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def read_policy_hook_config(bridge_dir: Path) -> dict[str, object] | None:
    """
    Read the Omnigent coordinates for the codex-native policy hook.

    :param bridge_dir: Native Codex bridge directory.
    :returns: Parsed config, e.g.
        ``{"ap_server_url": "http://127.0.0.1:8787",
        "ap_auth_headers": {"Authorization": "Bearer <token>"}}``, or
        ``None`` when no config has been written (no Omnigent server
        configured for this session).
    """
    path = bridge_dir / _POLICY_HOOK_FILE
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    return raw


def socket_path_for_bridge_dir(bridge_dir: Path) -> Path:
    """
    Return the Codex app-server socket path for *bridge_dir*.

    :param bridge_dir: Native Codex bridge directory.
    :returns: Absolute Unix socket path for the app-server.
    """
    return bridge_dir / "app-server.sock"


def codex_home_for_bridge_dir(bridge_dir: Path) -> Path:
    """
    Return the private ``CODEX_HOME`` path for *bridge_dir*.

    :param bridge_dir: Native Codex bridge directory.
    :returns: Absolute per-session ``CODEX_HOME`` directory.
    """
    return bridge_dir / "codex-home"


def read_codex_config_model(bridge_dir: Path) -> str | None:
    """
    Read the active model from this session's Codex ``config.toml``.

    The top-level ``model`` key is exactly what an in-TUI ``/model`` writes
    (codex's ``config/batchWrite``), so it is the source of truth for which
    model the user has selected. Reading it from the hook at evaluation time
    is race-free: unlike the forwarder's async ``external_model_change``
    mirror to ``model_override``, the value is read synchronously the instant
    the cost gate needs it, so a ``/model`` switch takes effect on the very
    next tool call.

    Best-effort + fail-safe: a missing / unreadable / unparsable file (or a
    config with no top-level ``model``) returns ``None``, so the caller falls
    back to the server-resolved model rather than crashing.

    KNOWN LIMITATION — this file is SHARED across all codex sessions.
    ``config.toml`` here is symlinked to the single real ``~/.codex/config.toml``
    (see ``_CODEX_HOME_CONFIG_FILES`` /  ``_populate_codex_home_config`` in
    ``omnigent.inner.codex_executor``). So the model returned is whatever the
    LAST ``/model`` from ANY codex session wrote — it is NOT per-session.
    Consequences for the cost gate: (1) one session's ``/model`` to a cheap
    model makes a *different* session still on an EXPENSIVE model read the
    cheap value and never get gated; (2) a freshly spawned session reads a
    leftover value that may not match its own launch ``--model``. Acceptable
    for single-user/local use; a hole under concurrent/multi-user codex
    sessions. Fix (not yet done): copy config.toml per-session + seed it with
    the session's launch model.

    :param bridge_dir: The session's native-Codex bridge directory.
    :returns: The top-level ``model`` from ``config.toml`` (e.g.
        ``"gpt-5.4"``), or ``None`` when undeterminable.
    """
    config_path = codex_home_for_bridge_dir(bridge_dir) / "config.toml"
    try:
        data = tomllib.loads(config_path.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return None
    model = data.get("model")
    return model if isinstance(model, str) and model else None


def write_bridge_state(bridge_dir: Path, state: CodexNativeBridgeState) -> None:
    """
    Persist shared native Codex state atomically.

    :param bridge_dir: Native Codex bridge directory.
    :param state: State payload to persist.
    :returns: None.
    """
    bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = bridge_dir / _STATE_FILE
    fd, tmp_name = tempfile.mkstemp(prefix=f"{_STATE_FILE}.", dir=str(bridge_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "session_id": state.session_id,
                    "socket_path": state.socket_path,
                    "thread_id": state.thread_id,
                    "codex_home": state.codex_home,
                    "active_turn_id": state.active_turn_id,
                },
                handle,
                sort_keys=True,
            )
            handle.write("\n")
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def clear_bridge_state(bridge_dir: Path) -> None:
    """
    Remove stale native Codex runtime state for a bridge directory.

    New app-server launches reuse the same bridge directory for a
    conversation id, but the old ``state.json`` may point at a thread
    from a previous app-server process. Clear it before starting the new
    server so web message forwarding waits for the new launch to publish
    its current transport and thread instead of injecting into stale
    state.

    :param bridge_dir: Native Codex bridge directory.
    :returns: None.
    """
    for name in (_STATE_FILE, _STARTUP_ERROR_FILE):
        try:
            (bridge_dir / name).unlink()
        except FileNotFoundError:
            continue


def write_bridge_startup_error(bridge_dir: Path, message: str) -> None:
    """
    Record why a native Codex app-server never started its thread (issue #59).

    :param bridge_dir: Native Codex bridge directory.
    :param message: Human-readable failure cause.
    :returns: None.
    """
    try:
        bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = bridge_dir / _STARTUP_ERROR_FILE
        fd, tmp_name = tempfile.mkstemp(prefix=f"{_STARTUP_ERROR_FILE}.", dir=str(bridge_dir))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"message": message}, handle, sort_keys=True)
                handle.write("\n")
            os.replace(tmp_name, path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
    except OSError:
        return  # best-effort; the real failure is already logged


def read_bridge_startup_error(bridge_dir: Path) -> str | None:
    """
    Read a recorded native Codex startup-failure message, if any.

    :param bridge_dir: Native Codex bridge directory.
    :returns: The recorded failure cause, or ``None`` if absent/unreadable.
    """
    path = bridge_dir / _STARTUP_ERROR_FILE
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    message = raw.get("message")
    return message if isinstance(message, str) and message else None


def read_bridge_state(bridge_dir: Path) -> CodexNativeBridgeState | None:
    """
    Read shared native Codex bridge state.

    :param bridge_dir: Native Codex bridge directory.
    :returns: Parsed state, or ``None`` when no state exists.
    """
    path = bridge_dir / _STATE_FILE
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    session_id = raw.get("session_id")
    socket_path = raw.get("socket_path")
    thread_id = raw.get("thread_id")
    codex_home = raw.get("codex_home")
    active_turn_id = raw.get("active_turn_id")
    required = (session_id, socket_path, thread_id, codex_home)
    if not all(isinstance(value, str) and value for value in required):
        return None
    parsed_active_turn_id = (
        active_turn_id if isinstance(active_turn_id, str) and active_turn_id else None
    )
    return CodexNativeBridgeState(
        session_id=session_id,
        socket_path=socket_path,
        thread_id=thread_id,
        codex_home=codex_home,
        active_turn_id=parsed_active_turn_id,
    )


def update_active_turn_id(bridge_dir: Path, active_turn_id: str | None) -> None:
    """
    Update the active Codex turn id in bridge state.

    :param bridge_dir: Native Codex bridge directory.
    :param active_turn_id: Active turn id, e.g. ``"turn_abc123"``,
        or ``None`` when no turn is running.
    :returns: None.
    """
    state = read_bridge_state(bridge_dir)
    if state is None:
        return
    write_bridge_state(
        bridge_dir,
        CodexNativeBridgeState(
            session_id=state.session_id,
            socket_path=state.socket_path,
            thread_id=state.thread_id,
            codex_home=state.codex_home,
            active_turn_id=active_turn_id,
        ),
    )


def update_thread_id(bridge_dir: Path, thread_id: str, active_turn_id: str | None = None) -> None:
    """
    Update the Codex thread id in bridge state.

    Used when a native Codex action creates a fresh thread while the
    Omnigent session stays the same.

    :param bridge_dir: Native Codex bridge directory.
    :param thread_id: New Codex thread id, e.g. ``"thread_abc123"``.
    :param active_turn_id: Active turn id for the new thread, e.g.
        ``"turn_abc123"``, or ``None`` when no turn is running yet.
    :returns: None.
    """
    state = read_bridge_state(bridge_dir)
    if state is None:
        return
    write_bridge_state(
        bridge_dir,
        CodexNativeBridgeState(
            session_id=state.session_id,
            socket_path=state.socket_path,
            thread_id=thread_id,
            codex_home=state.codex_home,
            active_turn_id=active_turn_id,
        ),
    )


def clear_active_turn_id_if_matches(bridge_dir: Path, completed_turn_id: str | None) -> bool:
    """
    Clear the active Codex turn id if a terminal event matches it.

    Terminal Codex notifications can race with a newer ``turn/started``
    notification under rapid web sends. A stale terminal event must not
    erase the newer active turn id, or later web messages stop steering
    the running native Codex turn.

    A terminal event without a turn id is ambiguous: it cannot be
    correlated to the active turn, so when a turn is live it is ignored
    rather than clearing the turn. Clearing it would post a premature
    ``idle`` to the session and hide the "working" spinner while Codex is
    still mid-turn. (Codex includes a turn id on real terminal events;
    the id-less shape is a legacy/malformed edge case.)

    :param bridge_dir: Native Codex bridge directory.
    :param completed_turn_id: Completed or failed turn id, e.g.
        ``"turn_abc123"``. ``None`` means Codex did not include an id;
        if a turn is live it is left intact (returns ``False``), and if
        no turn is live the call is a no-op (returns ``True``).
    :returns: ``True`` when bridge state was cleared or did not exist,
        ``False`` when a stale or ambiguous terminal event was ignored.
    """
    state = read_bridge_state(bridge_dir)
    if state is None:
        return True
    if completed_turn_id is None:
        # No-id terminal mid-turn is ambiguous — ignore (clearing posts a premature idle).
        if state.active_turn_id is not None:
            return False
    elif state.active_turn_id != completed_turn_id:
        return False
    write_bridge_state(
        bridge_dir,
        CodexNativeBridgeState(
            session_id=state.session_id,
            socket_path=state.socket_path,
            thread_id=state.thread_id,
            codex_home=state.codex_home,
            active_turn_id=None,
        ),
    )
    return True
