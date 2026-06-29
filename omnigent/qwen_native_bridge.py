"""File + tmux bridge for the qwen-native terminal harness.

Unlike goose-/cursor-native (which simulate keystrokes via ``tmux send-keys``),
``qwen`` ships a built-in remote-control protocol that we drive through two files
the TUI is launched against:

- ``--input-file`` (:func:`input_file_path`): ``qwen`` ``watchFile``\\s it and
  parses appended JSONL commands. We append ``{"type":"submit","text":...}`` to
  deliver a web-UI turn (qwen routes it through the *same* ``submitQuery`` path
  the keyboard uses, so it renders in the TUI transcript) and
  ``{"type":"confirmation_response","request_id":...,"allowed":...}`` to answer a
  tool-approval request.
- ``--json-file`` (:func:`events_file_path`): ``qwen`` streams structured JSON
  events here while the TUI renders normally; :mod:`omnigent.qwen_native_forwarder`
  tails it.

The runner launches the TUI in a runner-owned tmux pane (for the embedded
display) and records that pane via :func:`write_tmux_target`. Message injection
is file-based, but two affordances still go through the pane because qwen's
input-file watcher has no command for them: **interrupt** (Stop → ``Escape``, see
:func:`inject_interrupt`) and **hard stop** (:func:`kill_session`).

Verified against ``qwen`` v0.18.1 (``RemoteInputWatcher`` + dual-output). See
``docs/QWEN_NATIVE_DESIGN.md``.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

#: Env var carrying the bridge dir into the harness executor process.
BRIDGE_DIR_ENV_VAR = "HARNESS_QWEN_NATIVE_BRIDGE_DIR"

#: Fixed namespace for deriving a stable qwen ``--session-id`` from an Omnigent
#: conversation id (UUIDv5). Never change it — it would orphan every existing
#: qwen recording (resume would mint a new id and lose history).
_QWEN_SESSION_NAMESPACE = uuid.UUID("6b6f3d2e-9a1c-5e84-bf0a-1d7c5a2e9f43")

_BRIDGE_ROOT = Path(os.environ.get("TMPDIR", "/tmp")) / f"omnigent-{os.getuid()}" / "qwen-native"
_TMUX_FILE = "tmux.json"
#: JSONL command file qwen watches (``--input-file``); we append to it.
_INPUT_FILE = "qwen_in.jsonl"
#: NDJSON event file qwen writes (``--json-file``); the forwarder tails it.
_EVENTS_FILE = "qwen_out.ndjson"
_TMUX_READY_TIMEOUT_S = 30.0
_TMUX_SEND_TIMEOUT_S = 10.0
_POLL_INTERVAL_S = 0.2

#: Token config the shared Omnigent MCP relay (``serve-mcp``) reads from the
#: bridge dir. Mirrors cursor-/claude-native (``cursor_native_bridge.py``).
_BRIDGE_CONFIG_FILE = "bridge.json"
#: Name qwen lists the Omnigent MCP server under (shows in ``/mcp``).
_MCP_SERVER_NAME = "omnigent"
#: Per-session MCP config passed to qwen via ``--mcp-config <path>``. Lives in
#: the bridge dir (NOT the workspace), so we never drop a file in the user's repo
#: and concurrent same-workspace sessions can't collide. CLI-provided MCP servers
#: are also ungated (no "Untrusted MCP server" prompt), unlike a project
#: ``.mcp.json`` / ``.qwen/settings.json``. The claude-native ``--mcp-config``
#: model (it writes no workspace file either).
_MCP_CONFIG_FILE = "mcp_config.json"


def bridge_dir_for_session_id(session_id: str) -> Path:
    """Return the per-session bridge dir, e.g. ``/tmp/omnigent-<uid>/qwen-native/<hash>``."""
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
    return _BRIDGE_ROOT / digest


def bridge_root() -> Path:
    """Return the configured qwen-native bridge root."""
    return _BRIDGE_ROOT


def qwen_session_id_for_conversation(conversation_id: str) -> str:
    """Return the deterministic qwen ``--session-id`` for an Omnigent conversation.

    UUIDv5 of the conversation id: stable across resumes (recomputable, never
    stored) and a valid UUID (qwen requires one). The runner launches a fresh
    session with ``--session-id <this>`` and later restores it with
    ``--resume <this>`` so the qwen TUI shows the prior conversation on resume.

    :param conversation_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :returns: A stable UUID string usable as qwen's session id.
    """
    return str(uuid.uuid5(_QWEN_SESSION_NAMESPACE, conversation_id))


def _qwen_project_slug(workspace: Path | str) -> str:
    """Return qwen's per-project directory slug for *workspace*.

    qwen keys its on-disk session store by project: the cwd's real path with
    every non-alphanumeric character replaced by ``-`` (verified against qwen
    v0.18.1 — e.g. ``/private/tmp/qwen_x`` → ``-private-tmp-qwen-x``). Uses
    ``realpath`` because the runner launches qwen with ``cwd=realpath(workspace)``
    and qwen records under ``process.cwd()``.
    """
    real = os.path.realpath(str(workspace))
    return re.sub(r"[^A-Za-z0-9]", "-", real)


def qwen_session_recording_path(session_id: str, workspace: Path | str) -> Path:
    """Return the path to qwen's on-disk chat recording for *session_id*.

    ``~/.qwen/projects/<project-slug>/chats/<session-id>.jsonl`` — the JSONL
    qwen appends interactive-session events to (``--chat-recording``, on by
    default), scoped to *workspace*'s project slug. The file may not exist yet
    (a fresh session creates it on first event). Used both to gate ``--resume``
    (:func:`qwen_session_recording_exists`) and to tail for the
    ``chat_compression`` marker (see :mod:`omnigent.qwen_native_forwarder`).
    """
    return (
        Path.home()
        / ".qwen"
        / "projects"
        / _qwen_project_slug(workspace)
        / "chats"
        / f"{session_id}.jsonl"
    )


def qwen_session_recording_exists(session_id: str, workspace: Path | str) -> bool:
    """Return whether qwen has an on-disk chat recording for *session_id* in *workspace*.

    qwen records interactive sessions (``--chat-recording``, on by default) to
    ``~/.qwen/projects/<project-slug>/chats/<session-id>.jsonl`` and resolves
    ``--resume <id>`` **relative to the current project** (cwd slug) — not
    globally. So the check must be scoped to the *launch workspace's* slug: a
    glob across all projects would report a recording made under workspace A as
    present when resuming from workspace B, choosing ``--resume`` and landing the
    user on qwen's blocking "No saved session found with ID" error screen — the
    exact failure this guard exists to prevent (moved/renamed repo, or resume
    from a different cwd). The runner uses this to choose ``--resume`` (recording
    present here) vs ``--session-id`` (fresh). A false negative (slug drift) only
    degrades to a clean fresh launch, never the blocking error.

    :param session_id: A qwen session id (see :func:`qwen_session_id_for_conversation`).
    :param workspace: The cwd qwen will be (re)launched in.
    :returns: ``True`` if a recording for *session_id* exists under *workspace*'s
        qwen project dir.
    """
    try:
        return qwen_session_recording_path(session_id, workspace).is_file()
    except OSError:
        return False


def input_file_path(bridge_dir: Path) -> Path:
    """Return the ``--input-file`` path qwen watches for JSONL commands."""
    return bridge_dir / _INPUT_FILE


def events_file_path(bridge_dir: Path) -> Path:
    """Return the ``--json-file`` path qwen writes structured events to."""
    return bridge_dir / _EVENTS_FILE


def _ensure_dir(path: Path) -> None:
    """Create *path* (and parents) with owner-only permissions."""
    path.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o700)


def _ensure_secure_bridge_dir(bridge_dir: Path) -> None:
    """Create/validate *bridge_dir* as an owner-only chain before writing secrets.

    ``_ensure_dir`` only ``mkdir(parents=True, exist_ok=True)`` + a suppressed
    ``chmod`` on the leaf: it trusts pre-existing ancestors, so on a shared host
    an attacker could pre-create ``$TMPDIR/omnigent-<uid>`` (or a deeper ancestor)
    as a symlink / world-writable dir and redirect the bridge tree. That tree now
    holds ``bridge.json`` — a bearer token for the relay's localhost control
    endpoint — so its directory must be hardened. Delegate to the same
    ``_ensure_secure_dir`` the shared relay (``start_tool_relay``) already applies
    to token-bearing trees; it rejects symlinked / non-owned / group-or-other
    accessible ancestors (the qwen-native root is in its allowlist). Lazy import
    avoids a cycle (``claude_native_bridge`` resolves qwen's ``bridge_root`` lazily
    in turn).

    :raises RuntimeError: If any ancestor fails owner-only validation.
    """
    from omnigent.claude_native_bridge import _ensure_secure_dir

    _ensure_secure_dir(bridge_dir)


def prepare_bridge_files(bridge_dir: Path) -> None:
    """Create the bridge dir and a fresh, empty input file before launch.

    qwen ``watchFile``\\s the ``--input-file`` and reads from a byte offset, so
    the path must exist when the TUI starts. We truncate it (and the events file)
    so a relaunched terminal can't replay a prior process's queued commands.
    """
    _ensure_dir(bridge_dir)
    # Truncate both so a re-created terminal starts from a clean slate.
    input_file_path(bridge_dir).write_text("", encoding="utf-8")
    events_file_path(bridge_dir).write_text("", encoding="utf-8")


def build_qwen_native_spawn_env(session_id: str) -> dict[str, str]:
    """Build the ``HARNESS_QWEN_NATIVE_*`` env the harness executor reads.

    The executor only needs the bridge dir (to locate the input file it appends
    to). qwen's model / auth / dual-output flags are set by the runner when it
    launches the TUI (``_auto_create_qwen_terminal``), not here.

    :param session_id: The Omnigent session id (keys the bridge dir).
    :returns: Env-var overrides for the harness spawn.
    """
    bridge_dir = bridge_dir_for_session_id(session_id)
    _ensure_dir(bridge_dir)
    return {BRIDGE_DIR_ENV_VAR: str(bridge_dir)}


# ---------------------------------------------------------------------------
# Omnigent MCP server config — expose Omnigent's builtin tools (sys_*,
# load_skill, web_fetch, …) to the qwen TUI so it can call them and ``/mcp``
# lists them. Reuses the shared stdio relay implemented in
# ``omnigent.claude_native_bridge serve-mcp`` (same server cursor-/claude-/
# opencode-native point at); only the *registration* surface differs per CLI.
# ---------------------------------------------------------------------------


def write_mcp_bridge_config(bridge_dir: Path) -> None:
    """Write the token config the shared Omnigent MCP relay reads at startup.

    The ``serve-mcp`` relay (spawned by qwen) reads ``bridge.json`` for a bearer
    token and exits if it's missing, so this must exist *before* qwen launches.
    ``bridge.json`` only ever holds ``{token}``; the live tool surface is
    advertised separately via the ``tool_relay.json`` that the runner's comment
    relay (``ensure_comment_relay`` → ``_ensure_comment_relay_started``) writes
    into this same bridge dir when it starts. Mirrors
    :func:`omnigent.cursor_native_bridge.write_mcp_bridge_config`.

    :raises RuntimeError: If the bridge dir fails owner-only validation
        (:func:`_ensure_secure_bridge_dir`) — the token is not written.
    """
    _ensure_secure_bridge_dir(bridge_dir)
    config_path = bridge_dir / _BRIDGE_CONFIG_FILE
    if config_path.exists():
        return
    payload = {"token": secrets.token_urlsafe(32)}
    tmp = bridge_dir / (_BRIDGE_CONFIG_FILE + ".tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, config_path)


def build_mcp_server_entry(
    bridge_dir: Path,
    *,
    python_executable: str | None = None,
) -> dict[str, Any]:
    """Build qwen's ``mcpServers.omnigent`` entry for the Omnigent relay.

    ``trust: true`` auto-approves the qwen-side MCP tool gate so the TUI doesn't
    add a second in-terminal prompt: Omnigent already gates these calls through
    its own policy/elicitation engine (surfaced as web cards by the approval
    mirror), so qwen's prompt would only be a hidden duplicate. Same rationale
    as cursor's ``autoApprove`` (see ``cursor_native_bridge.build_mcp_config``).
    """
    python = python_executable or sys.executable
    return {
        "command": python,
        "args": [
            "-I",
            "-m",
            "omnigent.claude_native_bridge",
            "serve-mcp",
            "--bridge-dir",
            str(bridge_dir),
        ],
        "env": {
            "PYTHONUNBUFFERED": "1",
            "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        },
        "trust": True,
    }


def mcp_config_path(bridge_dir: Path) -> Path:
    """Return the per-session ``--mcp-config`` file path inside the bridge dir."""
    return bridge_dir / _MCP_CONFIG_FILE


def write_mcp_config(
    bridge_dir: Path,
    *,
    python_executable: str | None = None,
) -> Path:
    """Write the per-session Omnigent MCP config for qwen's ``--mcp-config`` flag.

    Writes ``{"mcpServers": {"omnigent": ...}}`` to a file *inside the bridge dir*
    (never the workspace) and the relay token (:func:`write_mcp_bridge_config`).
    The runner passes the returned path to qwen via ``--mcp-config <path>``. Unlike
    a project ``.mcp.json`` / ``.qwen/settings.json``, a CLI-provided MCP server:

    - drops no file in the user's (often git) workspace — nothing to accidentally
      commit, nothing left behind pointing at a dead bridge dir;
    - is per-session by construction (the file and its ``--bridge-dir`` live in
      this session's bridge dir), so concurrent same-workspace sessions can't
      collide on a shared file;
    - is **not** gated behind qwen's "Untrusted MCP server" prompt (CLI servers
      carry no project/workspace scope), so no pre-approval step is needed.

    The claude-native ``--mcp-config`` model (it writes no workspace file either).

    :returns: Path to the written ``--mcp-config`` file.
    """
    write_mcp_bridge_config(bridge_dir)
    path = mcp_config_path(bridge_dir)
    payload = {
        "mcpServers": {
            _MCP_SERVER_NAME: build_mcp_server_entry(
                bridge_dir, python_executable=python_executable
            )
        }
    }
    tmp = path.with_name(f"{_MCP_CONFIG_FILE}.{secrets.token_hex(8)}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def _append_command(bridge_dir: Path, command: dict[str, Any]) -> None:
    """Append one JSONL command line to the input file qwen watches.

    A single ``write`` of one ``\\n``-terminated line is atomic enough for qwen's
    incremental ``readNewLines`` reader (it splits on newlines), so concurrent
    appends from ``run_turn`` and a confirmation response don't interleave.

    :raises RuntimeError: If the input file can't be written.
    """
    line = json.dumps(command, ensure_ascii=False) + "\n"
    try:
        _ensure_dir(bridge_dir)
        with open(input_file_path(bridge_dir), "a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
    except OSError as exc:
        raise RuntimeError(f"qwen-native could not write input command: {exc}") from exc


def wait_for_ready(
    bridge_dir: Path,
    *,
    timeout_s: float = 30.0,
    poll_interval_s: float = _POLL_INTERVAL_S,
) -> bool:
    """Block until the qwen TUI has booted its dual-output stream.

    qwen's ``RemoteInputWatcher`` initializes its read offset (``bytesRead``) to
    the **current size of the input file** when it starts watching, synchronously
    during TUI boot (before the React app renders). If we append a ``submit``
    *before* that runs, qwen initializes ``bytesRead`` past our line and never
    reads it — the message is silently dropped. The first turn of a freshly
    launched session hits exactly this race, since the harness turn fires while
    ``qwen`` is still starting up (it takes seconds).

    qwen emits its first event — a ``{"type":"system","subtype":"session_start"}``
    on the ``--json-file`` stream — only *after* the watcher's constructor (and
    thus ``startWatching``) has run. So the appearance of a ``system`` event in
    the events file is a safe "the watcher is active, ``bytesRead`` was taken on
    the still-empty input file" signal: appending after it is reliably detected.

    :param bridge_dir: The qwen-native bridge dir holding the events file.
    :param timeout_s: Max seconds to wait for the boot signal.
    :param poll_interval_s: Seconds between polls of the events file.
    :returns: ``True`` once the boot signal is seen; ``False`` on timeout (the
        caller submits anyway — best effort beats hanging the turn).
    """
    events_file = events_file_path(bridge_dir)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _events_file_has_system_event(events_file):
            return True
        time.sleep(poll_interval_s)
    return False


def _events_file_has_system_event(events_file: Path) -> bool:
    """Return whether the NDJSON events file contains a parsed ``system`` event.

    Parses line by line and checks ``event["type"] == "system"`` rather than a
    raw substring scan: a substring like ``"type":"system"`` could appear inside
    another event's payload (latching ready early and re-opening the boot race),
    and the first ``system`` event isn't guaranteed to sit within a fixed byte
    window. The boot ``session_start`` is qwen's first emitted line, so this
    returns on the first line in practice.
    """
    try:
        with open(events_file, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue  # tolerate a partial/garbled line mid-write
                if isinstance(event, dict) and event.get("type") == "system":
                    return True
    except OSError:
        return False
    return False


def submit_user_message(bridge_dir: Path, *, content: str) -> None:
    """Deliver a web-UI user message into the qwen TUI via the input file.

    Appends ``{"type":"submit","text":content}``; qwen's ``RemoteInputWatcher``
    routes it through ``submitQuery`` (the keyboard's submit path), so the message
    renders in the TUI transcript exactly like typed input.

    :param bridge_dir: The qwen-native bridge dir holding the input file.
    :param content: User text (non-empty).
    :raises RuntimeError: If *content* is empty or the input file can't be written.
    """
    if not content:
        raise RuntimeError("qwen-native submit requires non-empty content")
    _append_command(bridge_dir, {"type": "submit", "text": content})


def submit_confirmation(bridge_dir: Path, *, request_id: str, allowed: bool) -> None:
    """Answer a qwen ``can_use_tool`` control request via the input file.

    :param bridge_dir: The qwen-native bridge dir holding the input file.
    :param request_id: The ``request_id`` from the ``control_request`` event.
    :param allowed: Whether the tool call is permitted.
    :raises RuntimeError: If the input file can't be written.
    """
    _append_command(
        bridge_dir,
        {"type": "confirmation_response", "request_id": request_id, "allowed": allowed},
    )


# ---------------------------------------------------------------------------
# tmux target (display pane) — used only for interrupt / hard-stop.
# ---------------------------------------------------------------------------


def write_tmux_target(
    bridge_dir: Path,
    *,
    socket_path: Path,
    tmux_target: str,
    pid: int | None = None,
) -> None:
    """Advertise the tmux socket + target for the running qwen terminal."""
    _ensure_dir(bridge_dir)
    payload: dict[str, Any] = {
        "socket_path": str(socket_path),
        "tmux_target": tmux_target,
        "updated_at": time.time(),
    }
    if pid is not None:
        payload["pid"] = pid
    tmp = bridge_dir / (_TMUX_FILE + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, bridge_dir / _TMUX_FILE)


def read_tmux_info(bridge_dir: Path) -> dict[str, str] | None:
    """Return ``{socket_path, tmux_target}`` from ``tmux.json``, or ``None``."""
    try:
        raw = (bridge_dir / _TMUX_FILE).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    socket_path = data.get("socket_path")
    tmux_target = data.get("tmux_target")
    if (
        isinstance(socket_path, str)
        and socket_path
        and isinstance(tmux_target, str)
        and tmux_target
    ):
        return {"socket_path": socket_path, "tmux_target": tmux_target}
    return None


def _wait_for_tmux_info(bridge_dir: Path, *, timeout_s: float) -> dict[str, str]:
    """Block until ``tmux.json`` is advertised, or raise on timeout."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        info = read_tmux_info(bridge_dir)
        if info is not None:
            return info
        time.sleep(_POLL_INTERVAL_S)
    raise RuntimeError(f"qwen-native tmux target was not advertised within {timeout_s:.0f}s")


def _run_tmux(socket_path: str, *args: str) -> None:
    """Invoke ``tmux -S <socket> <args...>`` and raise on failure."""
    try:
        proc = subprocess.run(
            ["tmux", "-S", socket_path, *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=_TMUX_SEND_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"tmux command timed out after {_TMUX_SEND_TIMEOUT_S}s") from exc
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "<no output>"
        raise RuntimeError(f"tmux command failed (rc={proc.returncode}): {detail}")


def inject_interrupt(bridge_dir: Path, *, timeout_s: float = _TMUX_READY_TIMEOUT_S) -> None:
    """Cancel the in-flight qwen turn by sending ``Escape`` to the pane.

    qwen's input-file watcher accepts only ``submit`` / ``confirmation_response``,
    so the web UI's Stop button drives interrupt through the display pane — the
    analog of :func:`submit_user_message` for cancellation. The harness
    ``run_turn`` returns right after appending the submit line, so the runner's
    in-process cancel floor can't reach the turn.

    :raises RuntimeError: If the tmux target is not advertised or send-keys fails.
    """
    info = _wait_for_tmux_info(bridge_dir, timeout_s=timeout_s)
    # No ``-l``: tmux must interpret ``Escape`` as a key name.
    _run_tmux(info["socket_path"], "send-keys", "-t", info["tmux_target"], "Escape")


def kill_session(bridge_dir: Path, *, timeout_s: float = _TMUX_READY_TIMEOUT_S) -> None:
    """Hard-stop the qwen session by killing its tmux session.

    Terminates ``qwen`` and the pane outright — the analog of the user manually
    exiting the attached TUI, for the web UI's "Stop session" affordance.

    :raises RuntimeError: If the tmux target is not advertised or kill-session fails.
    """
    info = _wait_for_tmux_info(bridge_dir, timeout_s=timeout_s)
    _run_tmux(info["socket_path"], "kill-session", "-t", info["tmux_target"])
