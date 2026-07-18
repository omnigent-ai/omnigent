"""Filesystem bridge + tmux injection for the coco-native terminal harness.

The runner launches the Snowflake CoCo (Cortex Code) TUI — the ``cortex``
binary — in a private tmux pane and records that pane's socket + target here
via :func:`write_tmux_target`. The harness executor then delivers Omnigent
web-UI messages into the *same* pane via :func:`inject_user_message` (tmux
bracketed paste + a single Enter) — the CoCo analog of the goose-native tmux
bridge.

Unlike goose, CoCo supports lifecycle hooks, but (as of v1.1.1) loads them
only from ``$SNOWFLAKE_HOME/cortex/hooks.json`` — there is no project-level or
``--config`` hook source. So the bridge builds a **per-session
``SNOWFLAKE_HOME``** (:func:`write_coco_home`): every entry of the user's real
``~/.snowflake`` tree is symlinked in (auth ``connections.toml``, settings,
MCP config, skills, and — importantly — the ``conversations`` store, so
sessions stay resumable from the user's own ``cortex resume``), and only
``cortex/hooks.json`` is replaced with a merged copy that adds the Omnigent
lifecycle hook (:mod:`omnigent.coco_native_hook`). The hook appends
``SessionStart`` / ``UserPromptSubmit`` / ``Stop`` events to
``<bridge_dir>/hook_events.jsonl``, which the forwarder tails.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from omnigent._platform import stable_user_id

#: Env var carrying the bridge dir into the harness executor process.
BRIDGE_DIR_ENV_VAR = "HARNESS_COCO_NATIVE_BRIDGE_DIR"

_BRIDGE_ROOT = Path(tempfile.gettempdir()) / f"omnigent-{stable_user_id()}" / "coco-native"
_TMUX_FILE = "tmux.json"
_SNOWFLAKE_HOME_SUBDIR = "snowflake_home"
_TMUX_READY_TIMEOUT_S = 30.0
_TMUX_SEND_TIMEOUT_S = 10.0
_POLL_INTERVAL_S = 0.2
_PASTE_SETTLE_S = 0.3
_PASTE_BUFFER = "omnigent-coco-paste"
# How long to wait for the pasted text to become visible in the pane before
# sending Enter — submitting before the TUI commits the paste folds the Enter
# into the paste as a newline and the message sits unsent.
_PASTE_COMMIT_TIMEOUT_S = 5.0
# CoCo emits no fixed ready-prompt sentinel; readiness is detected by the pane
# settling (no byte changes across consecutive captures), like goose-native.
_SETTLE_STABLE_POLLS = 3

#: Hook events the bridge registers. SessionStart/UserPromptSubmit carry the
#: CoCo session id (TUI-mode discovery); Stop marks the turn end at which CoCo
#: has flushed the transcript (verified on v1.1.1).
_HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "Stop")


def bridge_dir_for_session_id(session_id: str) -> Path:
    """Return the per-session bridge dir, e.g. ``/tmp/omnigent-<uid>/coco-native/<hash>``."""
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
    return _BRIDGE_ROOT / digest


def bridge_root() -> Path:
    """Return the configured coco-native bridge root."""
    return _BRIDGE_ROOT


def _ensure_dir(path: Path) -> None:
    """Create *path* (and parents) with owner-only permissions."""
    path.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o700)


def default_snowflake_home() -> Path:
    """Return the user's real Snowflake home (``SNOWFLAKE_HOME`` or ``~/.snowflake``)."""
    override = os.environ.get("SNOWFLAKE_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".snowflake"


def coco_conversations_dir(snowflake_home: Path | None = None) -> Path:
    """Return CoCo's conversations store under *snowflake_home*.

    The per-session home symlinks this directory from the user's real home, so
    session recordings resolve to the same files either way.
    """
    home = snowflake_home or default_snowflake_home()
    return home / "cortex" / "conversations"


def coco_session_recording_exists(coco_session_id: str) -> bool:
    """Return whether a CoCo transcript exists for *coco_session_id*.

    Guards cold resume: launch ``cortex -r <id>`` only when the recording is
    present, else fall back to a fresh session (CoCo's resume errors on an
    unknown id).
    """
    if not coco_session_id:
        return False
    history = coco_conversations_dir() / f"{coco_session_id}.history.jsonl"
    return history.is_file()


def _user_hooks_config(user_cortex_dir: Path) -> dict[str, Any]:
    """Load the user's own ``hooks.json`` map, tolerating both accepted shapes.

    CoCo's loader accepts ``{"hooks": {...}}`` or the bare event map; normalize
    to the bare map so merging is uniform. Malformed content degrades to no
    user hooks rather than failing the launch.
    """
    path = user_cortex_dir / "hooks.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if isinstance(raw, dict):
        hooks = raw.get("hooks", raw)
        if isinstance(hooks, dict):
            return hooks
    return {}


def _omnigent_hook_command(bridge_dir: Path) -> str:
    """Shell command string CoCo runs for each registered lifecycle event."""
    hook_script = Path(__file__).resolve().parent / "coco_native_hook.py"
    return (
        f"{shlex.quote(sys.executable)} {shlex.quote(str(hook_script))} "
        f"--bridge-dir {shlex.quote(str(bridge_dir))}"
    )


def _symlink_children(source_dir: Path, target_dir: Path, *, skip: frozenset[str]) -> None:
    """Symlink every child of *source_dir* into *target_dir* except *skip* names."""
    if not source_dir.is_dir():
        return
    for entry in source_dir.iterdir():
        if entry.name in skip:
            continue
        link = target_dir / entry.name
        with contextlib.suppress(OSError):
            # Heal a broken symlink (its target was deleted since the last
            # launch): left in place it would hard-fail CoCo's own directory
            # creation for that path.
            if link.is_symlink() and not link.exists():
                link.unlink()
            if not link.exists() and not link.is_symlink():
                link.symlink_to(entry)


def write_coco_home(bridge_dir: Path) -> Path:
    """Build the per-session ``SNOWFLAKE_HOME`` with the Omnigent lifecycle hook.

    Mirrors the user's real Snowflake home by symlink — auth
    (``connections.toml``), all of ``cortex/`` (settings, MCP, skills, agents,
    the ``conversations`` store, ...) — and writes only ``cortex/hooks.json``
    fresh, merging the user's own hook entries with the Omnigent event relay
    so neither is lost. Idempotent: re-running refreshes the hook config and
    fills in links that appeared in the user's home since the last launch.

    :param bridge_dir: Per-session bridge dir (also the hook's append target).
    :returns: The per-session ``SNOWFLAKE_HOME`` path.
    """
    _ensure_dir(bridge_dir)
    home = bridge_dir / _SNOWFLAKE_HOME_SUBDIR
    _ensure_dir(home)
    cortex_dir = home / "cortex"
    _ensure_dir(cortex_dir)

    user_home = default_snowflake_home()
    user_cortex = user_home / "cortex"
    # Pre-create the user-side skeleton on a first run (no ~/.snowflake yet):
    # the conversations store MUST be a symlink into the user's real home, or
    # CoCo creates a real dir inside the throwaway bridge dir and transcripts
    # (and cold resume) die with the session.
    with contextlib.suppress(OSError):
        (user_cortex / "conversations").mkdir(parents=True, exist_ok=True)
    # Top level: connections.toml / config.toml etc. — everything but the
    # cortex dir itself, which needs the hooks.json carve-out below.
    _symlink_children(user_home, home, skip=frozenset({"cortex"}))
    _symlink_children(user_cortex, cortex_dir, skip=frozenset({"hooks.json"}))

    hooks: dict[str, Any] = {
        event: list(groups) if isinstance(groups, list) else []
        for event, groups in _user_hooks_config(user_cortex).items()
    }
    command = _omnigent_hook_command(bridge_dir)
    for event in _HOOK_EVENTS:
        groups = hooks.setdefault(event, [])
        groups.append({"hooks": [{"type": "command", "command": command}]})
    hooks_path = cortex_dir / "hooks.json"
    hooks_path.write_text(json.dumps({"hooks": hooks}, indent=2) + "\n", encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(hooks_path, 0o600)
    return home


def read_coco_home(bridge_dir: Path) -> Path | None:
    """Return the per-session ``SNOWFLAKE_HOME`` if it was written, else ``None``."""
    home = bridge_dir / _SNOWFLAKE_HOME_SUBDIR
    return home if home.is_dir() else None


def build_coco_native_spawn_env(session_id: str) -> dict[str, str]:
    """Build the ``HARNESS_COCO_NATIVE_*`` env the harness executor reads.

    Publishes the per-session bridge dir so the
    :class:`~omnigent.inner.coco_native_executor.CocoNativeExecutor` can find
    the tmux target advertised by the runner.

    :param session_id: The Omnigent session id (keys the bridge dir).
    :returns: Env-var overrides for the harness executor spawn.
    """
    bridge_dir = bridge_dir_for_session_id(session_id)
    _ensure_dir(bridge_dir)
    return {BRIDGE_DIR_ENV_VAR: str(bridge_dir)}


def write_tmux_target(
    bridge_dir: Path,
    *,
    socket_path: Path,
    tmux_target: str,
    pid: int | None = None,
) -> None:
    """Advertise the tmux socket + target for the running CoCo terminal."""
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
    raise RuntimeError(f"coco-native tmux target was not advertised within {timeout_s:.0f}s")


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


def _capture_pane(socket_path: str, tmux_target: str) -> str:
    """Capture the visible pane contents; ``""`` on any failure (treat as not-ready)."""
    try:
        proc = subprocess.run(
            ["tmux", "-S", socket_path, "capture-pane", "-p", "-t", tmux_target],
            check=False,
            capture_output=True,
            text=True,
            timeout=_TMUX_SEND_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, OSError):
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def _paste_payload_bytes(text: str) -> bytes:
    r"""Encode text for ``tmux load-buffer``: line breaks → CR, tabs kept, other
    control bytes dropped (a stray ESC would close the bracketed-paste early)."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    body = bytearray()
    for ch in normalized:
        if ch == "\n":
            body.append(0x0D)
            continue
        if ch == "\t":
            body.append(0x09)
            continue
        if ord(ch) < 0x20:
            continue
        body.extend(ch.encode("utf-8"))
    return bytes(body)


def _session_alive(socket_path: str, tmux_target: str) -> bool:
    """Return whether the tmux session/pane still exists (the TUI is running)."""
    try:
        proc = subprocess.run(
            ["tmux", "-S", socket_path, "has-session", "-t", tmux_target],
            check=False,
            capture_output=True,
            text=True,
            timeout=_TMUX_SEND_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return proc.returncode == 0


def _submit_needle(content: str) -> str:
    """A stable single-line substring used to confirm the paste rendered in the pane.

    Anchored to the LAST qualifying line, not the first: the tail of a freshly
    pasted message is far less likely to already be visible in the pane (a prior
    turn's echo, scrollback) than its opening line, so matching it is a tighter
    signal that *this* paste committed before we send Enter.
    """
    for line in reversed(content.splitlines()):
        stripped = line.strip()
        if len(stripped) >= 4:
            return stripped[:24]
    stripped = content.strip()
    return stripped[:24] if len(stripped) >= 4 else ""


def _settle_pane(socket_path: str, tmux_target: str, *, timeout_s: float) -> None:
    """Best-effort wait until the CoCo input box is ready to receive a paste.

    CoCo emits no fixed idle marker, so readiness is detected by the pane
    settling: the captured contents stop changing for :data:`_SETTLE_STABLE_POLLS`
    consecutive polls (no spinner churn, no streaming output). Falls through after
    the timeout (mid-turn steering may never fully settle) rather than raising —
    CoCo queues messages typed mid-turn, so a paste into a busy pane still lands.
    """
    deadline = time.monotonic() + timeout_s
    previous = _capture_pane(socket_path, tmux_target)
    stable = 0
    while time.monotonic() < deadline:
        time.sleep(_POLL_INTERVAL_S)
        current = _capture_pane(socket_path, tmux_target)
        if current and current == previous:
            stable += 1
            if stable >= _SETTLE_STABLE_POLLS:
                return
        else:
            stable = 0
        previous = current


def inject_user_message(
    bridge_dir: Path,
    *,
    content: str,
    timeout_s: float = _TMUX_READY_TIMEOUT_S,
) -> None:
    """Deliver a web-UI user message into the CoCo TUI via a tmux bracketed paste.

    Clears any leftover draft, pastes *content* (multi-line safe via
    ``load-buffer``/``paste-buffer -p`` so interior newlines stay data, not
    submits), settles, then submits with a *single* Enter. CoCo submits on
    Enter and inserts a newline on Ctrl+J, so exactly one Enter is sent — a
    second would submit twice.

    :param bridge_dir: The coco-native bridge dir holding ``tmux.json``.
    :param content: User text (non-empty).
    :param timeout_s: Per-readiness-gate timeout.
    :raises RuntimeError: If the tmux target is never advertised or a tmux
        command fails.
    """
    if not content:
        raise RuntimeError("coco-native injection requires non-empty content")
    info = _wait_for_tmux_info(bridge_dir, timeout_s=timeout_s)
    socket_path = info["socket_path"]
    tmux_target = info["tmux_target"]
    # Fast-fail if the TUI already exited: otherwise _settle_pane polls a dead
    # pane for the full timeout and the web message is silently lost.
    if not _session_alive(socket_path, tmux_target):
        raise RuntimeError(
            "CoCo terminal is no longer running (the TUI exited); restart the session"
        )
    _settle_pane(socket_path, tmux_target, timeout_s=timeout_s)
    # Clear any leftover draft: Home (C-a) + kill-to-end (C-k).
    _run_tmux(socket_path, "send-keys", "-t", tmux_target, "C-a")
    _run_tmux(socket_path, "send-keys", "-t", tmux_target, "C-k")
    with tempfile.NamedTemporaryFile(
        dir=bridge_dir, prefix="paste_", suffix=".bin", delete=False
    ) as paste_file:
        # Trailing newline absorbs any trailing backslash so it can't escape Enter.
        paste_file.write(_paste_payload_bytes(content + "\n"))
        paste_path = paste_file.name
    try:
        _run_tmux(socket_path, "load-buffer", "-b", _PASTE_BUFFER, paste_path)
        _run_tmux(
            socket_path,
            "paste-buffer",
            "-p",  # bracketed-paste markers — the TUI keeps newlines as data
            "-d",  # drop the buffer after pasting
            "-b",
            _PASTE_BUFFER,
            "-t",
            tmux_target,
        )
    finally:
        with contextlib.suppress(OSError):
            os.unlink(paste_path)
    # Wait until the paste is visibly committed before Enter. Submitting mid-paste
    # folds the Enter in as a newline (rapid stdin bursts coalesce), leaving the
    # message unsent. Poll for the text, then submit; blind-submit if no needle.
    needle = _submit_needle(content)
    if needle:
        deadline = time.monotonic() + _PASTE_COMMIT_TIMEOUT_S
        while time.monotonic() < deadline:
            if needle in _capture_pane(socket_path, tmux_target):
                break
            time.sleep(_POLL_INTERVAL_S)
    time.sleep(_PASTE_SETTLE_S)
    _run_tmux(socket_path, "send-keys", "-t", tmux_target, "Enter")


def inject_interrupt(bridge_dir: Path, *, timeout_s: float = _TMUX_READY_TIMEOUT_S) -> None:
    """Cancel the in-flight CoCo turn by sending ``Escape`` to the pane.

    CoCo's own footer advertises "esc to interrupt" while a turn runs, and a
    live check confirms Escape cancels tool execution cleanly ("Tool execution
    was interrupted by user"). Ctrl+C is deliberately NOT used: CoCo binds it
    to cancel-or-exit (double-tap exits), so two rapid Stop clicks would kill
    the TUI outright.

    :raises RuntimeError: If the tmux target is not advertised or send-keys fails.
    """
    info = _wait_for_tmux_info(bridge_dir, timeout_s=timeout_s)
    # No ``-l``: tmux must interpret ``Escape`` as a key name.
    _run_tmux(info["socket_path"], "send-keys", "-t", info["tmux_target"], "Escape")


def kill_session(bridge_dir: Path, *, timeout_s: float = _TMUX_READY_TIMEOUT_S) -> None:
    """Hard-stop the CoCo session by killing its tmux session.

    Terminates ``cortex`` and the pane outright — the analog of the user
    manually exiting the attached TUI, for the web UI's "Stop session"
    affordance.

    :raises RuntimeError: If the tmux target is not advertised or kill-session fails.
    """
    info = _wait_for_tmux_info(bridge_dir, timeout_s=timeout_s)
    _run_tmux(info["socket_path"], "kill-session", "-t", info["tmux_target"])


def capture_coco_pane(bridge_dir: Path) -> str | None:
    """Return the visible CoCo pane text, or ``None`` if the TUI is not running.

    ``None`` (no advertised tmux target, or a dead pane) is distinct from
    ``""`` (a live but empty capture).
    """
    info = read_tmux_info(bridge_dir)
    if info is None:
        return None
    socket_path, tmux_target = info["socket_path"], info["tmux_target"]
    if not _session_alive(socket_path, tmux_target):
        return None
    return _capture_pane(socket_path, tmux_target)
