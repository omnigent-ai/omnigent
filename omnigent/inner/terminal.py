"""Terminal environment: managed tmux sessions with optional OS environments.

Each terminal instance runs a command in its own tmux server (isolated socket)
with optional filesystem isolation (fork) and sandboxing (bwrap/seccomp).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, TypeAlias

from omnigent.runner.identity import strip_runner_auth_secrets

from .datamodel import OSEnvSandboxSpec, OSEnvSpec, TerminalEnvSpec
from .egress import EgressProxyHandle, apply_egress_env, start_egress_proxy
from .os_env import (
    OSEnvironment,
    _copy_tree,
    create_os_environment,
)
from .sandbox import (
    SandboxPolicy,
    cleanup_private_tmpdir,
    create_exec_launcher,
    create_private_tmpdir,
    resolve_sandbox,
    with_additional_write_roots,
    with_denied_unix_sockets,
)

# Heterogeneous JSON-shaped result returned by :meth:`TerminalInstance.send`
# and :meth:`TerminalInstance.read`. In practice the dicts carry a mix of
# ``{"status": str}``, ``{"error": str}``, and ``{"terminal": str, "screen":
# str, "scrollback_lines": int}`` — a TypedDict union would spread across
# every caller (session.py's ``_terminal_send`` / ``_terminal_read``) so we
# keep the boundary open and let those callers pass it through as a
# ``ToolResult``.
TerminalResult: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]

logger = logging.getLogger(__name__)

_TMUX_CONFIG_PATH = os.devnull
_TMUX_CONVERSATION_LINK_OPTION = "@omnigent-conversation-link"
_TMUX_START_ON_ATTACH_CHANNEL = "omnigent-start-on-attach"
# Each terminal instance lives in a private tmpdir with this prefix
# (see ``create_terminal_instance``). The owner-pid marker inside it
# records the process that launched the instance so a later startup
# can reap tmux servers whose owner died without graceful shutdown
# (``reap_orphaned_terminals``).
_TERMINAL_DIR_PREFIX = "omnigent-terminal-"
_OWNER_PID_FILENAME = "owner.pid"
# Bound for each ``tmux kill-server`` in the orphan sweep; a wedged
# tmux must not stall runner startup.
_REAP_KILL_TIMEOUT_S = 10.0
# Literal tmux empty option value. Passing this as an argv value clears
# status segments and window formats; it is not an application sentinel.
_TMUX_EMPTY_OPTION_VALUE = ""


def _tmux_command_sequence(commands: list[list[str]]) -> list[str]:
    """
    Flatten tmux commands into one client command sequence.

    Tmux accepts multiple commands in one invocation when separated by
    a literal ``;`` argv. This lets Omnigent configure a fresh
    private server and create the session without writing a tmux conf
    file.

    :param commands: Tmux commands without the leading ``tmux`` argv,
        e.g. ``[["set-option", "-g", "mouse", "on"], ["new-session"]]``.
    :returns: Flattened argv suffix with command separators.
    """
    sequence: list[str] = []
    for command in commands:
        if sequence:
            sequence.append(";")
        sequence.extend(command)
    return sequence


def _tmux_managed_option_commands(
    scrollback: int,
    *,
    allow_passthrough: bool = False,
) -> list[list[str]]:
    """
    Build tmux commands for Omnigent-managed global options.

    :param scrollback: Tmux history limit, e.g. ``10000``.
    :param allow_passthrough: Whether to allow pane programs to send
        passthrough escape sequences to the real attached terminal.
    :returns: List of tmux commands to run before ``new-session``.
    """
    commands = [
        *_tmux_input_option_commands(scrollback),
        *_tmux_lockdown_commands(),
        *_tmux_status_option_commands(),
    ]
    if allow_passthrough:
        commands.append(["set-option", "-g", "allow-passthrough", "on"])
    return commands


def _tmux_input_option_commands(scrollback: int) -> list[list[str]]:
    """
    Build tmux options for scrollback and pane input behavior.

    ``history-limit`` is generated per terminal because it comes from
    ``TerminalEnvSpec.scrollback``. ``mouse on`` makes the attached web
    terminal scrollable. ``focus-events on`` lets interactive programs
    observe pane focus changes. ``extended-keys`` with CSI-u formatting
    lets programs inside tmux receive Kitty Keyboard Protocol keys such
    as Shift+Enter when the attached terminal supports them. Terminals
    without that protocol ignore tmux's request, and the quiet tmux
    options keep older tmux versions from failing launch. ``escape-time
    0`` prevents pasted ANSI escape bytes from accumulating tmux's
    default delay.

    :param scrollback: Tmux history limit, e.g. ``10000``.
    :returns: Tmux commands configuring pane input and scrollback.
    """
    return [
        ["set-option", "-g", "history-limit", str(scrollback)],
        ["set-option", "-sq", "extended-keys", "on"],
        ["set-option", "-sq", "extended-keys-format", "csi-u"],
        ["set-option", "-g", "mouse", "on"],
        ["set-option", "-g", "focus-events", "on"],
        ["set-option", "-g", "escape-time", "0"],
    ]


def _tmux_lockdown_commands() -> list[list[str]]:
    """
    Build tmux commands that remove user-facing pane/window creation controls.

    Managed terminals must stay inside Omnigent' terminal registry.
    Disabling the prefix table and right-click context menus prevents an
    attached user from creating extra panes, windows, or sessions through
    tmux UI controls. The root-table unbinds are quiet so missing default
    mouse bindings on a tmux version do not fail terminal launch.

    :returns: Tmux commands that disable prefix and creation menus.
    """
    return [
        ["set-option", "-g", "prefix", "None"],
        ["set-option", "-g", "prefix2", "None"],
        ["unbind-key", "-a", "-T", "prefix"],
        ["unbind-key", "-q", "-T", "root", "MouseDown3Pane"],
        ["unbind-key", "-q", "-T", "root", "M-MouseDown3Pane"],
        ["unbind-key", "-q", "-T", "root", "MouseDown3Status"],
        ["unbind-key", "-q", "-T", "root", "M-MouseDown3Status"],
        ["unbind-key", "-q", "-T", "root", "MouseDown3StatusLeft"],
        ["unbind-key", "-q", "-T", "root", "M-MouseDown3StatusLeft"],
    ]


def _tmux_status_option_commands() -> list[list[str]]:
    """
    Build tmux status-line options for managed terminals.

    The status line carries the conversation link while hiding tmux's
    window list so users do not see irrelevant tmux chrome for the
    private single-window server.

    :returns: Tmux commands configuring the managed status line.
    """
    return [
        ["set-option", "-g", "status", "on"],
        ["set-option", "-g", "status-style", "fg=default,bg=default"],
        [
            "set-option",
            "-g",
            "status-left",
            f"Omnigent: #{{{_TMUX_CONVERSATION_LINK_OPTION}}}",
        ],
        ["set-option", "-g", "status-left-style", "fg=default,bg=default"],
        ["set-option", "-g", "status-left-length", "200"],
        ["set-option", "-g", "status-right", _TMUX_EMPTY_OPTION_VALUE],
        ["set-option", "-g", "status-right-style", "fg=default,bg=default"],
        ["set-option", "-g", "status-right-length", "0"],
        ["set-option", "-g", "window-status-separator", _TMUX_EMPTY_OPTION_VALUE],
        ["set-window-option", "-g", "window-status-format", _TMUX_EMPTY_OPTION_VALUE],
        [
            "set-window-option",
            "-g",
            "window-status-current-format",
            _TMUX_EMPTY_OPTION_VALUE,
        ],
    ]


# How long the tmux pane must show no changes to be considered idle, and how
# often we poll capture-pane to check. Exposed as module-level constants so
# tests can lower them instead of waiting the full threshold per assertion.
_IDLE_THRESHOLD_SECONDS = 10.0
_IDLE_POLL_INTERVAL_SECONDS = 1.0

# When a web client interacts with the terminal (attach/detach, focus
# in/out, mouse, keystroke, resize — all stamped via
# ``TerminalInstance.note_client_interaction``), the TUI repaints in
# response. Those repaints are client-driven, not agent work, so the idle
# watcher discounts any pane change that lands within this window of the
# last interaction. The window must comfortably exceed the poll interval so
# a repaint that trails its triggering event by a tick (or a browser's
# burst of resizes on attach) is still absorbed. It only suppresses the
# *activity* edge — idle detection is unaffected — so the cost is at most a
# slightly-late ``running`` if the agent starts working within the window
# of an interaction.
_CLIENT_INTERACTION_WINDOW_SECONDS = 0.75

# Substrings that indicate the terminal is waiting for a human response even
# while other cells on the pane keep changing (e.g. Codex's blinking spinner
# glyph during a permission prompt). When any marker has been continuously
# visible in the ANSI-stripped pane capture for _IDLE_MARKER_THRESHOLD_SECONDS,
# the watcher treats the pane as idle. This is an alternative trigger to the
# diff-based one above; both tracks share a single ``idle_notified`` gate so
# at most one ``on_idle`` call fires per idle episode.
#
# Tests monkey-patch this list at the module level by rebinding (not by
# ``.append``/``.clear``), so production code must only read — never mutate —
# this list at runtime. Keep marker substrings short (well under 80 chars)
# so tmux's pane-width line wrapping (``-x 80`` at creation in ``launch``;
# wider once a client attaches) doesn't split them across a newline and
# defeat the substring match.
_IDLE_MARKER_SUBSTRINGS: list[str] = [
    "Press enter to confirm or esc to cancel",
    "1. Yes",
]
# Defaults to the same threshold as the diff path for simplicity. Kept as a
# separate name so tests (and future callers) can tune it independently; any
# future runtime change to _IDLE_THRESHOLD_SECONDS does NOT propagate here.
_IDLE_MARKER_THRESHOLD_SECONDS: float = _IDLE_THRESHOLD_SECONDS

# Bounded join window when stopping a threaded idle watcher. Long enough
# to let a tick that's currently inside ``subprocess.run`` finish, short
# enough that ``close()`` doesn't block the event loop visibly. The
# tmux capture-pane subprocess is the only operation in the loop body
# that can outlast a single Python frame; it normally returns in <50ms.
_IDLE_WATCHER_JOIN_TIMEOUT_S = 1.0

# tmux's client→server protocol rejects any single command larger than
# its 16KB imsg cap — the client exits non-zero with "command too long".
# Literal text typed via ``send-keys -l`` is therefore chunked so each
# invocation stays far under the cap even at 4 UTF-8 bytes per character
# (1024 chars ≤ 4KB packed). tmux writes each invocation's bytes to the
# pane in submission order, so the program sees one contiguous stream.
_SEND_KEYS_LITERAL_CHARS_PER_CALL = 1024


class _IdleDetector:
    """
    Pure state machine for the pane-idle decision.

    One instance per watcher invocation. Drive it by passing a fresh
    pane snapshot to :meth:`tick` once per poll interval; the return
    value indicates whether ``on_idle`` should fire this tick.

    Two parallel tracks share a single ``idle_notified`` gate so at
    most one notification fires per idle episode:

    1. **Marker track:** any substring in :data:`_IDLE_MARKER_SUBSTRINGS`
       that has been continuously visible for
       :data:`_IDLE_MARKER_THRESHOLD_SECONDS` triggers idleness even
       while other cells on the pane keep changing (e.g. a blinking
       spinner under a permission prompt).
    2. **Diff track:** the snapshot bytes have been unchanged for
       :data:`_IDLE_THRESHOLD_SECONDS`.

    Extracted from :meth:`TerminalInstance._idle_watch_loop` so the
    asyncio watcher (legacy inner Session path) and the threading
    watcher (AP ``sys_terminal_launch`` path) share one source of
    truth for the detection logic — refactoring the watcher to add a
    tracker now updates both paths automatically.
    """

    def __init__(self, *, idle_threshold_s: float | None = None) -> None:
        """Initialize per-watcher state.

        Each watcher invocation creates a fresh detector. The diff-track
        idle threshold defaults to the module constant (which tests
        rebind), but a caller can override it per-watcher — the
        claude-native status watcher uses a short threshold (~1s) so the
        session flips to ``idle`` promptly after Claude stops redrawing,
        while the generic terminal-activity watcher keeps the longer
        default.

        :param idle_threshold_s: Per-watcher diff-track idle threshold in
            seconds, e.g. ``1.0``. ``None`` falls back to the module
            constant :data:`_IDLE_THRESHOLD_SECONDS` at each tick (so
            tests that rebind the module constant still take effect).
            Does not affect the marker track, which always uses
            :data:`_IDLE_MARKER_THRESHOLD_SECONDS`.
        """
        self._last_snapshot: str | None = None
        self._last_change_at: float = time.monotonic()
        self._idle_notified: bool = False
        self._marker_first_seen_at: dict[str, float] = {}
        self._marker_notified: dict[str, bool] = {}
        # Per-watcher idle-threshold override; ``None`` means "read the
        # live module constant in ``tick``" so test rebinds still apply.
        self._idle_threshold_s: float | None = idle_threshold_s
        # Set by ``tick`` to whether the pane content changed *this* tick
        # (the diff track's edge). Read by the watcher loop to drive an
        # ``on_activity`` callback — the runner-determined "this terminal's
        # PTY produced output" signal that powers the web activity badge,
        # without any client PTY attach.
        self.changed_this_tick: bool = False

    def tick(self, snapshot: str, suppress_activity: bool = False) -> bool:
        """
        Feed a fresh pane snapshot and report whether idle fired.

        :param snapshot: The pane bytes from ``tmux capture-pane -p
            -e``, e.g. the raw ANSI-laden output of one capture call.
            Marker matching strips ANSI internally; the diff track
            compares the raw bytes verbatim.
        :param suppress_activity: When ``True``, a content change this
            tick is treated as a client-driven repaint (attach/detach
            reflow, focus, mouse, keystroke) rather than agent output: the
            snapshot is re-baselined but does NOT register as activity and
            does NOT reset the idle timer. The caller sets this when a web
            client interacted with the terminal within the recent window
            (see :data:`_CLIENT_INTERACTION_WINDOW_SECONDS`).
        :returns: ``True`` if this tick crosses an idle edge and the
            caller should invoke ``on_idle`` once. ``False`` on every
            subsequent tick of the same idle episode (re-arm requires
            new output that mutates the snapshot).
        """
        now = time.monotonic()
        # Reset the per-tick activity edge; set True below only when the
        # diff track sees the pane content actually change this tick.
        self.changed_this_tick = False
        stripped = _strip_ansi(snapshot) if _IDLE_MARKER_SUBSTRINGS else ""

        # Marker pass 1: update per-marker timers and cleanup absent
        # markers. Cleanup runs for ALL markers before the fire pass
        # so we don't leave stale per-marker state behind when we
        # break out of the fire pass below.
        for marker in _IDLE_MARKER_SUBSTRINGS:
            if marker in stripped:
                self._marker_first_seen_at.setdefault(marker, now)
            else:
                self._marker_first_seen_at.pop(marker, None)
                self._marker_notified.pop(marker, None)

        # Marker pass 2: pick the first eligible marker and fire once.
        # When we fire, mark EVERY currently-present marker as notified
        # so that if the diff track later clears ``idle_notified``
        # (because pane bytes keep changing under a persistent spinner),
        # another currently-visible marker cannot sneak through and
        # fire a second time within the same idle episode.
        if not self._idle_notified:
            for marker in _IDLE_MARKER_SUBSTRINGS:
                if (
                    marker in stripped
                    and not self._marker_notified.get(marker, False)
                    and now - self._marker_first_seen_at[marker] >= _IDLE_MARKER_THRESHOLD_SECONDS
                ):
                    self._idle_notified = True
                    for other in _IDLE_MARKER_SUBSTRINGS:
                        if other in stripped:
                            self._marker_notified[other] = True
                    return True

        # Diff track: shares ``idle_notified`` with the marker track
        # so we never double-fire.
        if self._last_snapshot is None:
            self._last_snapshot = snapshot
            self._last_change_at = now
            return False

        if snapshot != self._last_snapshot:
            self._last_snapshot = snapshot
            if suppress_activity:
                # A web client interacted within the recent window, so this
                # change is a client-driven repaint (attach/detach reflow,
                # focus, mouse, keystroke), not agent output. Re-baseline to
                # the new snapshot, but leave the change timer and idle
                # state untouched so it neither reads as ``running`` nor
                # re-arms an idle edge.
                return False
            self._last_change_at = now
            self._idle_notified = False
            self.changed_this_tick = True
            return False

        if self._idle_notified:
            return False

        visible_notified_marker = any(
            marker in stripped and self._marker_notified.get(marker, False)
            for marker in _IDLE_MARKER_SUBSTRINGS
        )
        idle_threshold_s = (
            self._idle_threshold_s
            if self._idle_threshold_s is not None
            else _IDLE_THRESHOLD_SECONDS
        )
        if now - self._last_change_at >= idle_threshold_s:
            self._idle_notified = True
            # If the diff track fires while an idle marker is visible, treat
            # that marker as having delivered this idle episode too. Otherwise
            # a shell can emit the marker, quiesce long enough for the diff
            # track to fire, repaint the prompt, and then let the still-visible
            # marker fire a duplicate notification before it disappears.
            for marker in _IDLE_MARKER_SUBSTRINGS:
                if marker in stripped:
                    self._marker_notified[marker] = True
            if visible_notified_marker:
                return False
            return True

        return False


def _clone_sandbox_spec(sandbox: OSEnvSandboxSpec | None) -> OSEnvSandboxSpec | None:
    """Deep-copy an :class:`OSEnvSandboxSpec` for a terminal launch.

    Uses :func:`dataclasses.replace` so every scalar/bool field is
    carried through automatically — the previous hand-written
    field-by-field constructor silently dropped fields added after
    it was written (``egress_rules``, ``egress_allow_private_destinations``,
    ``cwd_allow_hidden``, ``env_passthrough``,
    ``cwd_hidden_scan_max_entries``, ``cwd_hidden_scan_overflow``),
    which downgraded terminal sandboxes to "no MITM proxy, default
    env, only ``.venv`` allowed through" even when the YAML defined
    a strict policy. List fields are explicitly cloned to preserve
    the "doesn't mutate the original" invariant covered by
    :func:`test_build_terminal_os_env_spec_does_not_mutate_original_spec`.
    """
    if sandbox is None:
        return None
    return replace(
        sandbox,
        read_paths=list(sandbox.read_paths) if sandbox.read_paths is not None else None,
        write_paths=list(sandbox.write_paths) if sandbox.write_paths is not None else None,
        write_files=list(sandbox.write_files) if sandbox.write_files is not None else None,
        cwd_allow_hidden=(
            list(sandbox.cwd_allow_hidden) if sandbox.cwd_allow_hidden is not None else None
        ),
        env_passthrough=(
            list(sandbox.env_passthrough) if sandbox.env_passthrough is not None else None
        ),
        egress_rules=list(sandbox.egress_rules) if sandbox.egress_rules is not None else None,
    )


def _clone_os_env_spec(spec: OSEnvSpec) -> OSEnvSpec:
    """Deep-copy an :class:`OSEnvSpec` for a terminal launch.

    Uses :func:`dataclasses.replace` (with an explicitly-cloned
    ``sandbox`` via :func:`_clone_sandbox_spec`) so every
    :class:`OSEnvSpec` field — including ``start_in_scratch`` —
    is carried through. The previous hand-written constructor
    omitted ``start_in_scratch``, silently resetting it to
    ``False`` whenever a terminal inherited its parent's os_env.
    """
    return replace(spec, sandbox=_clone_sandbox_spec(spec.sandbox))


# Regex to strip ANSI escape codes from terminal output.
_ANSI_RE = re.compile(
    r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\].*?(?:\x07|\x1b\\)|\x1b[()][AB012]|\x1b\[[\?]?[0-9;]*[hlm]"
)


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape codes from terminal output."""
    return _ANSI_RE.sub("", text)


def _tmux_available() -> bool:
    """Check if tmux is installed."""
    return shutil.which("tmux") is not None


def _process_alive(pid: int) -> bool:
    """
    Return whether a process with *pid* currently exists.

    Used by the orphan sweep as the owner-death check. The check is
    conservative in the dangerous direction: ``ProcessLookupError`` is a
    definitive "gone", while a reused pid (or one owned by another user,
    which raises ``PermissionError``) reads as alive and merely defers
    the reap to a later sweep — it can never kill a live owner's
    terminal.

    :param pid: Process id recorded at instance creation,
        e.g. ``48213``.
    :returns: ``True`` when a process with that pid exists.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _terminals_tmp_root() -> Path:
    """
    Return the directory scanned for terminal instance dirs.

    Indirection point so tests can retarget the orphan sweep at a
    scratch directory without monkeypatching the process-wide
    ``tempfile`` module (see omnigent-testing rule 14).

    :returns: The system temp directory, e.g. ``Path("/tmp")``.
    """
    return Path(tempfile.gettempdir())


def reap_orphaned_terminals() -> int:
    """
    Kill terminal tmux servers whose owning process is gone.

    Terminal tmux servers are deliberately detached so they survive
    transient client disconnects; graceful shutdown closes them
    (``TerminalRegistry.shutdown``), but a SIGKILL'd runner — or one
    whose whole process group is torn down by a test harness — leaks
    them forever, one per session now that runner-bound SDK sessions
    auto-create the embedded REPL terminal. Each instance dir records
    its owner pid at creation; this sweep (run at runner startup) kills
    the tmux server of every instance whose owner no longer exists and
    removes the instance dir. Dirs without an owner-pid marker are left
    untouched — they are either from an older version or not ours.

    :returns: The number of orphaned instance dirs reaped.
    """
    if not _tmux_available():
        return 0
    reaped = 0
    for entry in _terminals_tmp_root().glob(f"{_TERMINAL_DIR_PREFIX}*"):
        try:
            pid = int((entry / _OWNER_PID_FILENAME).read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            continue
        if _process_alive(pid):
            continue
        socket_path = entry / "tmux.sock"
        if socket_path.exists():
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                subprocess.run(
                    ["tmux", "-S", str(socket_path), "kill-server"],
                    # kill-server on an already-dead server exits non-zero;
                    # that is the common case for half-torn-down orphans.
                    check=False,
                    capture_output=True,
                    timeout=_REAP_KILL_TIMEOUT_S,
                )
        shutil.rmtree(entry, ignore_errors=True)
        reaped += 1
    return reaped


def build_terminal_os_env_spec(
    spec: TerminalEnvSpec,
    *,
    parent_os_env_spec: OSEnvSpec | None = None,
    cwd_override: str | None = None,
    sandbox_override: str | None = None,
) -> OSEnvSpec:
    effective_os_env_spec: OSEnvSpec | None = None
    if spec.os_env == "inherit" or spec.os_env is None:
        effective_os_env_spec = (
            _clone_os_env_spec(parent_os_env_spec) if parent_os_env_spec is not None else None
        )
    elif isinstance(spec.os_env, OSEnvSpec):
        effective_os_env_spec = _clone_os_env_spec(spec.os_env)

    if effective_os_env_spec is None:
        effective_os_env_spec = OSEnvSpec(
            type="caller_process",
            cwd=os.getcwd(),
            sandbox=OSEnvSandboxSpec(type="none"),
        )

    if cwd_override is not None:
        if not spec.allow_cwd_override:
            raise ValueError("This terminal does not allow cwd overrides")
        # Containment check: the LLM-supplied cwd must resolve to the
        # spec's cwd or a subdirectory of it. Without this guard, an
        # LLM with ``allow_cwd_override: true`` could repoint the
        # terminal anchor anywhere — e.g. ``/``, ``~/.ssh``, ``/etc`` —
        # which would:
        #
        # - On bwrap: bind-mount that location as the workspace root
        #   (escapes the project sandbox).
        # - On seatbelt: anchor the dotfile/credential masker at the
        #   wrong root so e.g. ``~/.ssh/id_rsa`` is no longer a
        #   "hidden" path under the new cwd.
        # - Resolve ``write_paths: ["."]`` to the new root, granting
        #   writes anywhere the LLM picks.
        #
        # Relative overrides are interpreted against the spec's cwd
        # (not the supervisor's ``os.getcwd()``) so the LLM can say
        # ``cd .worktrees/foo`` without depending on where the
        # supervisor was launched from. Absolute overrides are
        # checked literally; they must still be under the spec cwd.
        if effective_os_env_spec.cwd:
            allowed_root = Path(effective_os_env_spec.cwd).expanduser().resolve(strict=False)
        else:
            allowed_root = Path(os.getcwd()).resolve(strict=False)
        override_path = Path(cwd_override).expanduser()
        if override_path.is_absolute():
            resolved_override = override_path.resolve(strict=False)
        else:
            resolved_override = (allowed_root / override_path).resolve(strict=False)
        try:
            resolved_override.relative_to(allowed_root)
        except ValueError as exc:
            raise ValueError(
                f"cwd override {cwd_override!r} resolves to {resolved_override} "
                f"which is outside the allowed root {allowed_root}. A terminal "
                "cwd override must point at the spec's cwd or a subdirectory "
                "of it; pointing elsewhere would escape the sandbox's "
                "filesystem and dotfile-masking anchors."
            ) from exc
        effective_os_env_spec.cwd = str(resolved_override)

    if sandbox_override is not None:
        if not spec.allow_sandbox_override:
            raise ValueError("This terminal does not allow sandbox overrides")
        sandbox = effective_os_env_spec.sandbox or OSEnvSandboxSpec(type="none")
        # Defense in depth on top of the parse-time check
        # (omnigent/inner/loader.py rejects allow_sandbox_override:
        # true paired with egress_rules at agent-load time). This
        # branch also fires for specs built programmatically without
        # going through the loader and catches any future code path
        # that synthesizes an override before launch. An override to
        # ``"none"`` can't hard-enforce network isolation; letting the
        # override drop ``sandbox.type`` to it while ``egress_rules``
        # stay on the policy would silently bypass the network
        # allow-list.
        if sandbox.egress_rules:
            raise ValueError(
                "sandbox_override is not allowed on a terminal whose "
                "effective sandbox declares egress_rules: overriding "
                "to 'none' would drop hard network "
                "enforcement while egress_rules remain as inert "
                "decoration on the policy."
            )
        sandbox.type = sandbox_override
        effective_os_env_spec.sandbox = sandbox

    return effective_os_env_spec


@dataclass
class TerminalInstance:
    """
    One running tmux session for a terminal environment.

    :param name: Terminal name from the agent spec, e.g. ``"bash"``.
    :param session_key: Per-launch session key, e.g. ``"s1"``.
    :param socket_path: Private tmux socket path for this instance.
    :param private_dir: Private directory holding the tmux socket and
        any forked workspace state.
    :param os_env: Optional OS environment backing this terminal.
    :param command: Executable to run inside tmux, e.g. ``"bash"``.
    :param args: Command arguments.
    :param env: Extra environment variables for the terminal process.
    :param env_unset: Environment variables to strip from the
        terminal's environment before launching, e.g.
        ``["DATABRICKS_CONFIG_PROFILE"]``. Applied AFTER ``env``
        is merged, so a listed key is removed unconditionally —
        if the same key also appears in ``env``, the strip wins.
        Intentional: ``env_unset`` is a leak-prevention boundary,
        not a soft default.
    :param inherit_env: Whether to start from ``os.environ`` before applying
        ``env`` / ``env_unset``.
    :param sandbox_policy: Optional sandbox wrapper policy.
    :param conversation_link: Optional web UI link for the owning
        conversation, e.g. ``"/c/conv_abc123"``.
    :param scrollback: Tmux scrollback history limit.
    :param tmux_allow_passthrough: Whether pane applications may use
        tmux passthrough escapes to query/control the attached terminal.
    :param tmux_start_on_attach: Whether to delay command startup until
        the first tmux client attaches to the session.
    :param running: Whether the tmux server is currently expected to
        be alive.
    """

    name: str
    session_key: str
    socket_path: Path
    private_dir: Path
    os_env: OSEnvironment | None = None
    command: str = "bash"
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    env_unset: list[str] = field(default_factory=list)
    inherit_env: bool = True
    sandbox_policy: SandboxPolicy | None = None
    conversation_link: str | None = None
    # Egress allow-list to enforce for this terminal. Populated
    # from the effective ``OSEnvSandboxSpec.egress_rules`` at
    # create-instance time. When non-empty AND the sandbox uses
    # a spawn-time backend (``linux_bwrap`` / ``darwin_seatbelt``),
    # :meth:`launch` starts a parent-side L7 MITM proxy and threads
    # ``HTTP_PROXY`` / ``HTTPS_PROXY`` / CA env vars through to the
    # tmux-spawned shell so its outbound HTTP(S) traffic is
    # allow-listed by the same engine that gates the helper. The
    # ``SandboxPolicy.egress_relay_port`` / ``egress_socket_path``
    # fields are populated from the proxy handle before encoding
    # the policy into the launcher script.
    egress_rules: list[str] | None = None
    egress_allow_private_destinations: bool = False
    scrollback: int = 10000
    tmux_allow_passthrough: bool = False
    tmux_start_on_attach: bool = False
    running: bool = False
    launch_cwd: str | None = None
    # Owned per-launch egress proxy. ``None`` when the sandbox
    # carries no ``egress_rules`` or the backend doesn't need a
    # spawn-time wrap (the ``none`` backend does nothing here). Cleaned
    # up in :meth:`close` so the asyncio thread and the bound
    # Unix socket don't outlive the terminal.
    _egress_handle: EgressProxyHandle | None = field(default=None, repr=False)
    _egress_tmpdir: Path | None = field(default=None, repr=False)
    _idle_task: asyncio.Task[None] | None = field(default=None, repr=False)
    # Threaded idle-watcher state. Mirrors :attr:`_idle_task` but for
    # callers that don't have a long-lived event loop (the Omnigent path:
    # ``SysTerminalLaunchTool`` runs ``asyncio.run`` per call, so an
    # asyncio task started inside it dies the moment ``launch``
    # returns). The thread polls tmux capture-pane synchronously
    # under ``_idle_stop_event``.
    _idle_thread: threading.Thread | None = field(default=None, repr=False)
    _idle_stop_event: threading.Event | None = field(default=None, repr=False)
    # Monotonic timestamp of the last client interaction observed on this
    # terminal's web attach (keystroke / focus / mouse / resize / connect /
    # disconnect — see :meth:`note_client_interaction`). The idle watcher
    # discounts pane changes that land within a short window of this stamp,
    # so a client attaching, detaching, focusing, clicking, or typing does
    # not read as agent activity. ``-inf`` until the first interaction.
    _last_client_interaction_at: float = field(default=float("-inf"), repr=False)
    _last_pane_snapshot: str | None = field(default=None, repr=False)

    @property
    def tmux_target(self) -> str:
        """The tmux target for send-keys/capture-pane (always 'main')."""
        return "main"

    def note_client_interaction(self) -> None:
        """Record that a web client just interacted with this terminal.

        Called from the WebSocket attach bridge on every client event —
        connect, disconnect, a forwarded keystroke/focus/mouse byte, or a
        resize message. The idle watcher reads
        :attr:`_last_client_interaction_at` and discounts pane changes
        within a short window of it, so client-driven repaints (attach /
        detach reflow, focus in/out, clicks, typing) don't register as
        agent activity.

        Thread-safety: this is written on the event loop (the attach
        bridge) and read on the watcher's daemon thread. It's a single
        ``float`` assignment, atomic under the GIL, so no lock is needed —
        a stale read is at worst a timestamp a few milliseconds old, which
        the window tolerates.

        :returns: None.
        """
        self._last_client_interaction_at = time.monotonic()

    def last_pane_text(self) -> str | None:
        """Return the last visible pane text captured for diagnostics.

        The value is updated opportunistically by reads and watcher polls.
        It is intentionally a snapshot, not a live tmux query, so callers can
        still retrieve useful context after tmux has already disappeared.
        """
        snapshot = self._last_pane_snapshot
        if snapshot is None:
            return None
        text = _strip_ansi(snapshot).strip()
        return text or None

    def _remember_pane_snapshot(self, snapshot: str) -> None:
        """Store a pane capture for later exit diagnostics."""
        self._last_pane_snapshot = snapshot

    def _tmux_base_cmd(self) -> list[str]:
        """
        Build the tmux argv prefix for this instance's private server.

        Managed terminal sessions must not inherit the user's
        ``~/.tmux.conf``. The terminal integration owns the server
        lifecycle and applies the supported options explicitly during
        launch, so user config would make identical agent specs behave
        differently across machines.

        :returns: Base argv for subprocess calls, e.g.
            ``["tmux", "-S", "/tmp/.../tmux.sock", "-f", "/dev/null"]``.
        """
        return ["tmux", "-S", str(self.socket_path), "-f", _TMUX_CONFIG_PATH]

    async def set_conversation_link(self, conversation_link: str | None) -> None:
        """
        Update the link shown in this terminal's tmux status bar.

        :param conversation_link: Conversation URL to show, e.g.
            ``"/c/conv_abc123"``, or ``None`` to clear the status
            value.
        :returns: None.
        :raises RuntimeError: If the running tmux server rejects the
            option update.
        """
        self.conversation_link = conversation_link
        if not self.running:
            return
        await self._tmux(
            "set-option",
            "-g",
            _TMUX_CONVERSATION_LINK_OPTION,
            conversation_link or "",
        )

    async def launch(self, *, cwd: Path | None = None) -> None:
        """Start the tmux session."""
        if self.running:
            return
        effective_cwd = str(cwd or self.private_dir)

        # Do NOT advertise the tmux control socket path to the
        # pane. The tmux server runs unsandboxed, so exposing its socket
        # let pane code run ``tmux -S <sock> run-shell '...'`` to execute
        # commands outside the sandbox. The host-side control plane
        # addresses the socket via ``self.socket_path`` directly and never
        # needs the env var; any inherited value is stripped below too.
        env = dict(os.environ) if self.inherit_env else {}
        env.pop("OMNIGENT_TMUX_SOCK", None)
        # Apply per-terminal env overrides (takes precedence over inherited env).
        env.update(self.env)
        # Strip vars the caller asked us not to leak into the terminal —
        # ambient values like ``DATABRICKS_CONFIG_PROFILE`` would otherwise
        # propagate to the terminal's children (including MCP servers),
        # whose own auth resolution then picks up the parent's profile
        # instead of the credentials they were explicitly configured with.
        # Applied AFTER ``env.update`` so the strip wins even if the
        # same key was set in ``self.env`` — ``env_unset`` is a
        # leak-prevention boundary, not a soft default.
        for key in self.env_unset:
            env.pop(key, None)
        # Strip the runner-auth secret: native agents run their shell in
        # this tmux pane, so the binding token must never reach it.
        # After ``env.update`` so ``self.env`` can't re-admit it.
        env = strip_runner_auth_secrets(env)

        # Build the command to run inside tmux. If a sandbox policy
        # is configured, wrap the command in the sandbox launcher so
        # the process tree runs under bwrap / seatbelt —
        # the launcher's ``run_launcher`` re-execs itself under the
        # spawn-time wrap for ``linux_bwrap`` / ``darwin_seatbelt``
        # before activating the in-process pieces (relay daemon,
        # seccomp filter).
        #
        # When the sandbox carries ``egress_rules``, we also start
        # a parent-side MITM proxy here and bake its socket path /
        # relay port / CA bundle into the policy + env BEFORE
        # encoding the launcher. The launcher (post-wrap) reads
        # them off the encoded policy and starts the in-namespace
        # relay daemon during ``activate_sandbox``; the shell
        # spawned beyond the launcher inherits HTTP_PROXY / CA
        # env vars so its outbound traffic is filtered.
        sandbox_for_launcher: SandboxPolicy | None = self.sandbox_policy
        if sandbox_for_launcher is not None and sandbox_for_launcher.active:
            if self.egress_rules:
                sandbox_for_launcher = self._bootstrap_egress_proxy(sandbox_for_launcher, env)
            cli_path = shutil.which(self.command) or self.command
            launcher_path = create_exec_launcher(cli_path, sandbox_for_launcher)
            inner_cmd = [launcher_path, *self.args]
        else:
            inner_cmd = [self.command, *self.args]
        inner_str = " ".join(_shell_quote(c) for c in inner_cmd)
        if self.tmux_start_on_attach:
            inner_str = f"tmux wait-for {_TMUX_START_ON_ATTACH_CHANNEL}; exec {inner_str}"

        option_commands = [
            *_tmux_managed_option_commands(
                self.scrollback,
                allow_passthrough=self.tmux_allow_passthrough,
            ),
            [
                "set-option",
                "-g",
                _TMUX_CONVERSATION_LINK_OPTION,
                self.conversation_link or _TMUX_EMPTY_OPTION_VALUE,
            ],
        ]
        if self.tmux_start_on_attach:
            option_commands.append(
                [
                    "set-hook",
                    "-g",
                    "client-attached",
                    f"wait-for -S {_TMUX_START_ON_ATTACH_CHANNEL}",
                ]
            )
        cmd = [
            *self._tmux_base_cmd(),
            *_tmux_command_sequence(
                [
                    *option_commands,
                    [
                        "new-session",
                        "-d",
                        "-s",
                        self.tmux_target,
                        # Deliberately small: first attach GROWS (lossless).
                        # The old 200x50 meant first attach SHRANK, and ink's
                        # cursor-up repaint (counted in unwrapped rows)
                        # stitched frames into rewrapped debris — garbled text.
                        "-x",
                        "80",
                        "-y",
                        "24",
                        "-c",
                        effective_cwd,
                        inner_str,
                    ],
                ]
            ),
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"tmux launch failed (rc={proc.returncode}): {stderr.decode().strip()}"
            )

        self.running = True
        self.launch_cwd = effective_cwd

    async def send(
        self,
        text: str | None = None,
        *,
        keys: str = "Enter",
    ) -> TerminalResult:
        """Send keystrokes to the terminal.

        Args:
            text: Literal text to type.  Sent via ``tmux send-keys -l`` so
                special characters are not interpreted.  Long text is split
                across multiple invocations of
                :data:`_SEND_KEYS_LITERAL_CHARS_PER_CALL` characters each —
                a single invocation over ~16KB exceeds tmux's per-command
                cap and fails with "command too long".
            keys: Tmux key names to press after the text, space-separated.
                Defaults to ``"Enter"``.  Set to ``""`` to type text without
                pressing any key after.  Examples: ``"Enter"``, ``"Tab"``,
                ``"C-c"``, ``"Escape"``, ``"C-d"``, ``"Up"``.
        """
        if not self.running:
            return {"error": "Terminal is not running"}

        try:
            if text:
                for start in range(0, len(text), _SEND_KEYS_LITERAL_CHARS_PER_CALL):
                    await self._tmux(
                        "send-keys",
                        "-l",
                        "-t",
                        self.tmux_target,
                        text[start : start + _SEND_KEYS_LITERAL_CHARS_PER_CALL],
                    )

            if keys:
                if text:
                    await asyncio.sleep(0.05)
                for key in keys.split():
                    await self._tmux("send-keys", "-t", self.tmux_target, key)
        except RuntimeError:
            self.running = False
            return {
                "error": (
                    f"Terminal {self.name}:{self.session_key} is no longer "
                    "running (tmux server exited)"
                )
            }

        return {"status": "sent"}

    async def read(self, scrollback: int = 0) -> TerminalResult:
        """Capture the terminal screen."""
        if not self.running:
            return {"error": "Terminal is not running"}

        args = ["capture-pane", "-t", self.tmux_target, "-p"]
        if scrollback > 0:
            args.extend(["-S", f"-{scrollback}"])

        try:
            result = await self._tmux_output(*args)
        except RuntimeError:
            self.running = False
            return {
                "error": (
                    f"Terminal {self.name}:{self.session_key} is no longer "
                    "running (tmux server exited)"
                )
            }

        self._remember_pane_snapshot(result)
        return {
            "terminal": f"{self.name}:{self.session_key}",
            "screen": _strip_ansi(result),
            "scrollback_lines": scrollback,
        }

    def _bootstrap_egress_proxy(
        self,
        sandbox: SandboxPolicy,
        env: dict[str, str],
    ) -> SandboxPolicy:
        """Start the parent-side L7 egress proxy for this terminal.

        Wires the proxy lifecycle into the terminal so close()
        tears it down. Mutates ``env`` to inject ``HTTP_PROXY`` /
        ``HTTPS_PROXY`` / CA env vars and returns an updated
        :class:`SandboxPolicy` whose ``egress_relay_port`` /
        ``egress_socket_path`` are populated for the launcher.
        The caller must use the returned policy for
        ``create_exec_launcher`` (and ideally ``wrap_launcher_argv``
        too); the old policy lacks the relay handshake info.

        Idempotent against repeated calls: every call creates a
        new proxy and replaces ``self._egress_handle`` /
        ``self._egress_tmpdir``. ``close()`` is the only sanctioned
        teardown.

        :param sandbox: Active sandbox policy (caller has verified
            ``sandbox.active``).
        :param env: Mutable env dict that will be passed to the
            tmux subprocess.
        :returns: Updated policy with relay info baked in and the
            scratch tmpdir added to ``write_roots`` so bwrap
            bind-mounts it inside the namespace.
        """
        assert self.egress_rules, "caller checked self.egress_rules"
        self._egress_tmpdir = create_private_tmpdir()
        # Add the scratch tmpdir to write_roots BEFORE encoding the
        # policy into the launcher. Without this, bwrap won't bind
        # the tmpdir into the namespace and the CA bundle /
        # egress socket the launcher needs at activate time would
        # be invisible.
        sandbox = with_additional_write_roots(sandbox, [self._egress_tmpdir])
        self._egress_handle = start_egress_proxy(
            rules=self.egress_rules,
            tmpdir=self._egress_tmpdir,
            allow_private_destinations=self.egress_allow_private_destinations,
            # Terminal path uses ``require_auth=False``: tmux closes
            # inherited FDs before exec, so we have no out-of-band
            # channel for a Proxy-Authorization token. Embedding the
            # token in HTTP_PROXY (the alternative) would leak it via
            # ``ps -E`` on every shell child anyway. The relay's
            # other defenses (random ephemeral port, default-deny on
            # private destinations, allow-list per :attr:`egress_rules`)
            # still apply; see the controller's docstring for the
            # full trade-off discussion.
            require_auth=False,
        )
        apply_egress_env(
            env,
            relay_port=self._egress_handle.relay_port,
            ca_bundle_path=self._egress_handle.ca_bundle_path,
            auth_token=None,
        )
        return replace(
            sandbox,
            egress_relay_port=self._egress_handle.relay_port,
            egress_socket_path=str(self._egress_handle.socket_path),
        )

    async def close(self) -> None:
        """Kill the tmux session and clean up."""
        # Cancel both idle-watcher variants first so they don't race
        # the socket teardown. Order doesn't matter — they're
        # independent.
        await self._stop_idle_watcher()
        self._stop_idle_watcher_thread()

        if self.running:
            with contextlib.suppress(RuntimeError):
                await self._tmux("kill-server")
            self.running = False

        if self.os_env is not None:
            self.os_env.close()

        # Stop the egress proxy + clean up its scratch tmpdir.
        # Order: stop first so the proxy isn't listening on a
        # socket inside a soon-to-be-deleted dir, then remove the
        # tmpdir.
        if self._egress_handle is not None:
            try:
                self._egress_handle.stop()
            except Exception:
                logger.exception(
                    "egress proxy stop failed for terminal %s:%s",
                    self.name,
                    self.session_key,
                )
            self._egress_handle = None
        if self._egress_tmpdir is not None:
            cleanup_private_tmpdir(self._egress_tmpdir)
            self._egress_tmpdir = None

        # Clean up the private dir (contains socket + fork).
        if self.private_dir.exists():
            shutil.rmtree(self.private_dir, ignore_errors=True)

    def start_idle_watcher(
        self,
        on_idle: Callable[[], None | Awaitable[None]],
        *,
        on_exit: Callable[[], None | Awaitable[None]] | None = None,
    ) -> None:
        """Start a background task that fires ``on_idle`` each time the pane
        becomes quiet (no change for ``_IDLE_THRESHOLD_SECONDS``).

        Edge-triggered: the callback fires once per idle transition.  It will
        fire again only after new output changes the pane and then stops again.
        The watcher is cancelled by ``close()``.
        """
        if not self.running:
            raise RuntimeError("Cannot start idle watcher before launch")
        if self._idle_task is not None and not self._idle_task.done():
            return
        self._idle_task = asyncio.create_task(self._idle_watch_loop(on_idle, on_exit=on_exit))

    async def _stop_idle_watcher(self) -> None:
        task = self._idle_task
        if task is None:
            return
        self._idle_task = None
        if task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    async def _idle_watch_loop(
        self,
        on_idle: Callable[[], None | Awaitable[None]],
        *,
        on_exit: Callable[[], None | Awaitable[None]] | None = None,
    ) -> None:
        """
        Asyncio polling loop driving an :class:`_IdleDetector`.

        :param on_idle: Edge-triggered callback. May be sync or
            async; awaited if it returns a coroutine. Exceptions
            inside the callback log + stop the watcher.
        """
        detector = _IdleDetector()

        async def _fire(callback: Callable[[], None | Awaitable[None]], kind: str) -> bool:
            """Invoke a callback. Returns False if it raised and the watcher should exit."""
            try:
                result = callback()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.exception(
                    "%s-notification callback failed for terminal %s:%s",
                    kind,
                    self.name,
                    self.session_key,
                )
                return False
            return True

        while self.running:
            await asyncio.sleep(_IDLE_POLL_INTERVAL_SECONDS)
            if not self.running:
                return
            try:
                snapshot = await self._tmux_output(
                    "capture-pane",
                    "-t",
                    self.tmux_target,
                    "-p",
                    "-e",
                )
            except RuntimeError:
                # tmux server likely gone.
                self.running = False
                if on_exit is not None:
                    await _fire(on_exit, "exit")
                return

            self._remember_pane_snapshot(snapshot)
            if detector.tick(snapshot) and not await _fire(on_idle, "idle"):
                return

    def start_idle_watcher_thread(
        self,
        on_idle: Callable[[], None] | None = None,
        *,
        on_activity: Callable[[], None] | None = None,
        on_exit: Callable[[], None] | None = None,
        idle_threshold_s: float | None = None,
        poll_interval_s: float | None = None,
        replace: bool = False,
    ) -> None:
        """
        Start a daemon thread driving idle/activity edges from the pane.

        Thread-based sibling of :meth:`start_idle_watcher` for callers
        without a long-lived event loop. The Omnigent ``sys_terminal_launch``
        path runs ``SysTerminalLaunchTool.invoke`` on a worker thread
        and drives :meth:`launch` via ``asyncio.run`` per call — that
        loop exits the moment ``launch`` returns, so an asyncio task
        started inside it dies. A daemon thread polling tmux via
        ``subprocess.run`` survives across launch / send / read tool
        calls and stops on :meth:`close` (or when the host process
        exits, since it's a daemon).

        Edge-triggered: ``on_idle`` fires once per idle transition (re-
        arms only after new output mutates the pane); ``on_activity``
        fires on every poll tick where the pane content changed — so at
        most once per *poll_interval_s*, which for the fast claude-native
        watcher (200ms) is up to ~5/sec while a pane redraws continuously.
        ``on_exit`` fires once when the tmux session disappears unexpectedly.
        Any further rate-limiting of activity (e.g. the runner's
        one-pulse-per-second ``session.terminal.activity`` throttle) is
        the caller's responsibility, not this watcher's. At least one
        callback should be provided; passing several is fine.

        :param on_idle: Optional sync callback invoked once per idle
            edge, or ``None`` to skip idle detection. Must not block the
            polling thread for long — invoked synchronously between
            snapshots.
        :param on_activity: Optional sync callback invoked on each tick
            the pane changed (the runner-determined "PTY had output"
            signal). Same non-blocking contract as *on_idle*.
        :param on_exit: Optional sync callback invoked when the watcher
            observes that tmux has disappeared. Same non-blocking contract
            as *on_idle*.
        :param idle_threshold_s: Per-watcher diff-track idle threshold in
            seconds passed to :class:`_IdleDetector`, e.g. ``1.0`` for the
            claude-native status watcher. ``None`` uses the module
            default :data:`_IDLE_THRESHOLD_SECONDS`.
        :param poll_interval_s: Per-watcher poll interval in seconds, e.g.
            ``0.2`` for the claude-native status watcher (snappier
            running/idle transitions). ``None`` uses the module default
            :data:`_IDLE_POLL_INTERVAL_SECONDS`.
        :param replace: When ``True``, replace any existing threaded watcher
            so callbacks can be rebound after terminal ownership transfer.
        :raises RuntimeError: When the instance is not currently
            running (caller forgot to ``await launch`` first).
        """
        if not self.running:
            raise RuntimeError("Cannot start idle watcher before launch")
        if on_idle is None and on_activity is None and on_exit is None:
            raise ValueError(
                "start_idle_watcher_thread requires at least one of "
                "on_idle / on_activity / on_exit — a watcher with none would poll "
                "tmux forever with no effect."
            )
        if self._idle_thread is not None and self._idle_thread.is_alive():
            if not replace:
                return
            self._stop_idle_watcher_thread()
        stop_event = threading.Event()
        self._idle_stop_event = stop_event
        self._idle_thread = threading.Thread(
            target=self._idle_watch_loop_threaded,
            args=(stop_event,),
            kwargs={
                "on_idle": on_idle,
                "on_activity": on_activity,
                "on_exit": on_exit,
                "idle_threshold_s": idle_threshold_s,
                "poll_interval_s": poll_interval_s,
            },
            name=f"terminal-idle-{self.name}-{self.session_key}",
            daemon=True,
        )
        self._idle_thread.start()

    def _idle_watch_loop_threaded(
        self,
        stop_event: threading.Event,
        *,
        on_idle: Callable[[], None] | None = None,
        on_activity: Callable[[], None] | None = None,
        on_exit: Callable[[], None] | None = None,
        idle_threshold_s: float | None = None,
        poll_interval_s: float | None = None,
    ) -> None:
        """
        Sync polling loop driving an :class:`_IdleDetector`.

        Runs on the daemon thread spawned by
        :meth:`start_idle_watcher_thread`. Stops cleanly when
        ``stop_event`` is set or when ``self.running`` flips to
        ``False`` (close path), and exits silently if ``tmux
        capture-pane`` fails (server likely gone).

        :param stop_event: Event the close path sets to signal
            shutdown. Doubles as the poll-interval sleep via
            :meth:`Event.wait` so the join window is bounded by
            one poll interval, not the full sleep.
        :param on_idle: Optional idle-edge callback (see
            :meth:`start_idle_watcher_thread`); skipped when ``None``.
        :param on_activity: Optional pane-changed callback; fired each
            tick the pane content changed. Skipped when ``None``.
        :param on_exit: Optional callback fired when tmux disappears.
            Skipped when ``None``.
        :param idle_threshold_s: Per-watcher diff-track idle threshold in
            seconds forwarded to :class:`_IdleDetector`, e.g. ``1.0``.
            ``None`` uses the module default.
        :param poll_interval_s: Seconds between polls, e.g. ``0.2`` for the
            claude-native status watcher. ``None`` uses the module default
            :data:`_IDLE_POLL_INTERVAL_SECONDS`.
        """
        detector = _IdleDetector(idle_threshold_s=idle_threshold_s)
        interval = poll_interval_s if poll_interval_s is not None else _IDLE_POLL_INTERVAL_SECONDS
        while self.running:
            # ``Event.wait`` doubles as the poll-interval sleep, so
            # ``stop_event.set()`` from :meth:`close` returns within
            # one tick instead of waiting out the full interval.
            if stop_event.wait(interval):
                return
            if not self.running:
                return
            snapshot = self._capture_pane_for_idle_or_none()
            if snapshot is None:
                self.running = False
                if on_exit is not None:
                    self._fire_watch_callback(on_exit, "exit")
                return
            self._remember_pane_snapshot(snapshot)
            # A pane change that lands within the recent-interaction window
            # is a client-driven repaint (attach/detach reflow, focus,
            # mouse, keystroke — stamped via note_client_interaction), not
            # agent output, so the detector discounts it.
            suppress = (
                time.monotonic() - self._last_client_interaction_at
            ) < _CLIENT_INTERACTION_WINDOW_SECONDS
            idle_fired = detector.tick(snapshot, suppress_activity=suppress)
            # Activity edge first: a tick can both change the pane and
            # (much later) cross the idle threshold, but never both in
            # the same tick — a change resets the idle timer.
            if (
                on_activity is not None
                and detector.changed_this_tick
                and not self._fire_watch_callback(on_activity, "activity")
            ):
                return
            if (
                idle_fired
                and on_idle is not None
                and not self._fire_watch_callback(on_idle, "idle")
            ):
                return

    def _capture_pane_for_idle_or_none(self) -> str | None:
        """
        Capture the pane for an idle tick, or signal "tmux gone".

        :returns: Pane bytes from ``tmux capture-pane -p -e``, or
            ``None`` when the tmux subprocess raised — the
            threaded loop reads ``None`` as "stop watching, the
            server is no longer there".
        """
        try:
            return self._tmux_output_sync("capture-pane", "-t", self.tmux_target, "-p", "-e")
        except RuntimeError:
            return None

    def _fire_watch_callback(self, callback: Callable[[], None], kind: str) -> bool:
        """
        Invoke a watcher edge callback, swallow + log on failure.

        :param callback: The user-supplied edge callback (idle or
            activity).
        :param kind: Label for logging, e.g. ``"idle"`` or
            ``"activity"``.
        :returns: ``True`` when the callback returned cleanly so
            the watcher continues; ``False`` when the callback
            raised (logged) so the watcher exits per the
            threaded-loop contract.
        """
        try:
            callback()
        except Exception:
            logger.exception(
                "%s-notification callback failed for terminal %s:%s",
                kind,
                self.name,
                self.session_key,
            )
            return False
        return True

    def _stop_idle_watcher_thread(self) -> None:
        """
        Signal the threaded watcher to stop and join with a timeout.

        Symmetrical to :meth:`_stop_idle_watcher` for the asyncio
        variant. Bounded by :data:`_IDLE_WATCHER_JOIN_TIMEOUT_S` so
        a wedged ``subprocess.run`` (rare — the only one in the loop
        body) doesn't block the close path indefinitely. After the
        timeout the thread keeps running, but it's a daemon — it
        will exit when the process does, and the next iteration's
        ``self.running`` check will short-circuit it anyway.
        """
        thread = self._idle_thread
        stop_event = self._idle_stop_event
        if thread is None:
            return
        self._idle_thread = None
        self._idle_stop_event = None
        if stop_event is not None:
            stop_event.set()
        if thread.is_alive():
            thread.join(timeout=_IDLE_WATCHER_JOIN_TIMEOUT_S)

    async def is_alive(self) -> bool:
        """
        Check if the tmux session is still running.

        When tmux reports the session gone, or the probe cannot start,
        this method marks ``self.running`` false. That side effect is
        intentional: subsequent pollers use the in-memory flag as a
        fast path instead of forking ``tmux has-session`` repeatedly
        after the server has exited.

        :returns: ``True`` when ``tmux has-session`` confirms the
            session is alive; otherwise ``False``.
        """
        if not self.running:
            return False
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._tmux_base_cmd(),
                "has-session",
                "-t",
                self.tmux_target,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.communicate()
            if proc.returncode != 0:
                self.running = False
                return False
            return True
        except OSError:
            self.running = False
            return False

    async def _tmux(self, *args: str) -> None:
        """Run a tmux command against this instance's server."""
        proc = await asyncio.create_subprocess_exec(
            *self._tmux_base_cmd(),
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"tmux command failed: {' '.join(args)}: {stderr.decode().strip()}")

    async def _tmux_output(self, *args: str) -> str:
        """Run a tmux command and return stdout."""
        proc = await asyncio.create_subprocess_exec(
            *self._tmux_base_cmd(),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"tmux command failed: {' '.join(args)}: {stderr.decode().strip()}")
        return stdout.decode()

    def _tmux_output_sync(self, *args: str) -> str:
        """
        Synchronous sibling of :meth:`_tmux_output`.

        Used by :meth:`_idle_watch_loop_threaded` because that
        watcher runs on a daemon thread without an event loop.
        Same error semantics as the async version: non-zero exit
        codes raise :class:`RuntimeError` carrying the stderr.

        :param args: Args to pass after ``tmux -S <socket>``,
            e.g. ``("capture-pane", "-t", "main", "-p", "-e")``.
        :returns: The captured stdout, decoded as UTF-8.
        :raises RuntimeError: When the tmux subprocess exits
            non-zero (typically because the server has gone away).
        """
        proc = subprocess.run([*self._tmux_base_cmd(), *args], capture_output=True, check=False)
        if proc.returncode != 0:
            raise RuntimeError(
                f"tmux command failed: {' '.join(args)}: {proc.stderr.decode().strip()}"
            )
        return proc.stdout.decode()


def _shell_quote(s: str) -> str:
    """Quote a string for shell use."""
    if not s:
        return "''"
    # Simple quoting for common cases.
    if re.match(r"^[a-zA-Z0-9_./:@=-]+$", s):
        return s
    return "'" + s.replace("'", "'\\''") + "'"


@dataclass(frozen=True)
class TerminalCreateResult:
    """
    Result of :func:`create_terminal_instance`.

    :param instance: The freshly-constructed :class:`TerminalInstance`.
        Not yet launched — the caller is responsible for calling
        :meth:`TerminalInstance.launch` with the ``cwd`` below.
    :param cwd: The resolved working directory the caller should pass to
        :meth:`TerminalInstance.launch`. This is either the forked copy
        of the source tree (when ``spec.os_env.fork`` is true) or the
        original cwd resolved to an absolute path.
    """

    instance: TerminalInstance
    cwd: Path


def create_terminal_instance(
    name: str,
    session_key: str,
    spec: TerminalEnvSpec,
    *,
    parent_os_env_spec: OSEnvSpec | None = None,
    cwd_override: str | None = None,
    sandbox_override: str | None = None,
    conversation_link: str | None = None,
) -> TerminalCreateResult:
    """Create a terminal instance from a spec.

    Creates a private directory for the instance, optionally forks the
    filesystem, and prepares the tmux socket path.

    If the terminal spec has no ``os_env``, the parent's ``os_env`` is
    inherited (same cwd, same sandbox, no fork).  Terminals always have
    an ``os_env`` so their filesystems can be mounted.

    :param name: Logical terminal name from the agent spec (e.g. ``"bash"``).
    :param session_key: Per-session identifier used to scope tmux
        sockets and private directories, e.g. ``"s1"``.
    :param spec: The :class:`TerminalEnvSpec` describing the command,
        args, env, scrollback, and optional os_env for this terminal.
    :param parent_os_env_spec: The parent session's os_env spec, used
        when the terminal spec itself has no ``os_env`` and should
        inherit from the parent.
    :param cwd_override: Optional override for the terminal's starting
        working directory. When provided, takes precedence over the
        spec's cwd.
    :param sandbox_override: Optional override for the sandbox type,
        one of ``"none"`` or ``"linux_bwrap"``.
    :param conversation_link: Optional web UI link for the owning
        conversation, e.g. ``"/c/conv_abc123"``.
    :returns: A :class:`TerminalCreateResult` carrying the new instance
        and the resolved cwd to pass to ``launch()``.
    """
    if not _tmux_available():
        raise RuntimeError("tmux is not installed or not on PATH")

    # Create the instance's private directory.
    private_dir = Path(tempfile.mkdtemp(prefix=_TERMINAL_DIR_PREFIX))
    socket_path = private_dir / "tmux.sock"
    # Record the owning process so a later startup can reap this tmux
    # server if we die without graceful shutdown (SIGKILL, harness
    # teardown) — see ``reap_orphaned_terminals``.
    (private_dir / _OWNER_PID_FILENAME).write_text(str(os.getpid()), encoding="utf-8")

    # Resolve os_env spec.  If none specified, inherit from parent.
    effective_os_env_spec = build_terminal_os_env_spec(
        spec,
        parent_os_env_spec=parent_os_env_spec,
        cwd_override=cwd_override,
        sandbox_override=sandbox_override,
    )

    os_env: OSEnvironment | None = None
    cwd: Path

    if effective_os_env_spec.fork:
        # Copy the directory tree for fork isolation.
        src_cwd = Path(effective_os_env_spec.cwd or os.getcwd()).resolve()
        fork_root = private_dir / "root"
        _copy_tree(src_cwd, fork_root)
        cwd = fork_root

        # Create an os_env pointing at the fork for mount support.
        # Use ``replace`` so any future OSEnvSpec field (e.g.
        # ``start_in_scratch``) is preserved without a code change here.
        forked_spec = replace(
            effective_os_env_spec,
            cwd=str(fork_root),
            fork=False,  # already forked
        )
        os_env = create_os_environment(forked_spec)
    else:
        cwd = Path(effective_os_env_spec.cwd or os.getcwd()).resolve()
        os_env = create_os_environment(effective_os_env_spec)

    # Resolve sandbox policy for the terminal process.
    sandbox: SandboxPolicy | None = None
    egress_rules: list[str] | None = None
    egress_allow_private: bool = False
    if effective_os_env_spec.sandbox is not None:
        sandbox_spec = effective_os_env_spec.sandbox
        if sandbox_spec.type != "none":
            sandbox = resolve_sandbox(effective_os_env_spec, cwd)
            if sandbox.active:
                # Add the private dir to write roots so a forked working
                # tree (``private_dir/root``) and the instance dir stay
                # writable inside the pane.
                sandbox = with_additional_write_roots(sandbox, [private_dir])
                # The tmux control socket lives inside that
                # now-writable ``private_dir``. Deny the sandboxed pane
                # from reaching it so it cannot ``tmux -S <sock> run-shell``
                # against the unsandboxed server. bwrap overlays /dev/null
                # onto the socket path; seatbelt emits a network-outbound
                # unix-socket deny (its default allow_network=true would
                # otherwise permit the connect).
                sandbox = with_denied_unix_sockets(sandbox, [socket_path])
        # Plumb the egress allow-list from the OSEnvSandboxSpec
        # onto the instance so :meth:`launch` can start a
        # parent-side MITM proxy. SandboxPolicy itself only carries
        # the *resolved* relay handshake fields, not the rule list
        # — see the policy docstring for the rationale (rules live
        # on the spec; resolved state on the policy).
        if sandbox_spec.egress_rules:
            egress_rules = list(sandbox_spec.egress_rules)
            egress_allow_private = bool(sandbox_spec.egress_allow_private_destinations)

    instance = TerminalInstance(
        name=name,
        session_key=session_key,
        socket_path=socket_path,
        private_dir=private_dir,
        os_env=os_env,
        command=spec.command or "bash",
        args=list(spec.args),
        env=dict(spec.env),
        env_unset=list(spec.env_unset),
        inherit_env=spec.inherit_env,
        sandbox_policy=sandbox,
        conversation_link=conversation_link,
        egress_rules=egress_rules,
        egress_allow_private_destinations=egress_allow_private,
        scrollback=spec.scrollback,
        tmux_allow_passthrough=spec.tmux_allow_passthrough,
        tmux_start_on_attach=spec.tmux_start_on_attach,
    )

    return TerminalCreateResult(instance=instance, cwd=cwd)
