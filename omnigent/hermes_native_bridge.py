"""Filesystem bridge + tmux injection for the hermes-native terminal harness.

The runner launches the ``hermes`` TUI in a private tmux pane and records that
pane's socket + target here via :func:`write_tmux_target`. The harness executor
then delivers Omnigent web-UI messages into the *same* pane via
:func:`inject_user_message` (tmux bracketed paste + a single Enter) — the Hermes
analog of the goose-native tmux bridge. This is what wires the web-UI chat box to
the running Hermes TUI (and, since the web UI embeds that pane, the message shows
in both surfaces).

When Omnigent policies are configured the runner writes a per-session
``HERMES_HOME`` with a ``pre_tool_call`` hook (via :func:`write_policy_hook_config`)
that evaluates tool calls against the Omnigent policy engine — the same hook used
by the headless ``hermes`` harness (:mod:`omnigent.inner.hermes_executor`). The
``HERMES_HOME`` env var in :func:`build_hermes_native_spawn_env` points the TUI at
this per-session dir so the hook fires alongside Hermes' own approval prompt.

The native TUI's own tool-approval prompt is surfaced to the web UI as a synced
approval card by the runner-side mirror
(:mod:`omnigent.hermes_native_permissions`), which reads the pane via
:func:`capture_hermes_pane` and answers it via :func:`send_hermes_pane_keys`.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

#: Env var carrying the bridge dir into the harness executor process.
BRIDGE_DIR_ENV_VAR = "HARNESS_HERMES_NATIVE_BRIDGE_DIR"

_BRIDGE_ROOT = Path(os.environ.get("TMPDIR", "/tmp")) / f"omnigent-{os.getuid()}" / "hermes-native"
_TMUX_FILE = "tmux.json"
_TMUX_READY_TIMEOUT_S = 30.0
_TMUX_SEND_TIMEOUT_S = 10.0
_POLL_INTERVAL_S = 0.2
_PASTE_SETTLE_S = 0.3
_PASTE_BUFFER = "omnigent-hermes-paste"
# How long to wait for the pasted text to become visible in the pane before
# sending Enter — submitting before the TUI commits the paste folds the Enter
# into the paste as a newline and the message sits unsent.
_PASTE_COMMIT_TIMEOUT_S = 5.0
# Hermes' prompt_toolkit TUI emits no fixed ready-prompt sentinel; readiness is
# detected by the pane settling (no byte changes across consecutive captures).
# This many stable polls in a row marks the input box ready.
_SETTLE_STABLE_POLLS = 3


def bridge_dir_for_session_id(session_id: str) -> Path:
    """Return the per-session bridge dir, e.g. ``/tmp/omnigent-<uid>/hermes-native/<hash>``."""
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
    return _BRIDGE_ROOT / digest


def bridge_root() -> Path:
    """Return the configured hermes-native bridge root."""
    return _BRIDGE_ROOT


def _ensure_dir(path: Path) -> None:
    """Create *path* (and parents) with owner-only permissions."""
    path.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o700)


def build_hermes_native_spawn_env(session_id: str) -> dict[str, str]:
    """Build the ``HARNESS_HERMES_NATIVE_*`` env the harness executor reads.

    Publishes the per-session bridge dir so the
    :class:`~omnigent.inner.hermes_native_executor.HermesNativeExecutor` can find
    the tmux target advertised by the runner. If a per-session ``HERMES_HOME``
    was written by :func:`write_policy_hook_config`, the env includes
    ``HERMES_HOME`` so the TUI picks up the policy hook.

    :param session_id: The Omnigent session id (keys the bridge dir).
    :returns: Env-var overrides for the harness executor spawn.
    """
    bridge_dir = bridge_dir_for_session_id(session_id)
    _ensure_dir(bridge_dir)
    env: dict[str, str] = {BRIDGE_DIR_ENV_VAR: str(bridge_dir)}
    hermes_home = read_hermes_home(bridge_dir)
    if hermes_home is not None:
        env["HERMES_HOME"] = str(hermes_home)
    return env


# Keys from the user's ``~/.hermes/config.yaml`` that the per-session
# HERMES_HOME needs in order to authenticate with the inference provider.
_USER_CONFIG_KEYS = frozenset(
    {
        "model",
        "providers",
        "fallback_providers",
        "credential_pool_strategies",
    }
)

_HERMES_HOME_SUBDIR = "hermes_home"


def _load_user_hermes_config() -> dict:
    """Load inference-relevant keys from the user's ``~/.hermes/config.yaml``."""
    user_config = Path.home() / ".hermes" / "config.yaml"
    if not user_config.is_file():
        return {}
    try:
        import yaml

        full = yaml.safe_load(user_config.read_text()) or {}
        return {k: v for k, v in full.items() if k in _USER_CONFIG_KEYS}
    except Exception:  # noqa: BLE001
        _logger.debug("Failed to load user Hermes config at %s", user_config, exc_info=True)
        return {}


_MCP_BRIDGE_CONFIG_FILE = "bridge.json"


def write_policy_hook_config(
    bridge_dir: Path,
    server_url: str,
    session_id: str,
) -> Path:
    """Write per-session ``HERMES_HOME`` with Omnigent policy hook and MCP server.

    Creates a ``config.yaml`` registering:

    1. A ``pre_tool_call`` shell hook that evaluates tool calls against the
       Omnigent policy engine (same hook the headless ``hermes`` harness uses).
    2. An ``mcp_servers.omnigent`` entry that launches the Omnigent MCP stdio
       server (``serve-mcp``), exposing Omnigent builtin tools
       (``sys_session_*``, ``sys_agent_*``, ``load_skill``, ``web_fetch``, etc.)
       to the Hermes model.

    Also copies the user's auth/env files so the TUI can still authenticate
    with its inference provider. Mirrors
    :func:`omnigent.inner.hermes_executor._populate_hermes_home`.

    :param bridge_dir: Per-session bridge dir (parent of the HERMES_HOME).
    :param server_url: Omnigent server base URL.
    :param session_id: Omnigent session / conversation ID.
    :returns: The HERMES_HOME path.
    """
    hermes_home = bridge_dir / _HERMES_HOME_SUBDIR
    hermes_home.mkdir(parents=True, exist_ok=True)

    hook_script_path = str(Path(__file__).resolve().parent / "inner" / "hermes_policy_hook.py")

    # Wrapper shell script: sets env vars and execs the Python hook.
    wrapper = hermes_home / "omnigent-policy-hook.sh"
    wrapper.write_text(
        f"#!/bin/sh\n"
        f"export _OMNIGENT_SERVER_URL='{server_url}'\n"
        f"export _OMNIGENT_SESSION_ID='{session_id}'\n"
        f"exec '{sys.executable}' '{hook_script_path}'\n"
    )
    wrapper.chmod(0o755)

    # Write bridge.json with an auth token for serve-mcp (idempotent).
    _write_mcp_bridge_config(bridge_dir)

    # Merge user config so model/provider/auth settings carry over.
    user_cfg = _load_user_hermes_config()
    config: dict = {**user_cfg}

    config["hooks_auto_accept"] = True
    config["hooks"] = {
        **config.get("hooks", {}),
        "pre_tool_call": [
            {
                "command": str(wrapper),
                "timeout": 86400,
            },
        ],
    }

    # Register the Omnigent MCP stdio server so Hermes can call
    # Omnigent builtin tools (sys_session_*, sys_agent_*, load_skill, etc.).
    config["mcp_servers"] = {
        **config.get("mcp_servers", {}),
        "omnigent": {
            "command": sys.executable,
            "args": [
                "-m",
                "omnigent.claude_native_bridge",
                "serve-mcp",
                "--bridge-dir",
                str(bridge_dir),
            ],
        },
    }

    config_path = hermes_home / "config.yaml"
    config_path.write_text(json.dumps(config, indent=2) + "\n")

    # Copy user's .env (API keys).
    user_env = Path.home() / ".hermes" / ".env"
    if user_env.is_file():
        shutil.copy2(user_env, hermes_home / ".env")

    # Copy user's auth.json (provider credentials).
    user_auth = Path.home() / ".hermes" / "auth.json"
    if user_auth.is_file():
        shutil.copy2(user_auth, hermes_home / "auth.json")

    # Pre-populate the allowlist so Hermes doesn't prompt for hook consent.
    allowlist_path = hermes_home / "shell-hooks-allowlist.json"
    allowlist_data = {
        "approvals": [
            {"event": "pre_tool_call", "command": str(wrapper)},
        ],
    }
    allowlist_path.write_text(json.dumps(allowlist_data, indent=2) + "\n")

    return hermes_home


def _write_mcp_bridge_config(bridge_dir: Path) -> None:
    """Write ``bridge.json`` with an auth token for ``serve-mcp``.

    Idempotent: skips if a config already exists (avoids overwriting a token
    that the relay HTTP server was started with). Mirrors
    :func:`omnigent.codex_native_bridge.write_mcp_bridge_config`.
    """
    config_path = bridge_dir / _MCP_BRIDGE_CONFIG_FILE
    if config_path.exists():
        return
    bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = {"token": secrets.token_urlsafe(32)}
    fd, tmp_name = tempfile.mkstemp(prefix=f"{_MCP_BRIDGE_CONFIG_FILE}.", dir=str(bridge_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_name, config_path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def inject_compress_command(bridge_dir: Path, *, timeout_s: float = 5.0) -> None:
    """Type ``/compress`` into the Hermes TUI pane.

    Hermes' ``/compress`` slash command compacts the conversation context,
    analogous to Claude Code's ``/compact``. This clears any draft the user
    is mid-typing, pastes ``/compress`` literally, and submits with Enter.

    :param bridge_dir: Per-session bridge dir holding ``tmux.json``.
    :param timeout_s: How long to wait for ``tmux.json`` to appear.
    :raises RuntimeError: If the tmux target is not advertised or send-keys fails.
    """
    info = _wait_for_tmux_info(bridge_dir, timeout_s=timeout_s)
    socket_path = info["socket_path"]
    target = info["tmux_target"]
    # Clear any draft the user is mid-typing.
    _run_tmux(socket_path, "send-keys", "-t", target, "C-u")
    # Paste ``/compress`` literally.
    _run_tmux(socket_path, "send-keys", "-l", "-t", target, "/compress")
    # Submit.
    _run_tmux(socket_path, "send-keys", "-t", target, "Enter")


def read_hermes_home(bridge_dir: Path) -> Path | None:
    """Return the per-session HERMES_HOME if it was previously written.

    :param bridge_dir: Per-session bridge dir.
    :returns: The HERMES_HOME path, or ``None`` if it doesn't exist.
    """
    hermes_home = bridge_dir / _HERMES_HOME_SUBDIR
    if hermes_home.is_dir():
        return hermes_home
    return None


def write_tmux_target(
    bridge_dir: Path,
    *,
    socket_path: Path,
    tmux_target: str,
    pid: int | None = None,
) -> None:
    """Advertise the tmux socket + target for the running Hermes terminal."""
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
    raise RuntimeError(f"hermes-native tmux target was not advertised within {timeout_s:.0f}s")


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
    """Best-effort wait until the Hermes input box is ready to receive a paste.

    Hermes emits no fixed idle marker, so readiness is detected by the pane
    settling: the captured contents stop changing for :data:`_SETTLE_STABLE_POLLS`
    consecutive polls (no spinner churn, no streaming output). Falls through after
    the timeout (mid-turn steering may never fully settle) rather than raising.
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
    """Deliver a web-UI user message into the Hermes TUI via a tmux bracketed paste.

    Clears any leftover draft, pastes *content* (multi-line safe via
    ``load-buffer``/``paste-buffer -p`` so interior newlines stay data, not
    submits), settles, then submits with a *single* Enter. Hermes' prompt_toolkit
    input submits on Enter, so exactly one Enter is sent — a second would submit
    an empty turn.

    :param bridge_dir: The hermes-native bridge dir holding ``tmux.json``.
    :param content: User text (non-empty).
    :param timeout_s: Per-readiness-gate timeout.
    :raises RuntimeError: If the tmux target is never advertised or a tmux
        command fails.
    """
    if not content:
        raise RuntimeError("hermes-native injection requires non-empty content")
    info = _wait_for_tmux_info(bridge_dir, timeout_s=timeout_s)
    socket_path = info["socket_path"]
    tmux_target = info["tmux_target"]
    # Fast-fail if the TUI already exited: otherwise _settle_pane polls a dead
    # pane for the full timeout and the web message is silently lost.
    if not _session_alive(socket_path, tmux_target):
        raise RuntimeError(
            "hermes terminal is no longer running (the TUI exited); restart the session"
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
    """Cancel the in-flight Hermes turn by sending ``C-c`` to the pane.

    Hermes uses Ctrl+C to interrupt a running turn (double-press within 2s
    forces exit). The harness ``run_turn`` returns right after the paste, so
    the runner's in-process cancel floor can't reach the turn — this is the
    analog of :func:`inject_user_message` for the web UI's Stop button.

    :raises RuntimeError: If the tmux target is not advertised or send-keys fails.
    """
    info = _wait_for_tmux_info(bridge_dir, timeout_s=timeout_s)
    _run_tmux(info["socket_path"], "send-keys", "-t", info["tmux_target"], "C-c")


def kill_session(bridge_dir: Path, *, timeout_s: float = _TMUX_READY_TIMEOUT_S) -> None:
    """Hard-stop the Hermes session by killing its tmux session.

    Terminates ``hermes`` and the pane outright — the analog of the user manually
    exiting the attached TUI, for the web UI's "Stop session" affordance.

    :raises RuntimeError: If the tmux target is not advertised or kill-session fails.
    """
    info = _wait_for_tmux_info(bridge_dir, timeout_s=timeout_s)
    _run_tmux(info["socket_path"], "kill-session", "-t", info["tmux_target"])


def capture_hermes_pane(bridge_dir: Path) -> str | None:
    """Return the visible Hermes pane text, or ``None`` if the TUI is not running.

    Used by the runner-side approval mirror
    (:mod:`omnigent.hermes_native_permissions`) to detect Hermes' in-terminal
    "DANGEROUS COMMAND" approval prompt. ``None`` (no advertised tmux target, or a
    dead pane) is distinct from ``""`` (a live but empty capture).

    :param bridge_dir: The hermes-native bridge dir holding ``tmux.json``.
    :returns: The captured pane text, or ``None`` when no live pane exists.
    """
    info = read_tmux_info(bridge_dir)
    if info is None:
        return None
    socket_path, tmux_target = info["socket_path"], info["tmux_target"]
    if not _session_alive(socket_path, tmux_target):
        return None
    return _capture_pane(socket_path, tmux_target)


def send_hermes_pane_keys(bridge_dir: Path, *keys: str) -> None:
    """Send one or more keys to the Hermes pane (tmux ``send-keys``).

    Used by the approval mirror to answer Hermes' native prompt from a web
    verdict, e.g. ``"o"`` to approve once or ``"d"`` to deny. Each key is a tmux
    key name/argument (not bracketed-paste data), so multi-byte keys like
    ``"Enter"`` are interpreted, not typed literally.

    :param bridge_dir: The hermes-native bridge dir holding ``tmux.json``.
    :param keys: tmux key arguments, e.g. ``"o"`` or ``"Enter"``.
    :raises RuntimeError: If the tmux target is not advertised or send-keys fails.
    """
    info = read_tmux_info(bridge_dir)
    if info is None:
        raise RuntimeError("hermes-native tmux target not advertised")
    _run_tmux(info["socket_path"], "send-keys", "-t", info["tmux_target"], *keys)
