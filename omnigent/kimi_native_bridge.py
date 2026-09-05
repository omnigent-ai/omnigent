"""Filesystem bridge + tmux injection for the kimi-native terminal harness.

The runner launches the ``kimi`` TUI in a private tmux pane and records
that pane's socket + target here via :func:`write_tmux_target`. The harness
executor then delivers Omnigent web-UI messages into the *same* pane via
:func:`inject_user_message` (tmux bracketed paste + Enter) — the kimi analog
of claude-native's tmux send-keys bridge. This is what wires the web-UI chat box
to the running Kimi TUI (and, since the web UI embeds that pane, the message
shows in both surfaces).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from omnigent._platform import stable_user_id
from omnigent.json_types import JsonObject as _JsonObject

#: Env var carrying the bridge dir into the harness executor process.
BRIDGE_DIR_ENV_VAR = "HARNESS_KIMI_NATIVE_BRIDGE_DIR"

_BRIDGE_ROOT = Path(tempfile.gettempdir()) / f"omnigent-{stable_user_id()}" / "kimi-native"
_TMUX_FILE = "tmux.json"
# Omnigent routing details the kimi hook subprocess reads to reach the server.
# Mirrors claude-native's ``permission_hook.json`` (server URL + auth headers +
# the active Omnigent session). Written by the runner at terminal-create time;
# read by :mod:`omnigent.kimi_native_hook` (PreToolUse deny-gate + the
# PermissionRequest read-only surface).
_HOOK_CONFIG_FILE = "hook_config.json"
_TMUX_READY_TIMEOUT_S = 30.0
# Per-command tmux budget. 10s matches every other native bridge: a tmux
# server starved by parallel worker boots can stall past 5s while healthy.
_TMUX_SEND_TIMEOUT_S = 10.0
_KIMI_READY_TIMEOUT_S = 120.0
_POLL_INTERVAL_S = 0.15
_PASTE_SETTLE_S = 0.1  # let the TUI commit a paste before the separate submit Enter
_PASTE_BUFFER = "omnigent-kimi-paste"
# How long to wait for the pasted text to become visible in the pane before
# sending Enter — submitting before the TUI commits the paste folds the Enter
# into the paste as a newline and the message sits unsent.
_PASTE_COMMIT_TIMEOUT_S = 5.0
# Kimi renders the editor marker inside a box-drawing input row.
_INPUT_BOX_MARKERS = ("> ", "! ")
_FOOTER_MARKERS = ("context:",)
_SUBMIT_VERIFY_TIMEOUT_S = 10.0
_SUBMIT_RETRY_INTERVAL_S = 1.0
_APPROVAL_SETTLE_TIMEOUT_S = 0.5
_CLEAR_SETTLE_TIMEOUT_S = 2.0
_DRAFT_NEEDLE_MAX_CHARS = 24
_PASTE_PLACEHOLDER_RE = re.compile(r"\[paste #(\d+) (?:(?:\+(\d+) lines?)|(?:(\d+) chars))\]")
_TRUST_HEADER = "Trust this folder?"
# Anchor on the accept-option label, not the modal's body prose: the body copy
# has been reworded between kimi releases while the option row has stayed put.
_TRUST_DESCRIPTION = "Enable project MCP servers"
_PERMISSION_MENU_FOOTER_MARKER = "1/2/3/4 choose"
_INJECTION_CANCEL_FILE = "injection.cancelled"

_logger = logging.getLogger(__name__)
_ACTIVE_INJECTION_EVENTS: dict[Path, set[threading.Event]] = {}
_ACTIVE_INJECTION_EVENTS_LOCK = threading.Lock()


class KimiTuiNotReadyError(RuntimeError):
    """The Kimi TUI did not mount an input box before the readiness deadline."""


class KimiApprovalPromptNotFoundError(RuntimeError):
    """The Kimi permission menu was absent before approval was injected."""


class KimiApprovalPromptAmbiguousError(RuntimeError):
    """The same Kimi permission menu remained visible after its option was typed."""


class KimiApprovalSessionNotFoundError(RuntimeError):
    """The Kimi session was not running when approval was injected."""


class KimiApprovalPendingError(RuntimeError):
    """A Kimi permission menu must be resolved before another message is sent."""


class _PaneRowKind(str, Enum):
    INSIDE_EDITOR = "inside-editor"
    MENU_CHROME = "menu-chrome"
    TRANSCRIPT = "transcript"


@dataclass(frozen=True)
class _KimiPaneState:
    lines: tuple[str, ...]
    row_kinds: tuple[_PaneRowKind, ...]
    editor_bounds: tuple[int, int] | None
    input_line: str | None
    input_content: str | None
    editor_content: str | None
    footer_visible: bool
    menu_visible: bool
    menu_chrome_visible: bool
    menu_start: int | None
    trust_visible: bool
    exit_armed: bool
    turn_streaming: bool

    @property
    def editor_present(self) -> bool:
        return self.input_line is not None

    @property
    def ready(self) -> bool:
        return self.editor_present and self.footer_visible and not self.menu_visible


def bridge_dir_for_session_id(session_id: str) -> Path:
    """Return the per-session bridge dir, e.g. ``/tmp/omnigent-<uid>/kimi-native/<hash>``."""
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
    return _BRIDGE_ROOT / digest


def bridge_root() -> Path:
    """Return the configured Kimi-native bridge root."""
    return _BRIDGE_ROOT


def _ensure_dir(path: Path) -> None:
    """Create *path* (and parents) with owner-only permissions."""
    path.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o700)


def build_kimi_native_spawn_env(session_id: str) -> dict[str, str]:
    """Build the ``HARNESS_KIMI_NATIVE_*`` env the harness executor reads."""
    bridge_dir = bridge_dir_for_session_id(session_id)
    _ensure_dir(bridge_dir)
    return {
        BRIDGE_DIR_ENV_VAR: str(bridge_dir),
    }


def write_hook_config(
    bridge_dir: Path,
    *,
    server_url: str,
    headers: dict[str, str],
    session_id: str,
) -> None:
    """Record the Omnigent routing details the kimi hook subprocess reads.

    The PreToolUse / PermissionRequest hook commands receive only
    ``--bridge-dir`` on their command line (no secrets); they read the
    server URL, auth headers, and active session id from this file. Mirrors
    :func:`omnigent.claude_native_bridge` ``permission_hook.json`` plumbing.

    :param bridge_dir: The kimi-native bridge dir.
    :param server_url: Omnigent server base URL, e.g. ``"http://127.0.0.1:8787"``.
    :param headers: Auth headers to replay on the hook's POSTs (may be empty).
    :param session_id: The Omnigent session the hook events belong to.
    """
    _ensure_dir(bridge_dir)
    payload = {
        "ap_server_url": server_url,
        "ap_auth_headers": dict(headers),
        "session_id": session_id,
        "updated_at": time.time(),
    }
    tmp = bridge_dir / (_HOOK_CONFIG_FILE + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(tmp, 0o600)
    os.replace(tmp, bridge_dir / _HOOK_CONFIG_FILE)


def read_hook_config(bridge_dir: Path) -> _JsonObject:
    """Read Omnigent routing details for the kimi hook subprocess.

    :param bridge_dir: The kimi-native bridge dir.
    :returns: ``{"ap_server_url", "ap_auth_headers", "session_id"}`` (or an
        empty dict when the file is absent or malformed).
    """
    try:
        raw = (bridge_dir / _HOOK_CONFIG_FILE).read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def read_active_session_id(bridge_dir: Path) -> str | None:
    """Return the Omnigent session id recorded for the hook subprocess.

    :param bridge_dir: The kimi-native bridge dir.
    :returns: The session id, or ``None`` when unset / malformed.
    """
    session_id = read_hook_config(bridge_dir).get("session_id")
    return session_id if isinstance(session_id, str) and session_id else None


def write_tmux_target(
    bridge_dir: Path,
    *,
    socket_path: Path,
    tmux_target: str,
    pid: int | None = None,
) -> None:
    """Advertise the tmux socket + target for the running Kimi terminal."""
    _ensure_dir(bridge_dir)
    payload: _JsonObject = {
        "socket_path": str(socket_path),
        "tmux_target": tmux_target,
        "updated_at": time.time(),
    }
    if pid is not None:
        payload["pid"] = pid
    tmp = bridge_dir / (_TMUX_FILE + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, bridge_dir / _TMUX_FILE)
    with contextlib.suppress(OSError):
        (bridge_dir / _INJECTION_CANCEL_FILE).unlink()


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
    raise RuntimeError(f"kimi-native tmux target was not advertised within {timeout_s:.0f}s")


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
    """Capture the visible pane; ``""`` on any failure."""
    try:
        proc = subprocess.run(
            [
                "tmux",
                "-S",
                socket_path,
                "capture-pane",
                "-p",
                "-t",
                tmux_target,
            ],
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
    """A stable single-line substring used to confirm the paste rendered in the pane."""
    first_line = _submit_first_line(content)
    return first_line[:_DRAFT_NEEDLE_MAX_CHARS] if len(first_line) >= 4 else ""


def _submit_first_line(content: str) -> str:
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    for line in normalized.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        for char_index, character in enumerate(stripped):
            if ord(character) < 0x20:
                stripped = stripped[:char_index]
                break
        return stripped
    return ""


def _is_input_box_marker_line(line: str) -> bool:
    stripped = line.strip()
    if not (stripped.startswith("│") and stripped.endswith("│")):
        return False
    body = stripped[1:-1]
    return any(body.startswith(f" {marker}") for marker in _INPUT_BOX_MARKERS)


def _is_box_row(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("│") and stripped.endswith("│")


def _input_box_frame_bounds(pane: str) -> tuple[int, int] | None:
    lines = pane.splitlines()
    frame_start: int | None = None
    match: tuple[int, int] | None = None
    for line_index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("╭") and stripped.endswith("╮"):
            frame_start = line_index
            continue
        if not (stripped.startswith("╰") and stripped.endswith("╯")):
            continue
        if frame_start is None:
            frame_start = line_index
            while frame_start > 0 and _is_box_row(lines[frame_start - 1]):
                frame_start -= 1
        frame = lines[frame_start : line_index + 1]
        if any(_is_input_box_marker_line(frame_line) for frame_line in frame):
            match = (frame_start, line_index)
        frame_start = None
    return match


def _input_content_from_line(line: str | None) -> str | None:
    if line is None:
        return None
    body = line.strip()[1:-1]
    for marker in _INPUT_BOX_MARKERS:
        prefix = f" {marker}"
        if body.startswith(prefix):
            return body[len(prefix) :].strip()
    return None


def _editor_content_from_frame(
    lines: tuple[str, ...], bounds: tuple[int, int] | None
) -> str | None:
    if bounds is None:
        return None
    content: list[str] = []
    for line in lines[bounds[0] : bounds[1] + 1]:
        stripped = line.strip()
        if not _is_box_row(stripped):
            continue
        body = stripped[1:-1]
        for marker in _INPUT_BOX_MARKERS:
            prefix = f" {marker}"
            if body.startswith(prefix):
                content.append(body[len(prefix) :].rstrip())
                break
        else:
            if body.startswith("   "):
                content.append(body[3:].rstrip())
    return "\n".join(content).strip()


def _permission_option_row(line: str) -> bool:
    normalized = line.strip().lstrip("▶").strip()
    options = (
        "1. Approve once",
        "2. Approve for this session",
        "3. Reject",
        "4. Reject with feedback",
    )
    return any(normalized == option or normalized.startswith(f"{option} ") for option in options)


def _menu_rule(line: str) -> bool:
    return bool(line.strip()) and set(line.strip()) <= {"─"}


def _menu_candidate(
    lines: tuple[str, ...], editor_indices: set[int]
) -> tuple[bool, set[int], int | None]:
    option_indices = {
        index
        for index, line in enumerate(lines)
        if index not in editor_indices and _permission_option_row(line)
    }
    header_indices = {
        index
        for index, line in enumerate(lines)
        if index not in editor_indices and line.strip().startswith("▶ Run this command?")
    }
    footer_indices = {
        index
        for index, line in enumerate(lines)
        if index not in editor_indices
        and line.strip().startswith("↑/↓ select")
        and _PERMISSION_MENU_FOOTER_MARKER in line
    }
    for header_index in sorted(header_indices):
        following_options = sorted(index for index in option_indices if index > header_index)
        if len(following_options) < 2:
            continue
        footer_index = min(
            (index for index in footer_indices if index > following_options[-1]),
            default=None,
        )
        if footer_index is None:
            continue
        if header_index == 0 or footer_index + 1 >= len(lines):
            continue
        if not _menu_rule(lines[header_index - 1]) or not _menu_rule(lines[footer_index + 1]):
            continue
        menu_start = header_index
        menu_end = footer_index
        menu_indices = set(range(menu_start, menu_end + 1)) - editor_indices
        return True, menu_indices, menu_start
    return False, set(), None


def _parse_pane(pane: str, *, turn_streaming: bool = False) -> _KimiPaneState:
    lines = tuple(pane.splitlines())
    bounds = _input_box_frame_bounds(pane)
    editor_indices = set(range(bounds[0], bounds[1] + 1)) if bounds is not None else set()
    menu_chrome_visible, menu_indices, menu_start = _menu_candidate(lines, editor_indices)
    menu_visible = menu_chrome_visible and bounds is None
    row_kinds = tuple(
        _PaneRowKind.INSIDE_EDITOR
        if index in editor_indices
        else _PaneRowKind.MENU_CHROME
        if index in menu_indices
        else _PaneRowKind.TRANSCRIPT
        for index in range(len(lines))
    )
    input_line = None
    if bounds is not None:
        input_line = next(
            (
                lines[index]
                for index in range(bounds[0], bounds[1] + 1)
                if _is_input_box_marker_line(lines[index])
            ),
            None,
        )
    outside_lines = [
        line for index, line in enumerate(lines) if row_kinds[index] != _PaneRowKind.INSIDE_EDITOR
    ]
    footer_visible = bool(
        bounds is not None and any(_FOOTER_MARKERS[0] in line for line in lines[bounds[1] + 1 :])
    )
    trust_headers = [
        index
        for index, line in enumerate(lines)
        if row_kinds[index] != _PaneRowKind.INSIDE_EDITOR and line.strip() == _TRUST_HEADER
    ]
    trust_descriptions = [
        index
        for index, line in enumerate(lines)
        if row_kinds[index] != _PaneRowKind.INSIDE_EDITOR and _TRUST_DESCRIPTION in line
    ]
    # The option row sits ~9-10 lines below the header; allow slack for a
    # wrapped body sentence or a long workspace path.
    trust_visible = any(
        description - header <= 14
        and description > header
        and header > 0
        and _menu_rule(lines[header - 1])
        for header in trust_headers
        for description in trust_descriptions
    )
    exit_armed = any("Press Ctrl+C again to exit" in line for line in outside_lines)
    return _KimiPaneState(
        lines=lines,
        row_kinds=row_kinds,
        editor_bounds=bounds,
        input_line=input_line,
        input_content=_input_content_from_line(input_line),
        editor_content=_editor_content_from_frame(lines, bounds),
        footer_visible=footer_visible,
        menu_visible=menu_visible,
        menu_chrome_visible=menu_chrome_visible,
        menu_start=menu_start,
        trust_visible=trust_visible,
        exit_armed=exit_armed,
        turn_streaming=turn_streaming,
    )


def _permission_prompt_visible(pane: str) -> bool:
    return _parse_pane(pane).menu_visible


def approval_prompt_visible(bridge_dir: Path) -> bool:
    info = read_tmux_info(bridge_dir)
    if info is None or not _session_alive(info["socket_path"], info["tmux_target"]):
        return False
    pane = _capture_pane(info["socket_path"], info["tmux_target"])
    return _parse_pane(pane).menu_visible


def _trust_prompt_visible(pane: str) -> bool:
    return _parse_pane(pane).trust_visible


def _input_box_line(pane: str) -> str | None:
    """Return the first framed line carrying a Kimi input marker."""
    return _parse_pane(pane).input_line


def _kimi_tui_ready(pane: str) -> bool:
    """Return whether both the Kimi input row and context footer are visible."""
    return _parse_pane(pane).ready


def _input_box_content(pane: str) -> str | None:
    return _parse_pane(pane).editor_content


def _draft_in_input_box(pane: str, needle: str) -> bool:
    """Return whether the pasted draft is visible after Kimi's input marker."""
    content = _parse_pane(pane).editor_content
    return bool(needle and content is not None and needle in content)


def _normalize_screen_text(value: str) -> str:
    return " ".join(value.split())


def _paste_placeholder_counts(content: str | None) -> dict[str, tuple[str, int]]:
    if not content:
        return {}
    return {
        match.group(0): (
            "lines",
            int(match.group(2)),
        )
        if match.group(2) is not None
        else ("chars", int(match.group(3)))
        for match in _PASTE_PLACEHOLDER_RE.finditer(content)
    }


def _paste_line_count(content: str) -> int:
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.count("\n") + 2


def _paste_char_count(content: str) -> int:
    payload = _paste_payload_bytes(content + "\n").decode("utf-8")
    return len(payload.encode("utf-16-le")) // 2


def _normalized_contains(content: str, expected: str) -> bool:
    """True when *expected* appears in *content*, tolerating screen line-wraps.

    Compares whitespace-collapsed text first, then falls back to a
    whitespace-stripped compare so a needle split across a wrapped pane row
    (which inserts spaces) still matches.
    """
    normalized_content = _normalize_screen_text(content)
    normalized_expected = _normalize_screen_text(expected)
    if normalized_expected in normalized_content:
        return True
    return normalized_expected.replace(" ", "") in normalized_content.replace(" ", "")


def _draft_visible_in_editor(
    state: _KimiPaneState,
    needle: str,
    *,
    expected_content: str | None = None,
    pre_paste_content: str | None = None,
    pre_paste_placeholders: frozenset[str] = frozenset(),
    expected_line_count: int | None = None,
    expected_char_count: int | None = None,
) -> bool:
    content = state.editor_content
    if not content:
        return False
    if pre_paste_content is not None and content == pre_paste_content:
        return False
    placeholders = _paste_placeholder_counts(content)
    if placeholders and content.strip() in placeholders:
        return any(
            token not in pre_paste_placeholders
            and (
                (kind == "lines" and count == expected_line_count)
                or (kind == "chars" and count == expected_char_count)
            )
            for token, (kind, count) in placeholders.items()
        )
    if expected_content is not None:
        expected = _paste_payload_bytes(expected_content).decode("utf-8")
        return _normalized_contains(content, expected)
    if needle:
        return _normalized_contains(content, needle)
    return not needle


def _approval_pending(state: _KimiPaneState) -> bool:
    if state.menu_visible:
        return True
    if not state.menu_chrome_visible or state.editor_bounds is None or state.menu_start is None:
        return False
    return state.menu_start > state.editor_bounds[1]


def _menu_matches_submission(
    state: _KimiPaneState, *, first_line: str, draft_was_visible: bool
) -> bool:
    if state.menu_start is None or not first_line:
        return draft_was_visible and not state.editor_present
    if any(
        state.row_kinds[index] == _PaneRowKind.TRANSCRIPT and first_line in line
        for index, line in enumerate(state.lines[: state.menu_start])
    ):
        return True
    return draft_was_visible and not state.editor_present


def _approval_menu_identity(state: _KimiPaneState) -> str | None:
    if not state.menu_visible or state.menu_start is None:
        return None
    end = next(
        (
            index
            for index in range(state.menu_start, len(state.lines))
            if state.lines[index].strip().startswith("↑/↓ select")
            and _PERMISSION_MENU_FOOTER_MARKER in state.lines[index]
        ),
        None,
    )
    if end is None:
        return None
    identity_lines = []
    for line in state.lines[state.menu_start : end + 1]:
        text = re.sub(r"^[▶●]\s*", "", line.strip())
        if text and set(text) != {"─"}:
            identity_lines.append(text)
    return _normalize_screen_text(" ".join(identity_lines))


class _InjectionCancellation:
    def __init__(self, bridge_dir: Path, event: threading.Event) -> None:
        self.event = event
        self._cancel_file = bridge_dir / _INJECTION_CANCEL_FILE
        self._started_at_ns = time.time_ns()

    def is_set(self) -> bool:
        if self.event.is_set():
            return True
        try:
            return self._cancel_file.stat().st_mtime_ns >= self._started_at_ns
        except OSError:
            return False


def _register_injection(bridge_dir: Path, event: threading.Event) -> None:
    with _ACTIVE_INJECTION_EVENTS_LOCK:
        _ACTIVE_INJECTION_EVENTS.setdefault(bridge_dir, set()).add(event)


def _unregister_injection(bridge_dir: Path, event: threading.Event) -> None:
    with _ACTIVE_INJECTION_EVENTS_LOCK:
        events = _ACTIVE_INJECTION_EVENTS.get(bridge_dir)
        if events is None:
            return
        events.discard(event)
        if not events:
            _ACTIVE_INJECTION_EVENTS.pop(bridge_dir, None)


def _cancel_injections(bridge_dir: Path) -> None:
    with _ACTIVE_INJECTION_EVENTS_LOCK:
        for event in _ACTIVE_INJECTION_EVENTS.get(bridge_dir, ()):
            event.set()
    tmp = bridge_dir / f"{_INJECTION_CANCEL_FILE}.tmp"
    try:
        tmp.write_text("cancelled", encoding="utf-8")
        os.replace(tmp, bridge_dir / _INJECTION_CANCEL_FILE)
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink()


def _raise_if_injection_cancelled(
    cancel_event: threading.Event | _InjectionCancellation | None,
) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise RuntimeError("Kimi native message injection was cancelled")


def _restore_editor_content(
    socket_path: str, tmux_target: str, bridge_dir: Path, content: str
) -> None:
    state = _parse_pane(_capture_pane(socket_path, tmux_target))
    if not state.editor_present or _approval_pending(state):
        _logger.warning("Kimi draft restore skipped; lost draft length=%d", len(content))
        return
    with tempfile.NamedTemporaryFile(
        dir=bridge_dir, prefix="restore_", suffix=".bin", delete=False
    ) as restore_file:
        restore_file.write(_paste_payload_bytes(content + "\n"))
        restore_path = restore_file.name
    try:
        _run_tmux(socket_path, "load-buffer", "-b", _PASTE_BUFFER, restore_path)
        state = _parse_pane(_capture_pane(socket_path, tmux_target))
        if not state.editor_present or _approval_pending(state):
            _logger.warning("Kimi draft restore skipped; lost draft length=%d", len(content))
            return
        # A menu can mount after this capture; the remaining gap is sub-100ms.
        _run_tmux(
            socket_path,
            "paste-buffer",
            "-p",
            "-d",
            "-b",
            _PASTE_BUFFER,
            "-t",
            tmux_target,
        )
    finally:
        with contextlib.suppress(OSError):
            os.unlink(restore_path)


def _settle_pane(
    socket_path: str,
    tmux_target: str,
    *,
    timeout_s: float,
    cancel_event: threading.Event | _InjectionCancellation | None = None,
) -> _KimiPaneState:
    """Wait until the Kimi input box is ready to receive a paste.

    Accepts the first-run trust modal (sends ``Enter`` at most once)
    so the input box can mount, then returns when the input row and footer appear.
    """
    deadline = time.monotonic() + timeout_s
    trust_accepted = False
    while True:
        _raise_if_injection_cancelled(cancel_event)
        state = _parse_pane(_capture_pane(socket_path, tmux_target))
        if _approval_pending(state):
            raise KimiApprovalPendingError(
                "Kimi approval is pending; resolve it in the terminal before sending "
                "another message"
            )
        if state.ready:
            return state
        # One-shot, only when no input marker is up (so a later transcript that
        # merely echoes the phrase can't spray repeated keystrokes into the TUI).
        if not trust_accepted and state.trust_visible:
            _raise_if_injection_cancelled(cancel_event)
            trust_accepted = True
            with contextlib.suppress(RuntimeError):
                _run_tmux(socket_path, "send-keys", "-t", tmux_target, "Enter")
        if time.monotonic() >= deadline:
            break
        time.sleep(_POLL_INTERVAL_S)
    raise KimiTuiNotReadyError(
        f"Kimi TUI input box did not become ready within {timeout_s:.0f}s; "
        "the message was not delivered. Restart the Kimi terminal and retry."
    )


def inject_user_message(
    bridge_dir: Path,
    *,
    content: str,
    timeout_s: float = _KIMI_READY_TIMEOUT_S,
    cancel_event: threading.Event | None = None,
    # Caller-declared streaming can race completion and append to a leftover draft.
    turn_streaming: bool = False,
) -> None:
    """Deliver a web-UI user message into the Kimi TUI via a tmux bracketed paste.

    Clears any leftover draft, pastes *content* (multi-line safe via
    ``load-buffer``/``paste-buffer -p`` so interior newlines stay data, not
    submits), settles, then submits with Enter.

    :param bridge_dir: The kimi-native bridge dir holding ``tmux.json``.
    :param content: User text (non-empty).
    :param timeout_s: Kimi TUI readiness timeout; override for slow boots.
    :param cancel_event: Optional cancellation flag checked before delivery.
    :param turn_streaming: True when Kimi is already streaming and queues input.
    :raises RuntimeError: If the tmux target is never advertised or a tmux
        command fails.
    """
    if not content:
        raise RuntimeError("kimi-native injection requires non-empty content")
    event = cancel_event if isinstance(cancel_event, threading.Event) else threading.Event()
    cancellation = _InjectionCancellation(bridge_dir, event)
    _register_injection(bridge_dir, event)
    try:
        _raise_if_injection_cancelled(cancellation)
        info = _wait_for_tmux_info(bridge_dir, timeout_s=_TMUX_READY_TIMEOUT_S)
        socket_path = info["socket_path"]
        tmux_target = info["tmux_target"]
        _raise_if_injection_cancelled(cancellation)
        # Fast-fail if the TUI already exited: otherwise _settle_pane polls a dead
        # pane for the full timeout and the web message is silently lost. A clear
        # error lets run_turn surface ExecutorError so the UI can say "restart".
        if not _session_alive(socket_path, tmux_target):
            raise RuntimeError(
                "kimi terminal is no longer running (the TUI exited); restart the session"
            )
        _settle_pane(
            socket_path,
            tmux_target,
            timeout_s=timeout_s,
            cancel_event=cancellation,
        )
        state = _parse_pane(_capture_pane(socket_path, tmux_target), turn_streaming=turn_streaming)
        if _approval_pending(state):
            raise KimiApprovalPendingError(
                "Kimi approval is pending; resolve it in the terminal before sending "
                "another message"
            )
        if state.trust_visible:
            raise KimiTuiNotReadyError(
                "Kimi trust prompt appeared before the message could be submitted; "
                "resolve it in the terminal and retry."
            )
        if not state.editor_present:
            raise KimiTuiNotReadyError(
                "Kimi TUI input box disappeared before the message could be submitted; "
                "the message was not delivered"
            )
        if state.editor_content and state.exit_armed:
            raise RuntimeError(
                "Kimi terminal is exit-armed with an unsent draft; press Escape and retry"
            )
        if state.editor_content and not state.turn_streaming:
            _raise_if_injection_cancelled(cancellation)
            state = _parse_pane(
                _capture_pane(socket_path, tmux_target), turn_streaming=turn_streaming
            )
            if _approval_pending(state):
                raise KimiApprovalPendingError(
                    "Kimi approval is pending; resolve it in the terminal before sending "
                    "another message"
                )
            if state.trust_visible:
                raise KimiTuiNotReadyError(
                    "Kimi trust prompt appeared before clearing the draft; "
                    "resolve it in the terminal and retry."
                )
            if state.exit_armed:
                raise RuntimeError("Kimi terminal is exit-armed; press Escape and retry")
            if state.editor_content:
                _raise_if_injection_cancelled(cancellation)
                draft_to_restore = state.editor_content
                _run_tmux(socket_path, "send-keys", "-t", tmux_target, "C-c")
                clear_deadline = time.monotonic() + _CLEAR_SETTLE_TIMEOUT_S
                clear_confirmed = False
                while time.monotonic() < clear_deadline:
                    state = _parse_pane(
                        _capture_pane(socket_path, tmux_target), turn_streaming=turn_streaming
                    )
                    if state.editor_present and state.editor_content == "":
                        clear_confirmed = True
                        break
                    if cancellation.is_set():
                        break
                    if _approval_pending(state):
                        raise KimiApprovalPendingError(
                            "Kimi approval is pending; resolve it in the terminal before sending "
                            "another message"
                        )
                    if state.trust_visible:
                        raise KimiTuiNotReadyError(
                            "Kimi trust prompt appeared while clearing the draft; "
                            "resolve it in the terminal and retry."
                        )
                    time.sleep(_POLL_INTERVAL_S)
                else:
                    raise RuntimeError(
                        "Kimi TUI did not clear the existing draft; the message was not delivered"
                    )
                if cancellation.is_set():
                    if clear_confirmed:
                        _restore_editor_content(
                            socket_path, tmux_target, bridge_dir, draft_to_restore
                        )
                    _raise_if_injection_cancelled(cancellation)
        needle = _submit_needle(content)
        first_line = _submit_first_line(content)
        expected_line_count = _paste_line_count(content)
        expected_char_count = _paste_char_count(content)
        pre_paste_placeholders: frozenset[str] = frozenset()
        pre_paste_content: str | None = None
        with tempfile.NamedTemporaryFile(
            dir=bridge_dir, prefix="paste_", suffix=".bin", delete=False
        ) as paste_file:
            # Trailing newline absorbs any trailing backslash so it can't escape Enter.
            paste_file.write(_paste_payload_bytes(content + "\n"))
            paste_path = paste_file.name
        try:
            _raise_if_injection_cancelled(cancellation)
            _run_tmux(socket_path, "load-buffer", "-b", _PASTE_BUFFER, paste_path)
            _raise_if_injection_cancelled(cancellation)
            state = _parse_pane(
                _capture_pane(socket_path, tmux_target), turn_streaming=turn_streaming
            )
            if _approval_pending(state):
                raise KimiApprovalPendingError(
                    "Kimi approval is pending; resolve it in the terminal before sending "
                    "another message"
                )
            if state.trust_visible or not state.editor_present:
                raise KimiTuiNotReadyError(
                    "Kimi TUI input box disappeared before the message could be submitted; "
                    "the message was not delivered"
                )
            pre_paste_placeholders = frozenset(_paste_placeholder_counts(state.editor_content))
            pre_paste_content = state.editor_content
            _raise_if_injection_cancelled(cancellation)
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
        draft_seen = False
        paste_deadline = time.monotonic() + _PASTE_COMMIT_TIMEOUT_S
        while time.monotonic() < paste_deadline:
            _raise_if_injection_cancelled(cancellation)
            state = _parse_pane(
                _capture_pane(socket_path, tmux_target), turn_streaming=turn_streaming
            )
            if _approval_pending(state):
                raise KimiApprovalPendingError(
                    "Kimi approval is pending; resolve it in the terminal before sending "
                    "another message"
                )
            if state.trust_visible:
                raise KimiTuiNotReadyError(
                    "Kimi trust prompt appeared before the message could be submitted; "
                    "resolve it in the terminal and retry."
                )
            if _draft_visible_in_editor(
                state,
                needle,
                expected_content=content,
                pre_paste_content=pre_paste_content,
                pre_paste_placeholders=pre_paste_placeholders,
                expected_line_count=expected_line_count,
                expected_char_count=expected_char_count,
            ):
                draft_seen = True
                break
            time.sleep(_POLL_INTERVAL_S)
        if not draft_seen:
            raise RuntimeError(
                "Kimi TUI did not show the pasted message in the input box; "
                "the message was not delivered"
            )
        time.sleep(_PASTE_SETTLE_S)
        pre_submit_deadline = time.monotonic() + _SUBMIT_VERIFY_TIMEOUT_S
        while time.monotonic() < pre_submit_deadline:
            _raise_if_injection_cancelled(cancellation)
            state = _parse_pane(
                _capture_pane(socket_path, tmux_target), turn_streaming=turn_streaming
            )
            if _approval_pending(state):
                raise KimiApprovalPendingError(
                    "Kimi approval is pending; resolve it in the terminal before sending "
                    "another message"
                )
            if state.trust_visible:
                raise KimiTuiNotReadyError(
                    "Kimi trust prompt appeared before the message could be submitted; "
                    "resolve it in the terminal and retry."
                )
            if _draft_visible_in_editor(
                state,
                needle,
                expected_content=content,
                pre_paste_content=pre_paste_content,
                pre_paste_placeholders=pre_paste_placeholders,
                expected_line_count=expected_line_count,
                expected_char_count=expected_char_count,
            ):
                _raise_if_injection_cancelled(cancellation)
                state = _parse_pane(
                    _capture_pane(socket_path, tmux_target), turn_streaming=turn_streaming
                )
                if _approval_pending(state):
                    raise KimiApprovalPendingError(
                        "Kimi approval is pending; resolve it in the terminal before sending "
                        "another message"
                    )
                if state.trust_visible:
                    raise KimiTuiNotReadyError(
                        "Kimi trust prompt appeared before the message could be submitted; "
                        "resolve it in the terminal and retry."
                    )
                if (
                    _draft_visible_in_editor(
                        state,
                        needle,
                        expected_content=content,
                        pre_paste_content=pre_paste_content,
                        pre_paste_placeholders=pre_paste_placeholders,
                        expected_line_count=expected_line_count,
                        expected_char_count=expected_char_count,
                    )
                    and not state.exit_armed
                ):
                    _raise_if_injection_cancelled(cancellation)
                    # A menu can mount after this capture; the remaining gap is one tmux call.
                    _run_tmux(socket_path, "send-keys", "-t", tmux_target, "Enter")
                    break
            time.sleep(_POLL_INTERVAL_S)
        else:
            raise RuntimeError(
                "Kimi TUI input box disappeared before the message could be submitted; "
                "the message was not delivered"
            )
        post_submit_deadline = time.monotonic() + _SUBMIT_VERIFY_TIMEOUT_S
        last_enter = time.monotonic()
        while time.monotonic() < post_submit_deadline:
            time.sleep(_POLL_INTERVAL_S)
            _raise_if_injection_cancelled(cancellation)
            state = _parse_pane(
                _capture_pane(socket_path, tmux_target), turn_streaming=turn_streaming
            )
            if state.menu_visible:
                if _menu_matches_submission(
                    state, first_line=first_line, draft_was_visible=draft_seen
                ):
                    return
                raise KimiApprovalPendingError(
                    "Kimi approval is pending before this message was submitted; "
                    "resolve it in the terminal before sending another message"
                )
            if state.trust_visible:
                raise KimiTuiNotReadyError(
                    "Kimi trust prompt appeared while submitting the message; "
                    "resolve it in the terminal and retry."
                )
            if _approval_pending(state):
                raise KimiApprovalPendingError(
                    "Kimi approval is pending before this message was submitted; "
                    "resolve it in the terminal before sending another message"
                )
            if state.editor_content is not None and draft_seen and not state.editor_content:
                return
            if state.exit_armed and state.editor_content:
                raise RuntimeError("Kimi terminal is exit-armed; press Escape and retry")
            if time.monotonic() - last_enter < _SUBMIT_RETRY_INTERVAL_S:
                continue
            _raise_if_injection_cancelled(cancellation)
            state = _parse_pane(
                _capture_pane(socket_path, tmux_target), turn_streaming=turn_streaming
            )
            if state.menu_visible:
                if _menu_matches_submission(
                    state, first_line=first_line, draft_was_visible=draft_seen
                ):
                    return
                raise KimiApprovalPendingError(
                    "Kimi approval is pending before this message was submitted; "
                    "resolve it in the terminal before sending another message"
                )
            if state.trust_visible:
                raise KimiTuiNotReadyError(
                    "Kimi trust prompt appeared while submitting the message; "
                    "resolve it in the terminal and retry."
                )
            if _approval_pending(state) or not state.editor_present:
                continue
            if state.editor_content is not None and draft_seen and not state.editor_content:
                return
            if state.exit_armed:
                raise RuntimeError("Kimi terminal is exit-armed; press Escape and retry")
            _raise_if_injection_cancelled(cancellation)
            _run_tmux(socket_path, "send-keys", "-t", tmux_target, "Enter")
            last_enter = time.monotonic()
        raise RuntimeError(
            f"Kimi TUI did not accept the submitted message within {_SUBMIT_VERIFY_TIMEOUT_S}s; "
            "the message was not delivered"
        )
    finally:
        _unregister_injection(bridge_dir, event)


def inject_interrupt(bridge_dir: Path, *, timeout_s: float = _TMUX_READY_TIMEOUT_S) -> None:
    """Cancel the in-flight Kimi turn by sending ``Escape`` to the pane.

    kimi stops a running turn on a single ``Escape`` (verified live).
    The harness ``run_turn`` returns right after the paste, so the runner's
    in-process cancel floor can't reach the turn — this is the analog of
    :func:`inject_user_message` for the web UI's Stop button.

    :raises RuntimeError: If the tmux target is not advertised or send-keys fails.
    """
    _cancel_injections(bridge_dir)
    info = _wait_for_tmux_info(bridge_dir, timeout_s=timeout_s)
    # No ``-l``: tmux must interpret ``Escape`` as a key name.
    _run_tmux(info["socket_path"], "send-keys", "-t", info["tmux_target"], "Escape")


#: Web-UI approve/deny → option digit in kimi's fixed numbered menu
#: (1=Approve once, 2=Approve for this session, 3=Reject, 4=Reject with feedback).
#: "Approve once" re-prompts each call so Omnigent governs every one.
APPROVE_KEY = "1"
DENY_KEY = "3"


def inject_approval_keystroke(
    bridge_dir: Path, *, key: str, timeout_s: float = _TMUX_READY_TIMEOUT_S
) -> bool:
    """Answer kimi's tool-permission menu by typing its option digit.

    kimi's permission prompt is a numbered select; the web-UI Approve/Deny
    buttons map to :data:`APPROVE_KEY` / :data:`DENY_KEY`. Kimi confirms the
    choice as soon as the digit is selected.

    Captures the pane first and injects only when a permission-menu label is
    visible, preventing a verdict from leaking into the next TUI prompt.

    :param bridge_dir: The kimi-native bridge dir holding ``tmux.json``.
    :param key: The option digit to select (e.g. :data:`APPROVE_KEY`).
    :returns: ``True`` if the keystroke was injected.
    :raises RuntimeError: If the tmux target is not advertised or send-keys fails.
    """
    info = _wait_for_tmux_info(bridge_dir, timeout_s=timeout_s)
    socket_path = info["socket_path"]
    tmux_target = info["tmux_target"]
    if not _session_alive(socket_path, tmux_target):
        message = "Kimi permission menu unavailable because the TUI session is not running"
        _logger.warning(message)
        raise KimiApprovalSessionNotFoundError(message)
    pane = _capture_pane(socket_path, tmux_target)
    state = _parse_pane(pane)
    if not state.menu_visible:
        message = "Kimi permission menu markers missing; approval keystroke was not sent"
        _logger.warning("%s; pane tail=%r", message, pane[-240:])
        raise KimiApprovalPromptNotFoundError(message)
    menu_identity = _approval_menu_identity(state)
    _run_tmux(socket_path, "send-keys", "-t", tmux_target, key)
    deadline = time.monotonic() + _APPROVAL_SETTLE_TIMEOUT_S
    last_capture_empty = False
    while time.monotonic() < deadline:
        pane = _capture_pane(socket_path, tmux_target)
        last_capture_empty = not pane.strip()
        if last_capture_empty:
            if not _session_alive(socket_path, tmux_target):
                message = "Kimi permission menu unavailable because the TUI session is not running"
                _logger.warning(message)
                raise KimiApprovalSessionNotFoundError(message)
            time.sleep(_POLL_INTERVAL_S)
            continue
        state = _parse_pane(pane)
        if not state.menu_visible:
            return True
        if _approval_menu_identity(state) != menu_identity:
            return True
        time.sleep(_POLL_INTERVAL_S)
    message = (
        "Kimi permission menu state remained indeterminate after the approval keystroke"
        if last_capture_empty
        else "Kimi permission menu remained visible after the approval keystroke"
    )
    _logger.warning(message)
    raise KimiApprovalPromptAmbiguousError(message)


def kill_session(bridge_dir: Path, *, timeout_s: float = _TMUX_READY_TIMEOUT_S) -> None:
    """Hard-stop the Kimi session by killing its tmux session.

    Terminates ``kimi`` and the pane outright — the analog of the
    user manually exiting the attached TUI, for the web UI's "Stop session"
    affordance. Mirrors :func:`omnigent.claude_native_bridge.kill_session`.

    :raises RuntimeError: If the tmux target is not advertised or kill-session fails.
    """
    info = _wait_for_tmux_info(bridge_dir, timeout_s=timeout_s)
    _run_tmux(info["socket_path"], "kill-session", "-t", info["tmux_target"])
