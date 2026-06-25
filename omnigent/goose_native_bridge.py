"""Filesystem bridge + tmux injection for the goose-native terminal harness.

The runner launches the ``goose session`` TUI in a private tmux pane and records
that pane's socket + target here via :func:`write_tmux_target`. The harness
executor then delivers Omnigent web-UI messages into the *same* pane via
:func:`inject_user_message` (tmux bracketed paste + a single Enter) — the goose
analog of the cursor-native tmux bridge. This is what wires the web-UI chat box
to the running Goose TUI (and, since the web UI embeds that pane, the message
shows in both surfaces).

Unlike cursor-native, this bridge does NOT write any vendor MCP config: Goose's
MCP "extensions" live in ``~/.config/goose/config.yaml`` (owned by the user via
``goose configure``), and Omnigent does not mutate user config in v1.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

#: Env var carrying the bridge dir into the harness executor process.
BRIDGE_DIR_ENV_VAR = "HARNESS_GOOSE_NATIVE_BRIDGE_DIR"

_BRIDGE_ROOT = Path(os.environ.get("TMPDIR", "/tmp")) / f"omnigent-{os.getuid()}" / "goose-native"
_TMUX_FILE = "tmux.json"
_TMUX_READY_TIMEOUT_S = 30.0
_TMUX_SEND_TIMEOUT_S = 10.0
_POLL_INTERVAL_S = 0.2
_PASTE_SETTLE_S = 0.3
_PASTE_BUFFER = "omnigent-goose-paste"
# How long to wait for the pasted text to become visible in the pane before
# sending Enter — submitting before the TUI commits the paste folds the Enter
# into the paste as a newline and the message sits unsent.
_PASTE_COMMIT_TIMEOUT_S = 5.0
# Goose emits no fixed ready-prompt sentinel; readiness is detected by the pane
# settling (no byte changes across consecutive captures). This many stable polls
# in a row marks the input box ready. See KTD3 / R2 in the plan — refine with a
# concrete idle marker once observed against a live `goose session`.
_SETTLE_STABLE_POLLS = 3


def bridge_dir_for_session_id(session_id: str) -> Path:
    """Return the per-session bridge dir, e.g. ``/tmp/omnigent-<uid>/goose-native/<hash>``."""
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
    return _BRIDGE_ROOT / digest


def bridge_root() -> Path:
    """Return the configured goose-native bridge root."""
    return _BRIDGE_ROOT


def _ensure_dir(path: Path) -> None:
    """Create *path* (and parents) with owner-only permissions."""
    path.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o700)


def build_goose_native_spawn_env(
    session_id: str,
    *,
    provider: str | None = None,
    model: str | None = None,
) -> dict[str, str]:
    """Build the ``HARNESS_GOOSE_NATIVE_*`` + ``GOOSE_*`` env the terminal reads.

    Sets the bridge dir (for the harness executor), forces the ANSI theme so the
    pane is cheaper to scrape, and — since ``goose session`` exposes no
    ``--provider``/``--model`` flags — pins the provider/model via env when the
    caller resolved them. A pasted/keyring credential the user already configured
    via ``goose configure`` is left untouched.

    :param session_id: The Omnigent session id (keys the bridge dir).
    :param provider: Optional ``GOOSE_PROVIDER`` override.
    :param model: Optional ``GOOSE_MODEL`` override.
    :returns: Env-var overrides for the terminal spawn.
    """
    bridge_dir = bridge_dir_for_session_id(session_id)
    _ensure_dir(bridge_dir)
    env: dict[str, str] = {
        BRIDGE_DIR_ENV_VAR: str(bridge_dir),
        # 8-color ANSI output is far easier to strip than the default truecolor
        # bat-highlighted rendering when capturing the pane.
        "GOOSE_CLI_THEME": "ansi",
    }
    if provider:
        env["GOOSE_PROVIDER"] = provider
    if model:
        env["GOOSE_MODEL"] = model
    return env


def write_tmux_target(
    bridge_dir: Path,
    *,
    socket_path: Path,
    tmux_target: str,
    pid: int | None = None,
) -> None:
    """Advertise the tmux socket + target for the running Goose terminal."""
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
    raise RuntimeError(f"goose-native tmux target was not advertised within {timeout_s:.0f}s")


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
    """Best-effort wait until the Goose input box is ready to receive a paste.

    Goose emits no fixed idle marker, so readiness is detected by the pane
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
    """Deliver a web-UI user message into the Goose TUI via a tmux bracketed paste.

    Clears any leftover draft, pastes *content* (multi-line safe via
    ``load-buffer``/``paste-buffer -p`` so interior newlines stay data, not
    submits), settles, then submits with a *single* Enter. Goose submits on
    Enter and inserts a newline on Ctrl+J, so exactly one Enter is sent — a
    second would submit twice.

    :param bridge_dir: The goose-native bridge dir holding ``tmux.json``.
    :param content: User text (non-empty).
    :param timeout_s: Per-readiness-gate timeout.
    :raises RuntimeError: If the tmux target is never advertised or a tmux
        command fails.
    """
    if not content:
        raise RuntimeError("goose-native injection requires non-empty content")
    info = _wait_for_tmux_info(bridge_dir, timeout_s=timeout_s)
    socket_path = info["socket_path"]
    tmux_target = info["tmux_target"]
    # Fast-fail if the TUI already exited: otherwise _settle_pane polls a dead
    # pane for the full timeout and the web message is silently lost.
    if not _session_alive(socket_path, tmux_target):
        raise RuntimeError(
            "goose terminal is no longer running (the TUI exited); restart the session"
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
    """Cancel the in-flight Goose turn by sending ``Escape`` to the pane.

    The harness ``run_turn`` returns right after the paste, so the runner's
    in-process cancel floor can't reach the turn — this is the analog of
    :func:`inject_user_message` for the web UI's Stop button.

    :raises RuntimeError: If the tmux target is not advertised or send-keys fails.
    """
    info = _wait_for_tmux_info(bridge_dir, timeout_s=timeout_s)
    # No ``-l``: tmux must interpret ``Escape`` as a key name.
    _run_tmux(info["socket_path"], "send-keys", "-t", info["tmux_target"], "Escape")


def kill_session(bridge_dir: Path, *, timeout_s: float = _TMUX_READY_TIMEOUT_S) -> None:
    """Hard-stop the Goose session by killing its tmux session.

    Terminates ``goose`` and the pane outright — the analog of the user manually
    exiting the attached TUI, for the web UI's "Stop session" affordance.

    :raises RuntimeError: If the tmux target is not advertised or kill-session fails.
    """
    info = _wait_for_tmux_info(bridge_dir, timeout_s=timeout_s)
    _run_tmux(info["socket_path"], "kill-session", "-t", info["tmux_target"])


def capture_goose_pane(bridge_dir: Path) -> str | None:
    """Return the visible Goose pane text, or ``None`` if the TUI is not running.

    Used by the runner-side approval mirror
    (:mod:`omnigent.goose_native_permissions`) to detect Goose's in-terminal
    ``cliclack`` tool-approval prompt. ``None`` (no advertised tmux target, or a
    dead pane) is distinct from ``""`` (a live but empty capture).

    :param bridge_dir: The goose-native bridge dir holding ``tmux.json``.
    :returns: The captured pane text, or ``None`` when no live pane exists.
    """
    info = read_tmux_info(bridge_dir)
    if info is None:
        return None
    socket_path, tmux_target = info["socket_path"], info["tmux_target"]
    if not _session_alive(socket_path, tmux_target):
        return None
    return _capture_pane(socket_path, tmux_target)


def send_goose_pane_keys(bridge_dir: Path, *keys: str) -> None:
    """Send one or more keys to the Goose pane (tmux ``send-keys``).

    Used by the approval mirror to drive Goose's ``cliclack`` select from a web
    verdict, e.g. ``"Enter"`` to choose the highlighted "Allow" or ``"Down"`` to
    move to "Deny". Each key is a tmux key name/argument (not bracketed-paste
    data), so multi-byte keys like ``"Enter"`` / ``"Down"`` are interpreted.

    :param bridge_dir: The goose-native bridge dir holding ``tmux.json``.
    :param keys: tmux key arguments, e.g. ``"Down"`` or ``"Enter"``.
    :raises RuntimeError: If the tmux target is not advertised or send-keys fails.
    """
    info = read_tmux_info(bridge_dir)
    if info is None:
        raise RuntimeError("goose-native tmux target not advertised")
    _run_tmux(info["socket_path"], "send-keys", "-t", info["tmux_target"], *keys)
