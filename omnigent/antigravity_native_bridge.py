"""Bridge state for native Antigravity TUI sessions."""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

_logger = logging.getLogger(__name__)

ANTIGRAVITY_NATIVE_BRIDGE_ID_LABEL_KEY = "omnigent.antigravity_native.bridge_id"
ANTIGRAVITY_NATIVE_BRIDGE_DIR_ENV_VAR = "HARNESS_ANTIGRAVITY_NATIVE_BRIDGE_DIR"
ANTIGRAVITY_NATIVE_REQUEST_SESSION_ID_ENV_VAR = "HARNESS_ANTIGRAVITY_NATIVE_REQUEST_SESSION_ID"

_STATE_FILE = "state.json"
# Advertises the runner-owned tmux pane (socket + target) hosting agy, so the
# executor can deliver a FIRST web turn into the idle agy TUI via send-keys (see
# the tmux/TUI section at the end of this module). Written by the runner after
# the agy terminal launches; read by the executor's first-turn bootstrap.
_TMUX_FILE = "tmux.json"
_BRIDGE_ROOT = Path.home() / ".omnigent" / "antigravity-native"

# Prefix of the launcher-minted placeholder conversation id (see
# ``antigravity_native._mint_agy_conversation_id``). agy mints its own real
# UUID, so the placeholder only seeds bridge state until the forwarder discovers
# and overwrites it; consumers (e.g. the executor) must treat a placeholder id as
# "not ready yet" rather than resolving an RPC against it.
AGY_PLACEHOLDER_CONVERSATION_PREFIX = "agy_conv_"


def is_placeholder_conversation_id(conversation_id: str) -> bool:
    """Return whether *conversation_id* is the launcher placeholder, not agy's real id.

    :param conversation_id: A bridge-state conversation id.
    :returns: ``True`` when it is an ``agy_conv_*`` placeholder (the forwarder has
        not yet discovered agy's real UUID); ``False`` for a real agy id.
    """
    return conversation_id.startswith(AGY_PLACEHOLDER_CONVERSATION_PREFIX)


# Canonical root of agy's per-user app-data tree (``~/.gemini/antigravity-cli``).
# Single-sourced here and imported by the forwarder (transcript / brain
# discovery) so a change to agy's data-dir layout is a one-line edit. (The
# onboarding/auth layer keeps its own ``~/.gemini`` OAuth root in ``gemini_auth``
# — a broader, distinct concern that must not depend on this harness module.)
AGY_APP_DATA_DIR = Path.home() / ".gemini" / "antigravity-cli"

# agy persists onboarding completion in this HOME-global cache file. On a first
# run where it is absent, agy launches an interactive TUI onboarding wizard
# (login method, then color-scheme / telemetry steps) that has no pre-emptive
# flag and no PermissionRequest-style hook. On a host-spawned (web-driven) or
# otherwise headless launch there is no TTY to answer it, so agy hangs and the
# web UI shows nothing. Seeding ``onboardingComplete`` here suppresses the
# wizard, mirroring how ``claude_native_bridge.ensure_claude_workspace_trusted``
# pre-accepts Claude's ``hasCompletedOnboarding`` gate. The agy OAuth token is a
# separate per-host secret (seeded outside this code path); this file carries no
# credential — only the three onboarding-state booleans.
_AGY_ONBOARDING_MARKER = AGY_APP_DATA_DIR / "cache" / "onboarding.json"
# The exact keys agy itself writes on a completed consumer (subscription)
# onboarding — captured ground-truth from a real onboarded profile. Enterprise
# onboarding is a distinct flow Omnigent does not drive, so it stays ``False``.
_AGY_ONBOARDING_COMPLETE_STATE: dict[str, object] = {
    "consumerOnboardingComplete": True,
    "enterpriseOnboardingComplete": False,
    "onboardingComplete": True,
}


def ensure_agy_onboarding_complete() -> None:
    """Pre-accept agy's first-run onboarding wizard so a headless launch never blocks.

    Idempotently seeds ``onboardingComplete`` (and the sibling consumer/enterprise
    flags) into agy's ``~/.gemini/antigravity-cli/cache/onboarding.json`` so the
    interactive TUI onboarding wizard does not stall a host-spawned or headless
    ``agy`` launch that has no TTY to answer it. Call once before launching agy
    (see :func:`omnigent.runner.app._auto_create_antigravity_terminal` and the CLI
    ``_launch_and_record`` path).

    Any unrecognised keys already in the file are preserved (the three known keys
    are merged over them), and the write is skipped entirely when all three
    already hold their exact boolean values — so a returning user's agy state is
    never churned. Unlike
    ``~/.claude.json`` (which holds the Claude OAuth account block and is treated
    fail-loud), this file is a regenerable, non-secret agy cache: an existing
    file that is unreadable or not a JSON object is treated as absent and
    overwritten with the known-complete state rather than raising.

    :returns: None.
    :raises OSError: If the marker directory cannot be created or the file cannot
        be written (e.g. an unwritable home directory). Surfaced rather than
        swallowed: a missing marker means agy will hang on the wizard, so a write
        failure is a real, launch-blocking fault worth failing loudly on.
    """
    marker = _AGY_ONBOARDING_MARKER
    existing: dict[str, object] = {}
    if marker.is_file():
        try:
            loaded = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # Regenerable non-secret cache: an unreadable (OSError), non-UTF-8
            # (UnicodeDecodeError) or malformed-JSON (json.JSONDecodeError) marker
            # would make agy re-run onboarding anyway, so overwrite from an empty
            # base. ValueError is the common supertype of both decode failures.
            loaded = None
        if isinstance(loaded, dict):
            existing = loaded
    # Type-strict identity (``is``, not ``==``) so a stored numeric ``1``/``0``
    # (which ``==`` True/False in Python) is NOT accepted as the boolean state but
    # is normalised on write — matching this module's ``read_bridge_state``, which
    # likewise rejects bool/int conflation.
    if all(existing.get(key) is value for key, value in _AGY_ONBOARDING_COMPLETE_STATE.items()):
        return
    merged: dict[str, object] = {**existing, **_AGY_ONBOARDING_COMPLETE_STATE}
    marker.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f"{marker.name}.", dir=str(marker.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(merged, handle, sort_keys=True)
            handle.write("\n")
            # fsync before the atomic replace so a crash/power-loss cannot leave a
            # present-but-empty marker that would send agy back into the wizard
            # (matches ``claude_native_bridge._atomic_write_user_json``).
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, marker)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    _logger.info("Seeded agy onboarding-complete marker at %s", marker)


def bridge_root() -> Path:
    """
    Return the configured Antigravity-native bridge root.

    Tests may monkeypatch :data:`_BRIDGE_ROOT` to isolate bridge files.

    :returns: Absolute root for Antigravity-native bridge directories, e.g.
        ``Path("~/.omnigent/antigravity-native")``.
    """
    return _BRIDGE_ROOT


@dataclass(frozen=True)
class AntigravityNativeBridgeState:
    """
    Runtime state shared by the native Antigravity wrapper and harness.

    :param session_id: Omnigent conversation id, e.g.
        ``"conv_abc123"``.
    :param conversation_id: agy's real conversation id, e.g.
        ``"68caaeac-2eaf-4e2c-9b95-721b022f4903"``. Discovered by the forwarder
        via by-pid ownership of this session's agy process (agy mints its own
        UUID and ignores any ``ANTIGRAVITY_CONVERSATION_ID`` the launcher sets).
        The launcher seeds this with its minted ``agy_conv_*`` placeholder, which
        the forwarder overwrites once it discovers the real id.
    :param active_turn_id: Current agy turn id, if one is running,
        e.g. ``"turn_abc123"``.

    .. note::
        There is intentionally no durable read cursor. The retired transcript
        forwarder persisted a ``forwarded_steps`` set (+ a ``forwarded_step_index``
        mirror) so a tail (re)start did not re-mirror the whole JSONL. The RPC read
        driver that superseded it (:mod:`omnigent.antigravity_native_reader`) keeps
        an *in-memory* seen-set only and is recreated per session by the runner, so
        no on-disk cursor is needed. Legacy ``forwarded_step_index`` /
        ``forwarded_steps`` keys in an on-disk ``state.json`` are tolerated and
        ignored on read.

    .. note::
        There is intentionally no ``agy_pid`` field. agy's pid is not stable to
        capture at launch — the CLI terminal uses ``tmux_start_on_attach=True``
        (agy does not exist until the human TTY attaches), and even the
        runner-owned web terminal (``tmux_start_on_attach=False``, agy started
        immediately) re-execs/supervises, so a captured pid can go stale. The
        executor instead discovers agy's connect-RPC port at injection time by
        enumerating agy processes and validating each against
        :attr:`conversation_id` via ``GetConversationMetadata`` (see
        :func:`omnigent.antigravity_native_rpc.resolve_language_server_port`).
        A legacy ``agy_pid`` key in an on-disk ``state.json`` is tolerated and
        ignored on read.
    """

    session_id: str
    conversation_id: str
    active_turn_id: str | None = None


def bridge_dir_for_bridge_id(bridge_id: str) -> Path:
    """
    Return the bridge directory for a native Antigravity bridge id.

    :param bridge_id: Opaque bridge id, e.g. ``"bridge_abc123"``.
    :returns: Absolute bridge directory under
        ``~/.omnigent/antigravity-native``.
    """
    digest = hashlib.sha256(bridge_id.encode("utf-8")).hexdigest()[:32]
    return _BRIDGE_ROOT / digest


def build_antigravity_native_spawn_env(
    conversation_id: str,
    *,
    bridge_id: str | None = None,
) -> dict[str, str]:
    """
    Build spawn env for the ``antigravity-native`` harness process.

    :param conversation_id: Omnigent conversation id, e.g.
        ``"conv_abc123"``.
    :param bridge_id: Opaque bridge id from
        :data:`ANTIGRAVITY_NATIVE_BRIDGE_ID_LABEL_KEY`, e.g.
        ``"bridge_abc123"``. ``None`` uses *conversation_id*.
    :returns: Environment variables needed by the Antigravity-native
        harness executor.
    """
    resolved_bridge_id = bridge_id or conversation_id
    return {
        ANTIGRAVITY_NATIVE_BRIDGE_DIR_ENV_VAR: str(bridge_dir_for_bridge_id(resolved_bridge_id)),
        ANTIGRAVITY_NATIVE_REQUEST_SESSION_ID_ENV_VAR: conversation_id,
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


def write_bridge_state(bridge_dir: Path, state: AntigravityNativeBridgeState) -> None:
    """
    Persist shared native Antigravity state atomically.

    :param bridge_dir: Native Antigravity bridge directory.
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
                    "conversation_id": state.conversation_id,
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
    Remove stale native Antigravity runtime state for a bridge directory.

    New agy launches reuse the same bridge directory for a conversation id,
    but the old ``state.json`` still names the *previous* agy run's discovered
    conversation id. Clear it before the new launch so the forwarder
    rediscovers the new run's real conversation id instead of binding to the
    stale one. The advertised tmux pane (``tmux.json``) is cleared for the same
    reason: a new launch opens a new pane (and, after a host restart, a new
    socket), so the executor must not bootstrap the first turn against the prior
    run's pane. The new launch re-advertises its pane via
    :func:`write_tmux_target`.

    :param bridge_dir: Native Antigravity bridge directory.
    :returns: None.
    """
    for runtime_file in (_STATE_FILE, _TMUX_FILE):
        with contextlib.suppress(FileNotFoundError):
            (bridge_dir / runtime_file).unlink()


def read_bridge_state(bridge_dir: Path) -> AntigravityNativeBridgeState | None:
    """
    Read shared native Antigravity bridge state.

    Legacy ``agy_pid`` / ``forwarded_step_index`` / ``forwarded_steps`` keys
    written by an older build are tolerated and ignored: ``agy_pid`` because agy
    does not exist at launch (the executor always discovers the connect-RPC port
    at injection time), and the two cursor keys because the durable read cursor
    was retired with the transcript forwarder (the RPC reader keeps an in-memory
    seen-set only).

    :param bridge_dir: Native Antigravity bridge directory.
    :returns: Parsed state, or ``None`` when no state exists or the file
        is missing, corrupt, or has invalid field types.
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
    conversation_id = raw.get("conversation_id")
    active_turn_id = raw.get("active_turn_id")
    # Validate required string fields are non-empty strings.
    if not isinstance(session_id, str) or not session_id:
        return None
    if not isinstance(conversation_id, str) or not conversation_id:
        return None
    parsed_active_turn_id = (
        active_turn_id if isinstance(active_turn_id, str) and active_turn_id else None
    )
    return AntigravityNativeBridgeState(
        session_id=session_id,
        conversation_id=conversation_id,
        active_turn_id=parsed_active_turn_id,
    )


def update_conversation_id(
    bridge_dir: Path,
    conversation_id: str,
    active_turn_id: str | None = None,
) -> None:
    """
    Update the agy conversation id in bridge state.

    Used when a native agy action creates a fresh conversation while the
    Omnigent session stays the same (e.g. the runner cold-start replacing the
    ``agy_conv_*`` placeholder with agy's real cascade id).

    :param bridge_dir: Native Antigravity bridge directory.
    :param conversation_id: New agy conversation id, e.g.
        ``"agy_conv_abc123"``.
    :param active_turn_id: Active turn id for the new conversation, e.g.
        ``"turn_abc123"``, or ``None`` when no turn is running yet.
    :returns: None.
    """
    state = read_bridge_state(bridge_dir)
    if state is None:
        return
    write_bridge_state(
        bridge_dir,
        AntigravityNativeBridgeState(
            session_id=state.session_id,
            conversation_id=conversation_id,
            active_turn_id=active_turn_id,
        ),
    )


# ── Web-turn TUI delivery (tmux send-keys) ───────────────────────────────────
#
# The executor types EVERY web/mobile turn into the agy TUI over the runner-owned
# tmux pane — never agy's connect-RPC ``SendAgentMessage``, which agy records as a
# ``SYSTEM_MESSAGE`` the transcript forwarder would not mirror (so the user's
# message would never be committed). Typing creates a real ``USER_INPUT`` step
# that the forwarder mirrors in order (validated against agy 1.0.10: a
# paste-buffer + Enter is recorded as ``USER_INPUT`` whether agy is idle or
# mid-turn, and on a fresh session it also creates the conversation + brain dir
# the forwarder then discovers). This mirrors the cursor/claude native send-keys
# delivery; no shared tmux helper exists, so the small primitives are duplicated
# per the established per-harness convention.

# tmux probe/command timeout. Short: these are local IPC calls to the runner's
# own tmux server.
_TMUX_SEND_TIMEOUT_S = 5.0
# Per-readiness-gate wait (tmux.json advertised, then the agy input box mounted).
_TMUX_READY_TIMEOUT_S = 30.0
# Poll cadence for the readiness / paste-commit / submit-verify loops.
_TMUX_POLL_INTERVAL_S = 0.2
# How long to wait for the pasted draft to render in the input box before Enter.
_PASTE_COMMIT_TIMEOUT_S = 5.0
# Settle pause after the draft renders, before the submit Enter.
_PASTE_SETTLE_S = 0.2
# How long one submit Enter is given to start a turn before it is re-sent.
_SUBMIT_VERIFY_TIMEOUT_S = 5.0
# Re-send budget for a submit Enter that the TUI folded into the paste burst.
_MAX_SUBMIT_ATTEMPTS = 3
# Named tmux buffer used to stream the paste (avoids the ~16KB send-keys argv cap).
_PASTE_BUFFER = "omnigent-agy-paste"
# agy TUI footer when idle (input box mounted, ready for a turn).
_AGY_IDLE_MARKER = "? for shortcuts"
# agy TUI footer while a turn is running — the positive "submit took" signal.
_AGY_ACTIVE_MARKER = "esc to cancel"


def write_tmux_target(
    bridge_dir: Path,
    *,
    socket_path: Path,
    tmux_target: str,
    pid: int | None = None,
) -> None:
    """
    Advertise the tmux socket + target for the running agy terminal.

    The runner calls this after launching the agy terminal so the executor can
    shell out to ``tmux send-keys`` against the same private socket to bootstrap
    the first web turn (see :func:`inject_user_message_via_tui`). Written
    atomically next to ``state.json``.

    :param bridge_dir: Native Antigravity bridge directory.
    :param socket_path: Absolute path to the terminal's private tmux socket.
    :param tmux_target: tmux pane target string, e.g. ``"main"``.
    :param pid: Optional agy/pane pid, recorded for diagnostics only.
    :returns: None.
    """
    bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "socket_path": str(socket_path),
        "tmux_target": tmux_target,
        "updated_at": time.time(),
    }
    if pid is not None:
        payload["pid"] = pid
    path = bridge_dir / _TMUX_FILE
    fd, tmp_name = tempfile.mkstemp(prefix=f"{_TMUX_FILE}.", dir=str(bridge_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def read_tmux_info(bridge_dir: Path) -> dict[str, str] | None:
    """
    Return the advertised ``{socket_path, tmux_target}`` for the agy terminal.

    :param bridge_dir: Native Antigravity bridge directory.
    :returns: A dict with non-empty ``socket_path`` and ``tmux_target`` string
        values, or ``None`` when ``tmux.json`` is missing, unreadable, malformed,
        or has invalid field types.
    """
    try:
        raw = (bridge_dir / _TMUX_FILE).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
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
    """
    Block until the agy terminal's tmux target is advertised, or raise.

    :param bridge_dir: Native Antigravity bridge directory.
    :param timeout_s: Maximum seconds to wait for ``tmux.json``.
    :returns: The advertised ``{socket_path, tmux_target}``.
    :raises RuntimeError: When the target is not advertised within *timeout_s*.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        info = read_tmux_info(bridge_dir)
        if info is not None:
            return info
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"antigravity-native tmux target was not advertised within {timeout_s:.0f}s "
                "(is the agy terminal running on this host?)"
            )
        time.sleep(_TMUX_POLL_INTERVAL_S)


def _run_tmux(socket_path: str, *args: str) -> None:
    """
    Invoke ``tmux -S <socket> <args...>`` and raise on failure.

    :param socket_path: tmux server socket path.
    :param args: tmux subcommand and arguments, e.g.
        ``("send-keys", "-t", "main", "Enter")``.
    :returns: None.
    :raises RuntimeError: On a non-zero exit or a timeout.
    """
    try:
        proc = subprocess.run(
            ["tmux", "-S", socket_path, *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=_TMUX_SEND_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"tmux command timed out after {_TMUX_SEND_TIMEOUT_S:.0f}s") from exc
    except OSError as exc:
        raise RuntimeError(f"tmux could not be executed: {exc}") from exc
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "<no output>"
        raise RuntimeError(f"tmux command failed (rc={proc.returncode}): {detail}")


def _capture_pane(socket_path: str, tmux_target: str) -> str:
    """
    Capture the visible pane contents; ``""`` on any failure (treat as not-ready).

    :param socket_path: tmux server socket path.
    :param tmux_target: tmux pane target.
    :returns: The pane text, or ``""`` when the capture fails.
    """
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


def _session_alive(socket_path: str, tmux_target: str) -> bool:
    """
    Return whether the tmux session/pane still exists (the agy TUI is running).

    :param socket_path: tmux server socket path.
    :param tmux_target: tmux pane target.
    :returns: ``True`` when ``has-session`` reports the target exists.
    """
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


def _paste_payload_bytes(text: str) -> bytes:
    r"""
    Encode text for ``tmux load-buffer``.

    Line breaks become CR (0x0D) so the agy TUI keeps a multi-line message as a
    single input under bracketed paste rather than submitting on each newline;
    tabs are kept; other control bytes are dropped (a stray ESC would close the
    bracketed paste early).

    :param text: Raw message text.
    :returns: The encoded paste payload.
    """
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


def _submit_needle(content: str) -> str:
    """
    A stable single-line substring used to confirm the paste rendered in the pane.

    :param content: Message content.
    :returns: Up to 24 chars of the first line with at least 4 non-space chars,
        or ``""`` when no such line exists (the caller then skips the
        paste-commit poll and submits after the settle pause).
    """
    for line in content.splitlines():
        stripped = line.strip()
        if len(stripped) >= 4:
            return stripped[:24]
    stripped = content.strip()
    return stripped[:24] if len(stripped) >= 4 else ""


def _wait_for_agy_prompt_ready(socket_path: str, tmux_target: str, *, timeout_s: float) -> None:
    """
    Best-effort wait until agy's input box is mounted (its footer is rendered).

    Polls ``capture-pane`` for EITHER footer marker: :data:`_AGY_IDLE_MARKER`
    (ready for a new turn) or :data:`_AGY_ACTIVE_MARKER` (a turn is running). Both
    mean the input box is mounted and can take a paste — agy accepts a mid-turn
    paste and queues it as the next turn — so a mid-turn steer must NOT wait for
    the idle footer that will not appear until the active turn ends (that would
    burn the whole budget). Falls through after *timeout_s* rather than raising:
    the submit step is the real guard, and a changed footer string in a future
    agy build must not hard-block delivery.

    :param socket_path: tmux server socket path.
    :param tmux_target: tmux pane target.
    :param timeout_s: Maximum seconds to wait for a footer to render.
    :returns: None.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        pane = _capture_pane(socket_path, tmux_target)
        if _AGY_IDLE_MARKER in pane or _AGY_ACTIVE_MARKER in pane:
            return
        time.sleep(_TMUX_POLL_INTERVAL_S)


def _submit_and_verify(socket_path: str, tmux_target: str) -> None:
    """
    Press Enter to submit the pasted draft, verifying the submit where possible.

    There are two cases, distinguished by the footer BEFORE the Enter:

    **Idle (a sequential turn — the common case).** agy shows
    :data:`_AGY_IDLE_MARKER`. The agy TUI coalesces a rapid stdin burst into a
    paste, so an Enter that lands while the paste is still being consumed is
    folded in as a newline and the draft sits unsent. After each Enter this polls
    for the footer leaving idle — :data:`_AGY_ACTIVE_MARKER` appears (accepted on
    first sighting) OR :data:`_AGY_IDLE_MARKER` is gone for TWO consecutive polls
    (robust to a future agy renaming/truncating the running footer, while a single
    transient redraw frame cannot be misread as started). If neither holds within
    :data:`_SUBMIT_VERIFY_TIMEOUT_S` the Enter is re-sent (the draft is still in
    the box; an Enter on an emptied box is a no-op).

    **Mid-turn (a steer).** agy already shows :data:`_AGY_ACTIVE_MARKER`, so the
    idle→running transition this function watches for is unavailable and a second
    Enter could queue a spurious empty turn. agy queues a mid-turn paste as the
    next ``USER_INPUT`` (verified against agy 1.0.10), and the caller's
    paste-commit poll already confirmed the draft rendered in the input box, so a
    single best-effort Enter is sent and the function returns WITHOUT a footer
    confirmation. The forwarder is the system-of-record for whether the steer
    actually registered (it mirrors the resulting ``USER_INPUT`` step); this path
    deliberately does not re-send or hard-fail on the unverifiable mid-turn case.

    :param socket_path: tmux server socket path.
    :param tmux_target: tmux pane target.
    :returns: None.
    :raises RuntimeError: When the agy TUI exits mid-submit, or (idle case only)
        the footer never leaves idle after :data:`_MAX_SUBMIT_ATTEMPTS` attempts.
    """
    if not _session_alive(socket_path, tmux_target):
        raise RuntimeError(
            "the agy terminal exited before the message could be submitted; restart the session"
        )
    if _AGY_ACTIVE_MARKER in _capture_pane(socket_path, tmux_target):
        # Mid-turn steer: footer-transition verification is unavailable (see the
        # docstring). Deliver one best-effort Enter; do not re-send.
        _run_tmux(socket_path, "send-keys", "-t", tmux_target, "Enter")
        return
    for _ in range(_MAX_SUBMIT_ATTEMPTS):
        # Re-check liveness each attempt: if the TUI exited between the paste and
        # now, every capture returns "" and the footer never changes — so fail
        # fast with the real cause instead of spinning the full budget and then
        # blaming paste-coalescing.
        if not _session_alive(socket_path, tmux_target):
            raise RuntimeError(
                "the agy terminal exited before the message could be submitted; "
                "restart the session"
            )
        _run_tmux(socket_path, "send-keys", "-t", tmux_target, "Enter")
        deadline = time.monotonic() + _SUBMIT_VERIFY_TIMEOUT_S
        non_idle_polls = 0
        while time.monotonic() < deadline:
            pane = _capture_pane(socket_path, tmux_target)
            if pane and _AGY_ACTIVE_MARKER in pane:
                return
            if pane and _AGY_IDLE_MARKER not in pane:
                non_idle_polls += 1
                if non_idle_polls >= 2:
                    return
            else:
                # Idle footer present, or an empty capture (tmux failure): reset
                # so the two confirmations must be consecutive, not cumulative.
                non_idle_polls = 0
            time.sleep(_TMUX_POLL_INTERVAL_S)
    raise RuntimeError(
        "agy did not start a turn after the message was submitted to its terminal "
        "(the submit Enter may have been folded into the paste)"
    )


def inject_user_message_via_tui(
    bridge_dir: Path,
    *,
    content: str,
    timeout_s: float = _TMUX_READY_TIMEOUT_S,
) -> None:
    """
    Deliver a user message into the agy TUI via a tmux bracketed paste + Enter.

    The executor's delivery path for EVERY web/mobile turn (sequential and
    mid-turn steer): a turn delivered over agy's connect-RPC ``SendAgentMessage``
    is recorded as a ``SYSTEM_MESSAGE`` the forwarder would not mirror, whereas
    typing into the TUI creates a real ``USER_INPUT`` step the forwarder mirrors
    in order. On a fresh session this also creates agy's conversation (agy mints
    its id only after processing a turn), which the forwarder then discovers.

    Steps: wait for the advertised tmux target and the agy input box (idle OR a
    running turn — agy accepts a mid-turn paste), clear any leftover draft (Home +
    kill-to-end), stream the content through a named tmux buffer
    (``load-buffer``/``paste-buffer -p`` so interior newlines stay data and a
    large message is not capped by the send-keys argv limit), wait for the draft
    to render, then submit (footer-verified when idle; best-effort mid-turn — see
    :func:`_submit_and_verify`).

    :param bridge_dir: Native Antigravity bridge directory holding ``tmux.json``.
    :param content: User text from the web UI. Must be non-empty.
    :param timeout_s: Total readiness budget, shared across the two gates
        (``tmux.json`` advertised, then the input box mounted) so the worst case
        is one ``timeout_s``, not two stacked.
    :returns: None.
    :raises RuntimeError: When the tmux target is never advertised, the agy TUI
        has exited, a tmux command fails, or the submit never starts a turn.
    """
    if not content:
        raise RuntimeError("antigravity-native TUI injection requires non-empty content")
    # One shared deadline across both readiness gates: the prompt-ready gate gets
    # only the budget the tmux-target gate did not consume, capping total
    # readiness latency at timeout_s (the gates were previously each given a full
    # timeout_s, so a stuck launch cost 2x before delivery even began).
    deadline = time.monotonic() + timeout_s
    info = _wait_for_tmux_info(bridge_dir, timeout_s=timeout_s)
    socket_path = info["socket_path"]
    tmux_target = info["tmux_target"]
    # Fast-fail if the TUI already exited: otherwise the readiness gate polls a
    # dead pane for the full timeout and the web message is silently lost. A clear
    # error lets the executor surface an ExecutorError so the UI can say "restart".
    if not _session_alive(socket_path, tmux_target):
        raise RuntimeError(
            "the agy terminal is no longer running (the TUI exited); restart the session"
        )
    _wait_for_agy_prompt_ready(
        socket_path, tmux_target, timeout_s=max(0.0, deadline - time.monotonic())
    )
    # Clear any leftover draft before typing: Home (C-a) + kill-to-end (C-k).
    _run_tmux(socket_path, "send-keys", "-t", tmux_target, "C-a")
    _run_tmux(socket_path, "send-keys", "-t", tmux_target, "C-k")
    # ``delete=False`` + name captured BEFORE the write, so a write failure still
    # leaves a path the finally can unlink (no leaked temp file in the bridge dir).
    paste_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=bridge_dir, prefix="paste_", suffix=".bin", delete=False
        ) as paste_file:
            paste_path = paste_file.name
            # Trailing newline absorbs any trailing backslash so it can't escape Enter.
            paste_file.write(_paste_payload_bytes(content + "\n"))
        _run_tmux(socket_path, "load-buffer", "-b", _PASTE_BUFFER, paste_path)
        _run_tmux(
            socket_path,
            "paste-buffer",
            "-p",  # bracketed-paste markers — the TUI keeps newlines as data
            "-d",  # drop the buffer after pasting (no stale copies server-side)
            "-b",
            _PASTE_BUFFER,
            "-t",
            tmux_target,
        )
    finally:
        if paste_path is not None:
            with contextlib.suppress(OSError):
                os.unlink(paste_path)
    # Wait until the paste is visibly committed to the input box before Enter, so
    # the submit is not folded into the paste burst (see _submit_and_verify).
    needle = _submit_needle(content)
    if needle:
        commit_deadline = time.monotonic() + _PASTE_COMMIT_TIMEOUT_S
        while time.monotonic() < commit_deadline:
            if needle in _capture_pane(socket_path, tmux_target):
                break
            time.sleep(_TMUX_POLL_INTERVAL_S)
    time.sleep(_PASTE_SETTLE_S)
    _submit_and_verify(socket_path, tmux_target)
