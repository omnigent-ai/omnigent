"""Bridge utilities for the native Claude Code wrapper.

The native wrapper has two live processes that need to rendezvous:

- Claude Code, running in the user's terminal resource.
- The Omnigent harness turn, running when the web UI submits a
  message to the session agent.

This module owns the small filesystem rendezvous directory plus two
helper surfaces:

- An MCP stdio server (``serve-mcp`` subcommand) that Claude Code
  launches as a child process. It advertises Omnigent tools to
  Claude (workspace ``sys_os_*`` tools outside an active turn,
  active-turn Omnigent tools via a per-turn relay).
- A tmux send-keys path. Web UI messages are delivered to Claude by
  typing them into the same tmux pane the user is attached to;
  Claude treats them as ordinary user input. The runner advertises
  the pane's socket + target in ``tmux.json`` after launching the
  ``claude/main`` terminal.

Claude's experimental Channels MCP capability was the original input
path but is blocked at the org policy layer, so this bridge does not
use it.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import queue
import re
import secrets
import shlex
import stat
import sys
import tempfile
import threading
import time
import urllib.parse
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib import error, request

from omnigent.claude_native_message_display_hook import MESSAGE_DELTAS_FILE

if TYPE_CHECKING:
    from omnigent.llms.context_window import ModelPricing

from omnigent.inner.bundle_skills import claude_native_skill_args
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.os_env import OSEnvironment, create_os_environment
from omnigent.reasoning_effort import CLAUDE_EFFORTS
from omnigent.tools.base import Tool, ToolContext
from omnigent.tools.builtins.os_env import build_os_env_tools

BRIDGE_DIR_ENV_VAR = "HARNESS_CLAUDE_NATIVE_BRIDGE_DIR"
REQUEST_SESSION_ID_ENV_VAR = "HARNESS_CLAUDE_NATIVE_REQUEST_SESSION_ID"
BRIDGE_ID_LABEL_KEY = "omnigent.claude_native.bridge_id"

# Root for the per-process Claude bridge tree. Namespaced by uid so
# other Unix users on the same host cannot read the bearer token or
# pre-create the parent as a symlink to redirect the bridge tree. The
# trusted parent (`/tmp`) is shared; everything under
# `_BRIDGE_ROOT_PARENT` must be owned by the current uid and not be a
# symlink — see :func:`_ensure_secure_dir`.
_TRUSTED_PARENT = Path("/tmp")
_BRIDGE_ROOT_PARENT = _TRUSTED_PARENT / f"omnigent-{os.getuid()}"
_BRIDGE_ROOT = _BRIDGE_ROOT_PARENT / "claude-native"
_CONFIG_FILE = "bridge.json"
_SERVER_FILE = "server.json"
_STATE_FILE = "state.json"
_HOOKS_FILE = "hooks.jsonl"
_RECENT_LOCAL_COMMAND_LINE_LIMIT = 200
_RECENT_LOCAL_COMMAND_WINDOW_S = 10.0
_FORKED_FROM_LINE_LIMIT = 200
_TOOL_RELAY_FILE = "tool_relay.json"
_TMUX_FILE = "tmux.json"
_PERMISSION_HOOK_FILE = "permission_hook.json"
_CONTEXT_FILE = "context.json"
_USER_CLAUDE_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
_MCP_SERVER_NAME = "omnigent"
_MCP_PROTOCOL_VERSION = "2024-11-05"
# Tools-changed: harness POSTs to the bridge MCP server's localhost
# control endpoint, which emits ``notifications/tools/list_changed``
# on its MCP stdout. Standard MCP notification — unrelated to the
# experimental Claude Channels feature that this module no longer
# uses.
_TOOLS_CHANGED_READY_TIMEOUT_S = 30.0
_TOOLS_CHANGED_POST_TIMEOUT_S = 10.0
# Ceiling the relay HTTP handler (``_run_relay_tool``) waits for a single
# tool dispatch to complete on the harness event loop.
_TOOL_CALL_TIMEOUT_S = 300.0
# Timeout for the bridge's POST to the active-turn relay server
# (``_call_relay_tool``). This is the OUTER hop: it waits for the relay
# handler's entire ``_TOOL_CALL_TIMEOUT_S`` dispatch, which itself fans out
# to the Omnigent policy server and back. It MUST exceed ``_TOOL_CALL_TIMEOUT_S``
# so the inner handler times out first and returns a clean MCP error over
# HTTP 200 — rather than the outer ``urlopen`` raising and tearing down the
# stdio MCP server (see ``_stdio_jsonrpc_loop``). The previous flat 10s sat
# below the real round-trip latency under load, so slow-but-healthy calls
# (session history reads, shell) tripped it and crashed the bridge.
_TOOL_RELAY_POST_TIMEOUT_S = _TOOL_CALL_TIMEOUT_S + 30.0
# Web-UI → Claude input now flows through tmux send-keys, not
# Claude's experimental Channels MCP capability. The runner writes
# ``tmux.json`` after the Claude terminal launches; the harness
# tails it and shells out to tmux.
_TMUX_READY_TIMEOUT_S = 30.0
_TMUX_SEND_TIMEOUT_S = 5.0
# Claude Code renders this prompt glyph in its input box once the TUI
# is interactive. We poll ``capture-pane`` for it before injecting the
# first message so keystrokes typed during Claude's boot aren't dropped.
# The glyph persists while Claude is busy responding, so its presence
# means "input box mounted" (not "idle"), which is what injection needs.
_CLAUDE_PROMPT_GLYPH = "❯"
# How many trailing non-empty lines to scan for the prompt glyph. The
# input box sits near the bottom of the pane; scanning only the tail
# avoids false positives from the glyph appearing in scrollback output.
# The window has to clear the footer rendered below the box — some
# people's statuslines run ~3 lines — so the ``❯`` row isn't the last
# non-empty line.
_PROMPT_SCAN_TAIL_LINES = 5
_CLAUDE_READY_POLL_INTERVAL_S = 0.15
_PASTE_SETTLE_S = 0.1  # let the TUI commit a paste before the separate submit Enter
# How long to wait for the pasted draft to visibly land in Claude's
# input box before sending the submit Enter. Claude Code coalesces
# rapid stdin bursts into a paste, so an Enter sent while the TUI is
# still consuming the paste gets folded in as a newline instead of
# submitting — the draft then sits unsent. Polling for the draft makes
# the handoff deterministic where the old fixed sleep raced it.
_PASTE_COMMIT_TIMEOUT_S = 5.0
# After the submit Enter, how long to keep checking that the draft
# actually left the input box (re-sending Enter while it hasn't)
# before failing loud.
_SUBMIT_VERIFY_TIMEOUT_S = 10.0
# Minimum spacing between repeated submit Enters during verification.
# Long enough for the TUI to clear the box after a successful submit
# (so a slow-but-successful first Enter isn't double-tapped), short
# enough that a swallowed Enter is retried promptly.
_SUBMIT_RETRY_INTERVAL_S = 1.0
# Claude Code collapses large pastes into this placeholder in the
# input box instead of rendering the text itself.
_PASTED_PLACEHOLDER_PREFIX = "[Pasted text"
# How many characters of the draft's first line to use when checking
# whether the draft is rendered in the input box. Short enough to fit
# on the prompt row of a default 80-column detached pane.
_DRAFT_NEEDLE_MAX_CHARS = 24
# When Claude Code's input prompt never renders (it failed to boot), the
# readiness gate attaches the tail of the captured pane to its error so
# the real cause — often Claude Code's own startup crash, e.g. a
# ``JSON Parse error`` from an HTML page served to its API client —
# surfaces in the web UI error banner instead of only in the terminal.
_TERMINAL_FAILURE_TAIL_LINES = 12
_TERMINAL_FAILURE_TAIL_CHARS = 800

ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[Any]]


def _absolute_syntactic_path(path: Path) -> Path:
    """
    Return an absolute path without following symlinks.

    Security validation needs to inspect symlinked ancestors with
    ``lstat``. ``Path.resolve`` would follow an existing symlink before
    that inspection, so this helper only expands ``~`` and normalizes
    ``.`` / ``..`` components.

    :param path: Path to normalize, e.g. ``Path("~/.omnigent/x")``.
    :returns: Absolute path with syntactic normalization applied.
    """
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def _trusted_parent_for_bridge_dir(target: Path) -> Path:
    """
    Return the trusted parent for an allowed bridge directory.

    Claude-native files live below the uid-scoped temp bridge root.
    Codex- and Cursor-native reuse the relay/MCP implementation but keep bridge
    files below their own bridge roots. All roots use the same
    owner-only ancestor validation; only the trusted anchor differs.

    :param target: Normalized bridge directory path being created or validated,
        e.g. ``Path("/tmp/omnigent-501/claude-native/abc")``.
    :returns: Absolute parent at which ancestor validation stops, e.g.
        ``Path("/tmp")``.
    :raises RuntimeError: If ``target`` is not below a known bridge root.
    """
    claude_root = _absolute_syntactic_path(_BRIDGE_ROOT)
    if target.is_relative_to(claude_root):
        return _absolute_syntactic_path(_TRUSTED_PARENT)

    from omnigent.codex_native_bridge import bridge_root

    codex_root = _absolute_syntactic_path(bridge_root())
    if target.is_relative_to(codex_root):
        # In production, trust $HOME and validate/chmod the two bridge-owned
        # directories below it: .omnigent and codex-native. In tests, the
        # monkeypatched root may not use that shape, so trust the direct parent.
        trusted_parent = codex_root.parent
        if codex_root.name == "codex-native" and codex_root.parent.name == ".omnigent":
            trusted_parent = codex_root.parent.parent
        return _absolute_syntactic_path(trusted_parent)

    from omnigent.cursor_native_bridge import bridge_root as cursor_bridge_root

    cursor_root = _absolute_syntactic_path(cursor_bridge_root())
    if target.is_relative_to(cursor_root):
        return _absolute_syntactic_path(cursor_root.parent.parent)

    from omnigent.antigravity_native_bridge import bridge_root as antigravity_bridge_root

    # antigravity-native keeps its bridge files below ``~/.omnigent/antigravity-native``,
    # the same ``$HOME/.omnigent/<harness>-native`` shape codex uses, so apply the
    # identical anchor logic: in production trust ``$HOME`` and validate/chmod the
    # two bridge-owned dirs below it (``.omnigent`` and ``antigravity-native``); in
    # tests the monkeypatched root may differ, so trust the direct parent.
    antigravity_root = _absolute_syntactic_path(antigravity_bridge_root())
    if target.is_relative_to(antigravity_root):
        trusted_parent = antigravity_root.parent
        if (
            antigravity_root.name == "antigravity-native"
            and antigravity_root.parent.name == ".omnigent"
        ):
            trusted_parent = antigravity_root.parent.parent
        return _absolute_syntactic_path(trusted_parent)

    raise RuntimeError(
        f"bridge dir {target!s} is not under an allowed bridge root "
        f"({claude_root!s}, {codex_root!s}, {cursor_root!s}, {antigravity_root!s})"
    )


@dataclass(frozen=True)
class ClaudeTranscriptItem:
    """
    One Omnigent conversation item parsed from Claude's JSONL log.

    :param source_id: Stable idempotency key derived from the Claude
        transcript record UUID and content block position, e.g.
        ``"747e:0:function_call"``.
    :param item_type: Omnigent conversation item type, e.g.
        ``"message"`` or ``"function_call"``.
    :param data: Item payload shaped like ``SessionEventInput.data``.
    :param response_id: Synthetic response id used to group the
        Claude turn in AP/web UI rendering.
    """

    source_id: str
    item_type: str
    data: dict[str, Any]
    response_id: str


@dataclass(frozen=True)
class TranscriptReadResult:
    """
    Result of reading Claude transcript JSONL records.

    :param line_cursor: Count of complete newline-terminated records
        consumed from the transcript, e.g. ``12``.
    :param byte_offset: Byte offset immediately after the last
        complete record consumed, e.g. ``4096``. A partial trailing
        line is not included.
    :param current_response_id: Response id for a Claude assistant
        turn that remains active across polls.
    :param items: Parsed Omnigent conversation items from the
        complete records after the caller's cursor.
    :param latest_usage: Token-usage from the most recent assistant
        entry with a ``message.usage`` block. Keys: ``context_tokens``,
        ``input_tokens``, ``output_tokens``. ``None`` when no such
        entry was scanned.
    :param latest_model: ``message.model`` from the most recent
        assistant entry, or ``None``.
    """

    line_cursor: int
    byte_offset: int
    current_response_id: str | None
    items: list[ClaudeTranscriptItem]
    latest_usage: dict[str, int] | None = None
    latest_model: str | None = None


@dataclass(frozen=True)
class ClaudeHookRecord:
    """
    One complete hook JSONL record read from ``hooks.jsonl``.

    :param event_cursor: Count of complete hook records consumed
        through this record, e.g. ``3``.
    :param byte_offset: Byte offset immediately after this complete
        record, e.g. ``512``.
    :param recorded_at: Unix timestamp for when the hook was recorded,
        e.g. ``1779922393.222``. ``None`` when the envelope did not
        carry a numeric timestamp.
    :param event_name: Claude hook event name, e.g. ``"Stop"``.
        ``None`` means the line was complete but malformed or did not
        contain a usable event name; the durable cursor may still
        advance past it.
    :param source: Claude ``SessionStart`` source, e.g. ``"clear"``,
        or ``None`` for hook records without a source field.
    :param claude_session_id: Claude-native session uuid from the hook
        payload, e.g. ``"a1b2c3d4-1234-5678-9abc-def012345678"``,
        or ``None`` when absent.
    :param transcript_path: Claude transcript path from the hook
        payload, e.g. ``"/home/user/.claude/projects/x/session.jsonl"``,
        or ``None`` when absent.
    :param previous_claude_session_id: Claude session id that was
        active immediately before this hook, e.g.
        ``"a1b2c3d4-1234-5678-9abc-def012345678"``, or ``None``
        when the hook did not capture one.
    :param claude_session_was_seen: Whether the incoming Claude
        session id had already been observed before this hook was
        recorded. ``None`` means the hook did not capture that
        context.
    :param clear_rotated_to: Omnigent session id created synchronously by the
        hook for ``SessionStart source=clear``, e.g. ``"conv_new"``,
        or ``None`` when the background forwarder should rotate.
    :param fork_detected: Whether the hook identified this record as a
        Claude branch/fork transition before recording it. The
        background forwarder uses this annotation because state.json
        already points at the new Claude session by the time it reads
        hooks.jsonl.
    :param fork_rotated_to: Omnigent session id created synchronously by the
        hook for a Claude branch/fork transition, e.g. ``"conv_fork"``,
        or ``None`` when the background forwarder should fork.
    :param todos: Updated todo list from a ``PostToolUse``/``TodoWrite``
        hook event, e.g.
        ``[{"content": "Write tests", "status": "in_progress",
        "activeForm": "Writing tests"}]``. ``None`` for all other events.
    :param task_id: Native task id from a ``TaskCreated``,
        ``TaskCompleted``, or ``PostToolUse``/``TaskUpdate`` hook event,
        e.g. ``"1"``. ``None`` for all other events.
    :param task_subject: Human-readable task subject from a
        ``TaskCreated`` hook event, e.g. ``"Create folder 'abc'"``.
        ``None`` for all other events.
    :param task_status: Task status from a ``TaskCreated`` (``"pending"``),
        ``TaskCompleted`` (``"completed"``), or
        ``PostToolUse``/``TaskUpdate`` event (``"in_progress"`` or
        ``"completed"``). ``None`` for all other events.
    """

    event_cursor: int
    byte_offset: int
    event_name: str | None
    recorded_at: float | None = None
    source: str | None = None
    claude_session_id: str | None = None
    transcript_path: Path | None = None
    previous_claude_session_id: str | None = None
    claude_session_was_seen: bool | None = None
    clear_rotated_to: str | None = None
    fork_detected: bool = False
    fork_rotated_to: str | None = None
    todos: list[dict[str, Any]] | None = None
    task_id: str | None = None
    task_subject: str | None = None
    task_status: str | None = None


@dataclass(frozen=True)
class HookReadResult:
    """
    Result of reading Claude hook JSONL records.

    :param event_cursor: Count of complete hook records consumed.
    :param byte_offset: Byte offset immediately after the last
        complete hook record consumed. A partial trailing line is not
        included.
    :param records: Complete hook records after the caller's cursor.
    """

    event_cursor: int
    byte_offset: int
    records: list[ClaudeHookRecord]


@dataclass(frozen=True)
class _JsonlRecord:
    """
    One complete newline-terminated JSONL record.

    :param line_number: One-based line number relative to the reader's
        line cursor, e.g. ``5``.
    :param byte_offset: Byte offset where the record starts.
    :param next_byte_offset: Byte offset immediately after the
        newline-terminated record.
    :param text: UTF-8 decoded JSONL text including the trailing
        newline, or ``None`` when the complete record was not valid
        UTF-8 and should advance cursors without being parsed.
    """

    line_number: int
    byte_offset: int
    next_byte_offset: int
    text: str | None


@dataclass(frozen=True)
class _JsonlReadResult:
    """
    Complete-record read result for an append-only JSONL file.

    :param line_cursor: Count of complete records consumed.
    :param byte_offset: Byte offset after the last complete record.
    :param records: Complete records read after the requested byte
        offset.
    """

    line_cursor: int
    byte_offset: int
    records: list[_JsonlRecord]


@dataclass(frozen=True)
class ClaudeMessageDelta:
    """
    One streamed assistant-text chunk recorded by the MessageDisplay hook.

    Written to ``<bridge_dir>/message_deltas.jsonl`` by
    :mod:`omnigent.claude_native_message_display_hook` and read back by
    the transcript forwarder to publish ``response.output_text.delta``
    events.

    :param message_id: Claude's stable per-assistant-message id, e.g.
        ``"2ca51d97-2f0f-493a-aed7-85a5b56c5747"``. Used by the web UI
        to scope the in-flight buffer for one message; it does NOT
        appear in the transcript JSONL, so the final item is correlated
        positionally rather than by this id.
    :param index: 0-based chunk order within the message, e.g. ``3``.
    :param final: ``True`` on the last chunk of the message.
    :param delta: Incremental text for this chunk, e.g.
        ``"Pour in the wine"``. Disjoint from other chunks' text.
    """

    message_id: str
    index: int
    final: bool
    delta: str


@dataclass(frozen=True)
class MessageDeltaReadResult:
    """
    Complete-record read result for the message-deltas JSONL file.

    :param byte_offset: Byte offset after the last complete record, to
        be persisted and passed to the next read so tailing resumes
        without re-reading.
    :param deltas: Parsed deltas appended after the requested offset,
        in file (append) order.
    """

    byte_offset: int
    deltas: list[ClaudeMessageDelta]


def read_message_deltas_from_offset(
    bridge_dir: Path,
    byte_offset: int,
) -> MessageDeltaReadResult:
    """
    Read assistant-text deltas appended after a byte offset.

    Only complete newline-terminated records are returned; a partial
    trailing record leaves the byte offset unchanged so the next poll
    retries it once the hook finishes the append. Records that fail to
    parse into a well-formed :class:`ClaudeMessageDelta` are skipped (the
    byte offset still advances past them) — a malformed line must not
    wedge the tail.

    :param bridge_dir: Bridge directory path.
    :param byte_offset: Byte offset already consumed, e.g. ``2048``.
    :returns: Parsed deltas plus the updated byte offset.
    """
    read_result = _read_complete_jsonl_records(
        bridge_dir / MESSAGE_DELTAS_FILE,
        byte_offset=byte_offset,
        start_line=0,
    )
    deltas: list[ClaudeMessageDelta] = []
    for record in read_result.records:
        delta = _message_delta_from_jsonl_text(record.text)
        if delta is not None:
            deltas.append(delta)
    return MessageDeltaReadResult(
        byte_offset=read_result.byte_offset,
        deltas=deltas,
    )


def _message_delta_from_jsonl_text(text: str | None) -> ClaudeMessageDelta | None:
    """
    Parse one deltas-file line into a :class:`ClaudeMessageDelta`.

    :param text: Raw JSONL line text, or ``None`` when the record bytes
        were not valid UTF-8.
    :returns: Parsed delta, or ``None`` when the line is malformed or
        lacks the required ``message_id``/``delta`` fields.
    """
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    message_id = payload.get("message_id")
    delta = payload.get("delta")
    index = payload.get("index")
    if not isinstance(message_id, str) or not message_id:
        return None
    if not isinstance(delta, str):
        return None
    # ``bool`` is an ``int`` subclass — exclude it so a stray ``true``
    # index is rejected rather than silently coerced to 0/1.
    if not isinstance(index, int) or isinstance(index, bool):
        return None
    return ClaudeMessageDelta(
        message_id=message_id,
        index=index,
        final=bool(payload.get("final")),
        delta=delta,
    )


class ClaudeNativeToolRelay:
    """
    HTTP relay for Claude MCP tool calls, scoped to its caller's lifetime.

    Claude's MCP helper process calls the relay synchronously when Claude
    Code invokes a relayed Omnigent tool; the relay forwards the call
    into the ``tool_executor`` callback supplied at start, which dispatches
    it on the runner event loop (e.g. through the Omnigent REST API).

    Callers choose the lifetime and call :meth:`close` when it ends. The
    comment-tool relay (``list_comments`` / ``update_comment``) is
    session-scoped — started when the Claude terminal launches and closed
    on session delete — whereas a per-turn caller would start and close it
    within one turn.

    :param bridge_dir: Bridge directory containing
        ``tool_relay.json``, e.g. ``/tmp/omnigent/claude-native/x``.
    :param httpd: Started localhost HTTP server for tool calls. Its bound
        address identifies this relay's advertisement on close.
    """

    def __init__(self, *, bridge_dir: Path, httpd: ThreadingHTTPServer) -> None:
        """
        Initialize the relay handle.

        :param bridge_dir: Bridge directory containing the relay
            advertisement, e.g. ``Path("/tmp/omnigent/...")``.
        :param httpd: Started localhost HTTP server for tool calls.
        :returns: None.
        """
        self._bridge_dir = bridge_dir
        self._httpd = httpd

    def close(self) -> None:
        """
        Stop the relay's HTTP server and remove its advertisement file.

        Only unlinks ``tool_relay.json`` when it still advertises *this*
        relay (its ``url`` matches this server's bound address). Sessions
        that fork/clear/resume keep the same ``bridge_id`` — hence the same
        bridge dir and relay file — so a newer session's relay may have
        overwritten the file with its own address. Unlinking unconditionally
        would delete the still-active session's advertisement and make its
        comment tools vanish. The HTTP server is always shut down (it is this
        relay's own socket).

        :returns: None.
        """
        relay_file = self._bridge_dir / _TOOL_RELAY_FILE
        host, port = self._httpd.server_address
        # A newer relay that overwrote the file advertises a different url
        # (this relay's socket is still bound, so its port is unique), so the
        # file is left for that relay to own.
        if _read_json_file(relay_file).get("url") == f"http://{host}:{port}":
            with contextlib.suppress(FileNotFoundError):
                relay_file.unlink()
        self._httpd.shutdown()
        self._httpd.server_close()


def _ensure_secure_dir(target: Path) -> None:
    """
    Create or validate ``target`` as an owner-only directory chain.

    ``Path.mkdir(mode=0o700, parents=True, exist_ok=True)`` only applies
    the mode to the leaf and silently trusts any pre-existing ancestor.
    On a shared host, an attacker could pre-create
    ``/tmp/omnigent-<UID>`` (Claude-native), ``~/.omnigent``
    (Codex-native), or a deeper ancestor as a symlink — or as a 0o777
    directory — and redirect the bridge tree (which stores bearer
    tokens in JSON files).

    This helper resolves the trusted parent for ``target`` and walks
    each ancestor from that trusted parent down to ``target``,
    creating new ones with mode 0o700 and rejecting any existing
    ancestor that is a symlink, not a directory, owned by a different
    uid, or has group/other permission bits set. Wrong-but-repairable
    modes on dirs we own are reset to 0o700.

    :param target: Final bridge directory path to ensure, e.g.
        ``Path("/tmp/omnigent-501/claude-native/abc")``.
    :raises RuntimeError: If validation fails for any ancestor.
    """
    target = _absolute_syntactic_path(target)
    trusted_parent = _trusted_parent_for_bridge_dir(target)
    ancestors: list[Path] = []
    cur = target
    while cur != trusted_parent and cur != cur.parent:
        ancestors.append(cur)
        cur = cur.parent
    if cur != trusted_parent:
        raise RuntimeError(f"bridge dir {target!s} is not under trusted parent {trusted_parent!s}")
    ancestors.reverse()
    my_uid = os.getuid()
    for ancestor in ancestors:
        try:
            os.mkdir(ancestor, mode=0o700)
            continue
        except FileExistsError:
            pass
        st = os.lstat(ancestor)
        if stat.S_ISLNK(st.st_mode):
            raise RuntimeError(f"refusing to use bridge ancestor {ancestor!s}: is a symlink")
        if not stat.S_ISDIR(st.st_mode):
            raise RuntimeError(f"refusing to use bridge ancestor {ancestor!s}: not a directory")
        if st.st_uid != my_uid:
            raise RuntimeError(
                f"refusing to use bridge ancestor {ancestor!s}: owned by uid "
                f"{st.st_uid}, not current user ({my_uid})"
            )
        if (st.st_mode & 0o077) != 0:
            os.chmod(ancestor, 0o700)


def bridge_dir_for_bridge_id(bridge_id: str) -> Path:
    """
    Return the deterministic bridge directory for a Claude-native bridge.

    :param bridge_id: Opaque bridge id, e.g. ``"bridge_abc123"``.
    :returns: Absolute bridge directory under
        ``/tmp/omnigent-<UID>/claude-native``.
    """
    digest = hashlib.sha256(bridge_id.encode("utf-8")).hexdigest()[:32]
    return _BRIDGE_ROOT / digest


def bridge_dir_for_conversation_id(conversation_id: str) -> Path:
    """
    Return the bridge directory for a legacy session id.

    :param conversation_id: Omnigent conversation id used as bridge id, e.g.
        ``"conv_abc123"``.
    :returns: Absolute bridge directory under
        ``/tmp/omnigent-<UID>/claude-native``.
    """
    return bridge_dir_for_bridge_id(conversation_id)


def build_claude_native_spawn_env(
    conversation_id: str,
    *,
    bridge_id: str | None = None,
) -> dict[str, str]:
    """
    Build spawn env for the ``claude-native`` harness process.

    :param conversation_id: Omnigent conversation id, e.g.
        ``"conv_abc123"``.
    :param bridge_id: Opaque bridge id from
        :data:`BRIDGE_ID_LABEL_KEY`, e.g. ``"bridge_abc123"``. ``None``
        normalizes old sessions by using *conversation_id*.
    :returns: Environment variables needed by
        :class:`ClaudeNativeExecutor`.
    """
    resolved_bridge_id = bridge_id or conversation_id
    return {
        BRIDGE_DIR_ENV_VAR: str(bridge_dir_for_bridge_id(resolved_bridge_id)),
        REQUEST_SESSION_ID_ENV_VAR: conversation_id,
    }


def prepare_bridge_dir(
    conversation_id: str,
    *,
    bridge_id: str | None = None,
    workspace: Path,
    launch_model: str | None = None,
) -> Path:
    """
    Create or refresh the bridge directory for a native Claude session.

    :param conversation_id: Omnigent conversation id, e.g.
        ``"conv_abc123"``.
    :param bridge_id: Opaque bridge id, e.g. ``"bridge_abc123"``.
        ``None`` normalizes old sessions by using *conversation_id*.
    :param workspace: Runner workspace/cwd used for local OS tools.
    :param launch_model: Gateway model name that Claude was launched
        with, e.g. ``"databricks-claude-opus-4-7"``.  Persisted so the
        forwarder can re-inject it when Claude Code's ``/model``
        normalizes the name to one the gateway rejects.  ``None`` when
        no ucode profile is active.
    :returns: Bridge directory path.
    """
    resolved_bridge_id = bridge_id or conversation_id
    bridge_dir = bridge_dir_for_bridge_id(resolved_bridge_id)
    _ensure_secure_dir(bridge_dir)
    config = _read_json_file(bridge_dir / _CONFIG_FILE)
    token = config.get("token") if isinstance(config, dict) else None
    if not isinstance(token, str) or not token:
        token = secrets.token_urlsafe(32)
    payload: dict[str, object] = {
        "bridge_id": resolved_bridge_id,
        "active_session_id": conversation_id,
        "conversation_id": conversation_id,
        "workspace": str(workspace),
        "token": token,
        "updated_at": time.time(),
    }
    if launch_model is not None:
        payload["launch_model"] = launch_model
    _write_json_file(bridge_dir / _CONFIG_FILE, payload)
    # Keep ``_PERMISSION_HOOK_FILE`` — the PermissionRequest command hook
    # reads the Omnigent server URL from it at runtime, so wiping it on re-prep
    # breaks approval routing on reattach/rebind. ``build_hook_settings``
    # rewrites it on cold launch.
    for filename in (
        _SERVER_FILE,
        _STATE_FILE,
        _HOOKS_FILE,
        _TOOL_RELAY_FILE,
        _TMUX_FILE,
    ):
        with contextlib.suppress(FileNotFoundError):
            (bridge_dir / filename).unlink()
    return bridge_dir


def ensure_claude_workspace_trusted(workspace: Path) -> None:
    """
    Pre-accept Claude Code's first-run trust + onboarding prompts.

    Claude Code blocks on two TUI prompts the first time it launches in
    a new context: a global onboarding flow (theme / login) gated by the
    top-level ``hasCompletedOnboarding`` key in ``~/.claude.json``, and a
    per-directory "Do you trust the files in this folder?" dialog gated
    by ``projects["<abs cwd>"].hasTrustDialogAccepted``. Neither fires a
    ``PermissionRequest`` hook, so on a host-spawned (web-UI-driven)
    session there is nobody at the terminal to answer them: Claude hangs
    and the web UI shows nothing. This is acute with
    per-session git worktrees, which hand Claude a brand-new —
    therefore untrusted — directory on every session.

    Seed both gating keys idempotently so the launch never blocks. Only
    those two keys are written; all other ``~/.claude.json`` state (the
    user's own onboarding choices, project history, MCP config, OAuth
    account) is preserved, and the file is left untouched when both keys
    are already set. This deliberately does NOT skip per-tool permission
    prompts — those still route to the web UI via the ``PermissionRequest``
    hook; only the unhookable startup gates are pre-accepted.

    Concurrency: this is a read-modify-write of a file Claude itself also
    rewrites. It runs once, before the terminal is launched (so Claude is
    not yet writing for this session), and uses an atomic replace. Two
    runners starting on the same host within the same instant could still
    race on last-writer-wins; the only consequence is that one session may
    re-show the trust prompt, which a relaunch clears. Matching this to
    Claude's own lock-free writes keeps the helper simple.

    :param workspace: The runner workspace Claude will launch in, e.g.
        ``Path("/home/user/repo-worktrees/feature-x")``. Resolved to an
        absolute path to match Claude's ``projects`` key convention.
    :returns: None.
    :raises ValueError: If an existing ``~/.claude.json`` (or its
        ``projects`` map / target project entry) is not a JSON object.
        Surfaced rather than silently overwritten so a corrupt or
        unexpected user config is never clobbered (fail loud).
    :raises json.JSONDecodeError: If an existing ``~/.claude.json`` is
        not valid JSON, for the same reason.
    """
    config_path = Path.home() / ".claude.json"
    if config_path.exists():
        data = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"{config_path} is not a JSON object; refusing to overwrite.")
    else:
        data = {}

    changed = False
    # Global onboarding gate (theme / login). Absent on a machine that
    # has never run Claude Code interactively.
    if data.get("hasCompletedOnboarding") is not True:
        data["hasCompletedOnboarding"] = True
        changed = True

    # Per-directory trust gate. Claude keys its ``projects`` map by the
    # resolved absolute path, so match that exactly.
    project_key = str(workspace.resolve())
    projects = data.setdefault("projects", {})
    if not isinstance(projects, dict):
        raise ValueError(f"{config_path} 'projects' is not a JSON object; refusing to overwrite.")
    project = projects.setdefault(project_key, {})
    if not isinstance(project, dict):
        raise ValueError(
            f"{config_path} projects[{project_key!r}] is not a JSON object; refusing to overwrite."
        )
    if project.get("hasTrustDialogAccepted") is not True:
        project["hasTrustDialogAccepted"] = True
        changed = True

    if not changed:
        return
    _atomic_write_user_json(config_path, data)


def _atomic_write_user_json(path: Path, payload: dict[str, Any]) -> None:
    """
    Atomically rewrite a user-owned JSON config file in place.

    Unlike :func:`_write_json_file` (which targets the owner-only bridge
    tree under ``/tmp`` and enforces secure-directory ownership on the
    parent), this writes the user's own ``~/.claude.json`` in their home
    directory: it must not re-permission the home directory, but it does
    pin the result to owner-only ``0o600`` because the file holds the
    Claude OAuth account block.

    :param path: Destination file, e.g. ``Path("~/.claude.json")``.
    :param payload: JSON-serializable config object to write. Rendered
        with two-space indentation to match Claude's own formatting and
        keep diffs readable.
    :returns: None.
    :raises OSError: If the temp file cannot be written, ``chmod``-ed,
        or atomically replaced into place — e.g. the home directory is
        read-only or the filesystem does not support ``os.replace``.
    """
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
            json.dump(payload, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path is not None:
            with contextlib.suppress(FileNotFoundError):
                tmp_path.unlink()


def read_active_session_id(bridge_dir: Path) -> str | None:
    """
    Read the Omnigent session currently receiving bridge-originated events.

    :param bridge_dir: Bridge directory path.
    :returns: Active Omnigent session id, e.g. ``"conv_abc123"``, or
        ``None`` when the bridge config is absent or malformed.
    """
    config = _read_json_file(bridge_dir / _CONFIG_FILE)
    if not isinstance(config, dict):
        return None
    active = config.get("active_session_id")
    if isinstance(active, str) and active:
        return active
    legacy = config.get("conversation_id")
    return legacy if isinstance(legacy, str) and legacy else None


def read_launch_model(bridge_dir: Path) -> str | None:
    """
    Read the gateway model name that Claude was launched with.

    :param bridge_dir: Bridge directory path.
    :returns: Gateway model name, e.g.
        ``"databricks-claude-opus-4-7"``, or ``None`` when no ucode
        profile was active at launch time.
    """
    config = _read_json_file(bridge_dir / _CONFIG_FILE)
    if not isinstance(config, dict):
        return None
    model = config.get("launch_model")
    return model if isinstance(model, str) and model else None


def read_bridge_id(bridge_dir: Path) -> str | None:
    """
    Read the opaque bridge id from bridge config.

    :param bridge_dir: Bridge directory path.
    :returns: Opaque bridge id, e.g. ``"bridge_abc123"``, or
        ``None`` when the bridge config is absent or malformed.
    """
    config = _read_json_file(bridge_dir / _CONFIG_FILE)
    if not isinstance(config, dict):
        return None
    bridge_id = config.get("bridge_id")
    return bridge_id if isinstance(bridge_id, str) and bridge_id else None


def write_active_session_id(bridge_dir: Path, session_id: str) -> None:
    """
    Atomically update the bridge's active Omnigent session.

    :param bridge_dir: Bridge directory path.
    :param session_id: New active Omnigent session id, e.g.
        ``"conv_abc123"``.
    :returns: None.
    :raises RuntimeError: If the bridge config does not exist.
    """
    config = _read_json_file(bridge_dir / _CONFIG_FILE)
    if not config:
        raise RuntimeError(f"bridge config missing: {bridge_dir / _CONFIG_FILE}")
    config["active_session_id"] = session_id
    config["conversation_id"] = session_id
    config["updated_at"] = time.time()
    _write_json_file(bridge_dir / _CONFIG_FILE, config)


def read_permission_hook_config(bridge_dir: Path) -> dict[str, Any]:
    """
    Read Omnigent routing details for the permission command hook.

    :param bridge_dir: Bridge directory path.
    :returns: Permission hook config, e.g.
        ``{"ap_server_url": "http://127.0.0.1:8787",
        "ap_auth_headers": {"Authorization": "Bearer token"}}``.
        Empty dict when the file is absent or malformed.
    """
    payload = _read_json_file(bridge_dir / _PERMISSION_HOOK_FILE)
    return payload if isinstance(payload, dict) else {}


def build_mcp_config(bridge_dir: Path, *, python_executable: str | None = None) -> dict[str, Any]:
    """
    Build the Claude Code MCP config for the Omnigent bridge server.

    :param bridge_dir: Bridge directory path.
    :param python_executable: Python executable to run, e.g.
        ``"/path/to/.venv/bin/python"``. ``None`` uses
        :data:`sys.executable`.
    :returns: JSON-serializable Claude MCP config.
    """
    python = python_executable or sys.executable
    return {
        "mcpServers": {
            _MCP_SERVER_NAME: {
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
                },
            }
        }
    }


def build_hook_settings(
    bridge_dir: Path,
    *,
    python_executable: str | None = None,
    ap_server_url: str | None = None,
    ap_auth_headers: dict[str, str] | None = None,
    api_key_helper: str | None = None,
) -> dict[str, Any]:
    """
    Build invocation-local Claude Code hook settings.

    :param bridge_dir: Bridge directory path.
    :param python_executable: Python executable to run, e.g.
        ``"/path/to/.venv/bin/python"``. ``None`` uses
        :data:`sys.executable`.
    :param ap_server_url: Omnigent server base URL the ``PermissionRequest``
        command hook should POST to, e.g. ``"http://127.0.0.1:8787"``.
        When ``None``, no ``PermissionRequest`` hook is registered and
        Claude falls back to its built-in TUI permission prompt.
    :param ap_auth_headers: Headers to send with the
        ``PermissionRequest`` command hook, e.g.
        ``{"Authorization": "Bearer <token>"}``. Stored in the
        owner-only bridge directory instead of in hook argv.
    :param api_key_helper: Optional Claude Code ``apiKeyHelper``
        command from ucode state, e.g. ``"databricks auth token
        --host https://example.databricks.com ..."``.
    :returns: JSON-serializable Claude settings fragment.
    """
    python = python_executable or sys.executable
    # -I (isolated mode) prevents Python from adding the session's
    # working directory to sys.path, which would shadow the installed
    # omnigent package with a local checkout in the cwd (e.g. a
    # git worktree that has its own omnigent/ directory on a
    # different branch).
    command_parts = [
        python,
        "-I",
        "-m",
        "omnigent.claude_native_hook",
        "--bridge-dir",
        str(bridge_dir),
    ]
    command = shlex.join(command_parts)
    hook = {"type": "command", "command": command}
    session_start_hook = {
        "type": "command",
        "command": command,
    }
    # ``MessageDisplay`` fires once per streamed assistant-text chunk and
    # Claude blocks on the hook, so it gets a dedicated stdlib-only
    # appender module instead of the heavier observer ``hook`` above —
    # the per-chunk subprocess must stay cheap. It just appends the
    # chunk to ``<bridge_dir>/message_deltas.jsonl``; the forwarder tails
    # that file and publishes ``response.output_text.delta`` events.
    message_display_command_parts = [
        python,
        "-I",
        "-m",
        "omnigent.claude_native_message_display_hook",
        "--bridge-dir",
        str(bridge_dir),
    ]
    message_display_hook = {
        "type": "command",
        "command": shlex.join(message_display_command_parts),
    }
    hooks: dict[str, Any] = {
        "SessionStart": [{"hooks": [session_start_hook]}],
        "Stop": [{"hooks": [hook]}],
        "StopFailure": [{"hooks": [hook]}],
        # ``UserPromptSubmit`` is the symmetric counterpart to
        # ``Stop`` — fires when a new user prompt reaches Claude
        # (web-UI message via tmux send-keys, or direct keystrokes
        # into the embedded terminal). The transcript forwarder
        # translates it into ``session.status: running``.
        "UserPromptSubmit": [{"hooks": [hook]}],
        # ``TaskCreated`` fires when Claude creates a new native task
        # (shown with ``□`` in the TUI). The payload carries ``task_id``
        # and ``task_subject``; the forwarder converts all current tasks
        # into a ``session.todos`` SSE event so the web UI can display
        # the task checklist.
        "TaskCreated": [{"hooks": [hook]}],
        # ``TaskCompleted`` fires when Claude marks a native task done
        # (``■`` in the TUI). The payload carries ``task_id`` so the
        # forwarder can flip that task's status to ``"completed"``.
        "TaskCompleted": [{"hooks": [hook]}],
        # ``PostToolUse`` filtered to ``TodoWrite`` fires whenever Claude
        # updates its simple todo list. The hook payload carries the new
        # todos under ``tool_input.todos``.
        # ``PostToolUse`` filtered to ``TaskUpdate`` fires when Claude
        # calls ``TaskUpdate`` to change a native task's status (e.g.
        # to ``"in_progress"``). The payload carries ``tool_input.taskId``
        # and ``tool_input.status``.
        "PostToolUse": [
            {"matcher": "TodoWrite", "hooks": [hook]},
            {"matcher": "TaskUpdate", "hooks": [hook]},
        ],
        # ``PreCompact`` fires right before Claude compacts its own
        # context — for both a manual ``/compact`` (web-UI button or
        # typed) and an automatic context-overflow compaction. The
        # forwarder translates it into a
        # ``response.compaction.in_progress`` SSE so the web UI shows
        # its "Compacting conversation…" spinner while Claude runs the
        # real compaction in the terminal. The matching completion
        # signal is ``SessionStart`` with ``source == "compact"`` (no
        # dedicated PreCompact-done hook exists), already wired above.
        "PreCompact": [{"hooks": [hook]}],
        # ``MessageDisplay`` fires per streamed assistant-text chunk.
        # Routed to the dedicated fast appender so the forwarder can
        # publish live token deltas to the web UI.
        "MessageDisplay": [{"hooks": [message_display_hook]}],
    }
    if ap_server_url:
        _write_json_file(
            bridge_dir / _PERMISSION_HOOK_FILE,
            {
                "ap_server_url": ap_server_url,
                "ap_auth_headers": ap_auth_headers or {},
                "updated_at": time.time(),
            },
        )
        # ``PermissionRequest`` fires only when Claude is about to
        # show its TUI permission prompt — that's exactly the
        # interception point we want for routing to the web UI.
        # Route through a command hook instead of baking a session id
        # into an HTTP URL at Claude launch. The subprocess reads the
        # current active session from bridge.json for every permission
        # request, so approvals follow `/clear` rotations without
        # restarting Claude.
        permission_command_parts = [
            python,
            "-I",
            "-m",
            "omnigent.claude_native_hook",
            "permission-request",
            "--bridge-dir",
            str(bridge_dir),
        ]
        permission_hook: dict[str, Any] = {
            "type": "command",
            "command": shlex.join(permission_command_parts),
            # Wait up to a day for the verdict. Claude Code's default
            # command-hook timeout (~60s) would otherwise kill the hook
            # subprocess long before the user answers, putting the
            # prompt back in the TUI and flipping the web card to
            # "Resolved elsewhere". A day is effectively wait-forever
            # for an interactive permission prompt; it stays in lockstep
            # with the subprocess/AP-side budgets so none caps first.
            "timeout": 86400,
        }
        hooks["PermissionRequest"] = [{"hooks": [permission_hook]}]

        # Policy-gate native Claude Code tools, not just relay/MCP tools.
        evaluate_policy_command_parts = [
            python,
            "-I",
            "-m",
            "omnigent.claude_native_hook",
            "evaluate-policy",
            "--bridge-dir",
            str(bridge_dir),
        ]
        evaluate_policy_hook: dict[str, Any] = {
            "type": "command",
            "command": shlex.join(evaluate_policy_command_parts),
        }
        # In bypassPermissions mode PermissionRequest never fires, so
        # AskUserQuestion needs its own PreToolUse hook to surface the
        # form. It's a no-op in other modes to avoid double-surfacing.
        ask_uq_command_parts = [
            python,
            "-I",
            "-m",
            "omnigent.claude_native_hook",
            "ask-user-question",
            "--bridge-dir",
            str(bridge_dir),
        ]
        ask_uq_hook: dict[str, Any] = {
            "type": "command",
            "command": shlex.join(ask_uq_command_parts),
            # Short timeout: if the web-UI elicitation isn't answered
            # within 10s, the hook returns empty output so Claude falls
            # through to its TUI picker in bypassPermissions mode. In
            # default mode this hook exits immediately (no-op), so the
            # timeout is irrelevant there.
            "timeout": 10,
        }
        # The ``AskUserQuestion`` matcher only fires if that tool is actually
        # callable. A session launched with ``--disallowedTools AskUserQuestion``
        # (e.g. the exit-plan-mode e2e fixture) can never trigger this hook, so
        # the registration is dormant there — harmless, just never reached.
        hooks["PreToolUse"] = [
            {"matcher": "AskUserQuestion", "hooks": [ask_uq_hook]},
            {"hooks": [evaluate_policy_hook]},
        ]
        # PostToolUse already has TodoWrite and TaskUpdate matchers
        # for the transcript forwarder (the observer ``hook``). Append
        # a catch-all policy evaluation entry so TOOL_RESULT policies
        # fire for all tools, not just the forwarder-specific ones.
        hooks["PostToolUse"].append({"hooks": [evaluate_policy_hook]})
        # UserPromptSubmit already carries the transcript forwarder's
        # status hook (running). Append the policy hook so REQUEST-phase
        # policies gate native prompts — for native sessions this is the
        # sole request gate (the server-level ``_evaluate_input_policy``
        # skips native message events). A DENY emits ``decision: "block"``,
        # dropping the prompt before the model sees it; ASK is resolved
        # server-side. Covers both web-UI-injected and direct-terminal
        # prompts, since both fire UserPromptSubmit.
        hooks["UserPromptSubmit"].append({"hooks": [evaluate_policy_hook]})
    settings: dict[str, Any] = {"hooks": hooks}
    if api_key_helper:
        settings["apiKeyHelper"] = api_key_helper
    # Override Claude Code's statusLine so we receive its stdin (the
    # only place ``context_window`` surfaces). Chain to whatever the
    # user had globally so claude-hud / their bar still renders.
    status_parts = [
        python,
        "-I",
        "-m",
        "omnigent.claude_native_status",
        "--bridge-dir",
        str(bridge_dir),
    ]
    chain_command = read_user_status_line_command()
    if chain_command is not None:
        status_parts.extend(["--chain", chain_command])
    settings["statusLine"] = {"type": "command", "command": shlex.join(status_parts)}
    return settings


def url_component(value: str) -> str:
    """
    Percent-encode one URL path component.

    :param value: Raw path component, e.g. ``"conv_abc123"``.
    :returns: URL-safe component with slashes escaped.
    """
    return urllib.parse.quote(value, safe="")


# Built-in Claude Code tools that need their own custom UI to be
# usable from the web chat. Until we ship that UI, disable them so
# Claude falls back to plain assistant text + a normal user reply,
# which already round-trips through the existing chat-input pipeline.
#
# Currently empty: ``AskUserQuestion`` routes through a dedicated
# ``PreToolUse`` hook (registered in ``build_hook_settings``) that
# surfaces the question + options to the web UI as an elicitation
# form and injects the user's answer via ``updatedInput``, and
# ``ExitPlanMode`` surfaces through the standard ``PermissionRequest``
# hook as an approve/reject elicitation card.
_OMNIGENT_DISALLOWED_TOOLS: tuple[str, ...] = ()


def augment_claude_args(
    claude_args: tuple[str, ...],
    *,
    bridge_dir: Path,
    python_executable: str | None = None,
    ap_server_url: str | None = None,
    ap_auth_headers: dict[str, str] | None = None,
    api_key_helper: str | None = None,
    bundle_dir: Path | None = None,
    agent_name: str | None = None,
    skills_filter: str | list[str] = "all",
) -> list[str]:
    """
    Return Claude CLI args with Omnigent MCP/hook/skill injection.

    :param claude_args: User-provided Claude Code args, e.g.
        ``("--resume", "abc")``.
    :param bridge_dir: Bridge directory path.
    :param python_executable: Python executable to run helper
        modules. ``None`` uses :data:`sys.executable`.
    :param ap_server_url: Omnigent server base URL passed through to
        :func:`build_hook_settings` so the ``PermissionRequest``
        command hook is registered. ``None`` omits the hook and
        Claude falls back to its built-in TUI prompt.
    :param ap_auth_headers: Auth headers for the
        ``PermissionRequest`` command hook. Passed through to
        :func:`build_hook_settings`.
    :param api_key_helper: Optional Claude Code ``apiKeyHelper``
        command from ucode state, e.g. ``"databricks auth token
        --host https://example.databricks.com ..."``.
    :param bundle_dir: Materialized agent-bundle root, when the
        session's agent ships a ``skills/`` directory. Triggers
        ``--plugin-dir <bundle>`` so Claude Code discovers bundled
        skills natively — the CLI mirror of the SDK executor's plugin
        wiring. ``None`` (e.g. the ``omnigent claude`` CLI's minimal
        spec) adds no plugin args.
    :param agent_name: Agent display name for the bundle's plugin
        manifest, e.g. ``"researcher"``. ``None`` falls back to the
        bundle directory's basename.
    :param skills_filter: The agent spec's ``skills_filter`` (``"all"``
        / ``"none"`` / list of skill names), mapped to
        ``--setting-sources`` exactly as the SDK executor maps it onto
        ``setting_sources``. Defaults to ``"all"``.
    :returns: Augmented argument list for the terminal resource.
    """
    mcp_config = build_mcp_config(bridge_dir, python_executable=python_executable)
    hook_settings = build_hook_settings(
        bridge_dir,
        python_executable=python_executable,
        ap_server_url=ap_server_url,
        ap_auth_headers=ap_auth_headers,
        api_key_helper=api_key_helper,
    )
    args = _merge_disallowed_tools(list(claude_args), _OMNIGENT_DISALLOWED_TOOLS)
    args.extend(
        [
            "--mcp-config",
            json.dumps(mcp_config, separators=(",", ":")),
            "--settings",
            json.dumps(hook_settings, separators=(",", ":")),
        ]
    )
    args.extend(
        claude_native_skill_args(
            bundle_dir,
            agent_name=agent_name,
            skills_filter=skills_filter,
        )
    )
    return args


def _merge_disallowed_tools(args: list[str], extra: tuple[str, ...]) -> list[str]:
    """
    Add ``extra`` tool names to a ``--disallowedTools`` flag in ``args``.

    Merges into an existing flag if present (deduping while preserving
    order) so a user-supplied ``--disallowedTools`` is not silently
    overridden; otherwise appends a new flag.

    :param args: Claude CLI argument list to mutate-and-return.
    :param extra: Tool names Omnigent wants disabled.
    :returns: ``args`` with the merged flag.
    """
    if not extra:
        return args
    try:
        idx = args.index("--disallowedTools")
    except ValueError:
        args.extend(["--disallowedTools", ",".join(extra)])
        return args
    value_idx = idx + 1
    if value_idx >= len(args):
        return args
    existing = [t for t in args[value_idx].split(",") if t]
    args[value_idx] = ",".join(dict.fromkeys([*existing, *extra]))
    return args


def record_hook_event(bridge_dir: Path, payload: dict[str, Any]) -> None:
    """
    Record one Claude Code hook payload in the bridge directory.

    :param bridge_dir: Bridge directory path.
    :param payload: Hook JSON object read from Claude Code stdin,
        e.g. ``{"hook_event_name": "Stop", "transcript_path":
        "/home/user/.claude/projects/x/session.jsonl"}``.
    :returns: None.
    """
    _ensure_secure_dir(bridge_dir)
    envelope = {
        "recorded_at": time.time(),
        "payload": payload,
    }
    with (bridge_dir / _HOOKS_FILE).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(envelope, separators=(",", ":")) + "\n")

    state = _read_json_file(bridge_dir / _STATE_FILE)
    if not isinstance(state, dict):
        state = {}
    event_name = payload.get("hook_event_name")
    if isinstance(event_name, str) and event_name:
        state["last_hook_event_name"] = event_name
    transcript_path = payload.get("transcript_path")
    if isinstance(transcript_path, str) and transcript_path:
        state["transcript_path"] = transcript_path
    claude_session_id = payload.get("session_id")
    if isinstance(claude_session_id, str) and claude_session_id:
        state["claude_session_id"] = claude_session_id
        seen = read_seen_claude_session_ids(bridge_dir)
        seen.add(claude_session_id)
        state["seen_claude_session_ids"] = sorted(seen)
    state["updated_at"] = time.time()
    _write_json_file(bridge_dir / _STATE_FILE, state)


def read_transcript_path(bridge_dir: Path) -> Path | None:
    """
    Return the transcript path last reported by Claude hooks.

    :param bridge_dir: Bridge directory path.
    :returns: Transcript path, or ``None`` when hooks have not
        reported one yet.
    """
    state = _read_json_file(bridge_dir / _STATE_FILE)
    raw = state.get("transcript_path") if isinstance(state, dict) else None
    if not isinstance(raw, str) or not raw:
        return None
    return Path(raw)


def read_claude_session_id(bridge_dir: Path) -> str | None:
    """
    Return the Claude-native session id captured from hook events.

    Set by :func:`record_hook_event` whenever a hook payload carries
    a ``session_id`` field (every event Claude Code emits does).
    Wrapper code reads it back to mirror the value into AP-side
    conversation state (e.g. ``external_session_id`` on the
    ``conversations`` row) so ``--resume`` can recover the prior
    Claude transcript on a fresh runner without the user having to
    know Claude's own id.

    :param bridge_dir: Bridge directory path.
    :returns: Claude session uuid string,
        e.g. ``"a1b2c3d4-1234-5678-9abc-def012345678"``, or ``None``
        when no hook has yet reported one (the first poll after a
        cold launch).
    """
    state = _read_json_file(bridge_dir / _STATE_FILE)
    raw = state.get("claude_session_id") if isinstance(state, dict) else None
    if not isinstance(raw, str) or not raw:
        return None
    return raw


def read_seen_claude_session_ids(bridge_dir: Path) -> set[str]:
    """
    Return Claude session ids already observed by this bridge.

    The set is transient local bridge state. It lets the hook
    distinguish Claude-created branch/fork session switches from
    ordinary resumes into sessions the wrapper already saw.

    :param bridge_dir: Bridge directory path.
    :returns: Claude session uuid strings, e.g.
        ``{"a1b2c3d4-1234-5678-9abc-def012345678"}``.
    """
    state = _read_json_file(bridge_dir / _STATE_FILE)
    if not isinstance(state, dict):
        return set()
    seen: set[str] = set()
    raw_seen = state.get("seen_claude_session_ids")
    if isinstance(raw_seen, list):
        seen.update(value for value in raw_seen if isinstance(value, str) and value)
    raw_current = state.get("claude_session_id")
    if isinstance(raw_current, str) and raw_current:
        seen.add(raw_current)
    return seen


def count_transcript_lines(transcript_path: Path) -> int:
    """
    Count JSONL records currently present in a Claude transcript.

    :param transcript_path: Claude transcript path, e.g.
        ``"/home/user/.claude/projects/x/session.jsonl"``.
    :returns: Number of newline-delimited records. Missing files
        count as zero.
    """
    try:
        with transcript_path.open("r", encoding="utf-8") as handle:
            return sum(1 for _line in handle)
    except FileNotFoundError:
        return 0


def transcript_has_recent_local_command(
    transcript_path: Path,
    *,
    claude_session_id: str,
    recorded_at: float,
    command_names: frozenset[str],
) -> bool:
    """
    Return whether Claude recently recorded one local command.

    :param transcript_path: Claude transcript JSONL path, e.g.
        ``"/home/user/.claude/projects/x/session.jsonl"``.
    :param claude_session_id: Claude-native session uuid from the
        hook payload, e.g. ``"a1b2c3d4-1234-5678-9abc-def012345678"``.
    :param recorded_at: Unix timestamp for the hook record,
        e.g. ``1779922393.222``.
    :param command_names: Slash-command names to match, including
        the leading slash, e.g. ``frozenset({"/fork", "/branch"})``.
    :returns: ``True`` when a matching ``local_command`` transcript
        record exists near ``recorded_at`` for ``claude_session_id``.
    """
    try:
        lines = transcript_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return False
    for line in lines[-_RECENT_LOCAL_COMMAND_LINE_LIMIT:]:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        if record.get("sessionId") != claude_session_id:
            continue
        if record.get("subtype") != "local_command":
            continue
        timestamp = _transcript_timestamp(record.get("timestamp"))
        if timestamp is None or abs(timestamp - recorded_at) > _RECENT_LOCAL_COMMAND_WINDOW_S:
            continue
        content = record.get("content")
        if not isinstance(content, str):
            continue
        command_name = _local_command_name(content)
        if command_name in command_names:
            return True
    return False


def transcript_has_forked_from_marker(
    transcript_path: Path,
    *,
    claude_session_id: str,
    source_claude_session_id: str | None,
) -> bool:
    """
    Return whether Claude marked a transcript as a fork.

    Claude branch/fork transcripts carry structured ``forkedFrom``
    metadata on copied records. This is the stable non-title signal
    that a ``SessionStart source=resume`` event represents a new
    branch rather than an ordinary resume.

    :param transcript_path: Claude transcript JSONL path, e.g.
        ``"/home/user/.claude/projects/x/session.jsonl"``.
    :param claude_session_id: New Claude-native session uuid from the
        hook payload, e.g. ``"a1b2c3d4-1234-5678-9abc-def012345678"``.
    :param source_claude_session_id: Expected source Claude session
        uuid, e.g. ``"9abc..."``. ``None`` accepts any different
        non-empty source id.
    :returns: ``True`` when the transcript records a fork from the
        expected source session.
    """
    try:
        lines = transcript_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return False
    for line in _sample_transcript_edges(lines, _FORKED_FROM_LINE_LIMIT):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        if record.get("sessionId") != claude_session_id:
            continue
        forked_from = record.get("forkedFrom")
        if not isinstance(forked_from, dict):
            continue
        raw_source_session_id = forked_from.get("sessionId")
        if not isinstance(raw_source_session_id, str) or not raw_source_session_id:
            continue
        if raw_source_session_id == claude_session_id:
            continue
        if (
            source_claude_session_id is not None
            and raw_source_session_id != source_claude_session_id
        ):
            continue
        return True
    return False


def _sample_transcript_edges(lines: list[str], limit: int) -> list[str]:
    """
    Return transcript lines from the start and end of a file.

    :param lines: Transcript JSONL lines.
    :param limit: Maximum number of lines to take from each edge,
        e.g. ``200``.
    :returns: Sampled lines, preserving file order.
    """
    if limit <= 0 or len(lines) <= limit * 2:
        return lines
    return [*lines[:limit], *lines[-limit:]]


def _transcript_timestamp(value: object) -> float | None:
    """
    Parse a Claude transcript timestamp.

    :param value: Timestamp string, e.g.
        ``"2026-05-27T22:53:13.245Z"``.
    :returns: Unix timestamp, e.g. ``1779922393.245``, or ``None``
        when parsing fails.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _local_command_name(content: str) -> str | None:
    """
    Extract a Claude local command name from transcript content.

    :param content: Local-command transcript content, e.g.
        ``"<command-name>/fork</command-name>"``.
    :returns: Command name including leading slash, e.g.
        ``"/fork"``, or ``None`` when no command tag exists.
    """
    name_match = _COMMAND_NAME_RE.search(content)
    if name_match is None:
        return None
    name = name_match.group(1).strip()
    return name or None


def read_assistant_text_since(
    transcript_path: Path,
    start_line: int,
) -> tuple[int, list[str]]:
    """
    Read assistant text blocks appended after a transcript cursor.

    :param transcript_path: Claude transcript path, e.g.
        ``"/home/user/.claude/projects/x/session.jsonl"``.
    :param start_line: Zero-based line cursor captured before a
        message is injected into the Claude terminal.
    :returns: ``(new_cursor, text_chunks)``.
    """
    texts: list[str] = []
    cursor = 0
    try:
        with transcript_path.open("r", encoding="utf-8") as handle:
            for cursor, line in enumerate(handle, start=1):
                if cursor <= start_line:
                    continue
                text = _assistant_text_from_transcript_line(line)
                if text:
                    texts.append(text)
    except FileNotFoundError:
        return start_line, []
    return cursor, texts


def read_transcript_items_since(
    transcript_path: Path,
    start_line: int,
    *,
    agent_name: str,
    current_response_id: str | None = None,
) -> tuple[int, str | None, list[ClaudeTranscriptItem]]:
    """
    Read Claude transcript records as Omnigent conversation items.

    Claude Code writes append-only JSONL records whose ``message``
    payloads include user prompts, assistant text, native tool calls,
    and native tool results. This parser intentionally ignores
    metadata records (title, file-history, permission mode, system
    bookkeeping) and raw ``thinking`` blocks, while translating the
    user-visible semantic records into Omnigent item types the web UI
    already understands.

    :param transcript_path: Claude transcript path, e.g.
        ``"/home/user/.claude/projects/x/session.jsonl"``.
    :param start_line: One-based line cursor. Lines at or before
        this cursor are skipped.
    :param agent_name: Agent/model name stamped on assistant and
        tool-call items, e.g. ``"claude-native-ui"``.
    :param current_response_id: Response id for an in-progress
        Claude assistant turn from a previous poll.
    :returns: ``(new_cursor, current_response_id, items)``.
    """
    result = read_transcript_items_since_with_position(
        transcript_path,
        start_line,
        agent_name=agent_name,
        current_response_id=current_response_id,
    )
    return result.line_cursor, result.current_response_id, result.items


def read_transcript_items_since_with_position(
    transcript_path: Path,
    start_line: int,
    *,
    agent_name: str,
    current_response_id: str | None = None,
) -> TranscriptReadResult:
    """
    Read transcript items from a line cursor and return byte position.

    This compatibility reader supports existing durable state that
    only stored a line cursor. It scans the file once, parses only
    complete newline-terminated records after ``start_line``, and
    returns the byte offset so future polls can seek directly.

    :param transcript_path: Claude transcript path, e.g.
        ``"/home/user/.claude/projects/x/session.jsonl"``.
    :param start_line: One-based line cursor. Lines at or before
        this cursor are skipped.
    :param agent_name: Agent/model name stamped on assistant and
        tool-call items, e.g. ``"claude-native-ui"``.
    :param current_response_id: Response id for an in-progress
        Claude assistant turn from a previous poll.
    :returns: Parsed items plus line and byte cursors.
    """
    read_result = _read_complete_jsonl_records(
        transcript_path,
        byte_offset=0,
        start_line=0,
        emit_after_line=start_line,
    )
    items: list[ClaudeTranscriptItem] = []
    active_response_id = current_response_id
    latest_usage: dict[str, int] | None = None
    latest_model: str | None = None
    for record in read_result.records:
        if record.text is None:
            continue
        try:
            entry = json.loads(record.text)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        active_response_id, parsed = _transcript_items_from_entry(
            entry,
            line_number=record.line_number,
            record_offset=None,
            agent_name=agent_name,
            current_response_id=active_response_id,
        )
        items.extend(parsed)
        usage = _usage_from_transcript_entry(entry)
        if usage is not None:
            latest_usage = usage
        model = _model_from_transcript_entry(entry)
        if model is not None:
            latest_model = model
    return TranscriptReadResult(
        line_cursor=read_result.line_cursor,
        byte_offset=read_result.byte_offset,
        current_response_id=active_response_id,
        items=items,
        latest_usage=latest_usage,
        latest_model=latest_model,
    )


def read_transcript_items_from_offset(
    transcript_path: Path,
    byte_offset: int,
    *,
    start_line: int,
    agent_name: str,
    current_response_id: str | None = None,
    include_sidechains: bool = False,
) -> TranscriptReadResult:
    """
    Read transcript items appended after a byte offset.

    Only complete newline-terminated JSONL records are parsed. If
    Claude is midway through writing a trailing JSON record, the
    returned byte offset remains before that partial line so the next
    poll retries it after completion.

    :param transcript_path: Claude transcript path, e.g.
        ``"/home/user/.claude/projects/x/session.jsonl"``.
    :param byte_offset: Byte offset already consumed, e.g. ``4096``.
    :param start_line: Count of complete records already consumed.
        Used only to keep legacy line cursors and diagnostics
        monotonic while byte offsets drive the actual seek.
    :param agent_name: Agent/model name stamped on assistant and
        tool-call items, e.g. ``"claude-native-ui"``.
    :param current_response_id: Response id for an in-progress
        Claude assistant turn from a previous poll.
    :param include_sidechains: Pass ``True`` when reading a
        sub-agent's own ``agent-<id>.jsonl`` — every record there is
        a sidechain by Claude's definition, and dropping them would
        leave the sub-agent's child Omnigent conversation empty. The
        default ``False`` keeps the parent-transcript path
        unchanged.
    :returns: Parsed items plus updated line and byte cursors.
    """
    read_result = _read_complete_jsonl_records(
        transcript_path,
        byte_offset=byte_offset,
        start_line=start_line,
    )
    items: list[ClaudeTranscriptItem] = []
    active_response_id = current_response_id
    latest_usage: dict[str, int] | None = None
    latest_model: str | None = None
    for record in read_result.records:
        if record.text is None:
            continue
        try:
            entry = json.loads(record.text)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        active_response_id, parsed = _transcript_items_from_entry(
            entry,
            line_number=record.line_number,
            record_offset=record.byte_offset,
            agent_name=agent_name,
            current_response_id=active_response_id,
            include_sidechains=include_sidechains,
        )
        items.extend(parsed)
        usage = _usage_from_transcript_entry(entry)
        if usage is not None:
            latest_usage = usage
        model = _model_from_transcript_entry(entry)
        if model is not None:
            latest_model = model
    return TranscriptReadResult(
        line_cursor=read_result.line_cursor,
        byte_offset=read_result.byte_offset,
        current_response_id=active_response_id,
        items=items,
        latest_usage=latest_usage,
        latest_model=latest_model,
    )


# Per-model pricing memo for transcript cost computation. Deliberately
# NOT ``functools.lru_cache``: a transient ``fetch_model_pricing`` failure
# returns ``None``, and lru_cache would pin that ``None`` for the model's
# lifetime; this dict stores only successful lookups, so a later poll
# retries a model whose first lookup failed.
_TRANSCRIPT_PRICING_CACHE: dict[str, ModelPricing] = {}


def _transcript_model_pricing(model: str) -> ModelPricing | None:
    """
    Look up per-token pricing for *model*, memoizing successful results.

    :param model: API model id from a transcript ``message.model``,
        e.g. ``"claude-opus-4-8"`` or ``"databricks-claude-sonnet-4-6"``.
    :returns: The model's :class:`ModelPricing`, or ``None`` when pricing
        is unavailable (network error / model absent from the catalog),
        so the caller skips that message's cost.
    """
    cached = _TRANSCRIPT_PRICING_CACHE.get(model)
    if cached is not None:
        return cached
    from omnigent.llms.context_window import fetch_model_pricing

    pricing = fetch_model_pricing(model)
    if pricing is not None:
        _TRANSCRIPT_PRICING_CACHE[model] = pricing
    return pricing


def compute_transcript_cumulative_cost(
    transcript_path: Path,
    *,
    include_sidechains: bool,
) -> float | None:
    """
    Sum the USD cost of every assistant message in a Claude transcript.

    Reads the whole transcript and prices each assistant record's
    ``message.usage`` by that record's ``message.model`` (so a
    mid-session ``/model`` switch is billed at the right rate), summing
    the per-message costs. This is the forwarder's *real-time* cost
    estimate for a transcript whose authoritative cumulative cost lags —
    specifically a Task sub-agent's own ``agent-<id>.jsonl``, which has
    no statusLine of its own, so its spend is otherwise invisible to the
    cost-budget policy until the sub-agent finishes.

    Cost is linear in token counts, so summing per-message costs equals
    pricing the token totals — but per-message pricing also stays correct
    across a model switch within one transcript.

    **Deduplicated by ``requestId``.** Claude writes more than one
    transcript record for a single API response (a streamed partial plus
    the final record, retries, etc.), and those records share one
    ``requestId`` while each carries that response's full ``message.usage``
    (not an increment). Summing every record would bill the same response
    two-plus times — observed ~2x inflation, with the parent badge and the
    cost-budget gate both reading the doubled figure. So records are keyed
    by ``requestId`` (last priceable record per id wins, as its usage is
    the authoritative final figure) and each billed response is counted
    exactly once. A record with no ``requestId`` (rare non-API assistant
    entry) gets a per-record unique key so it is never collapsed with
    another.

    :param transcript_path: Path to a Claude transcript JSONL, e.g.
        ``".../<session>.jsonl"`` (parent) or
        ``".../subagents/agent-<id>.jsonl"`` (sub-agent).
    :param include_sidechains: ``False`` for a parent transcript — its
        sub-agent records are inlined as ``isSidechain: true`` and are
        skipped here (they are counted via the sub-agent's own
        transcript) to avoid double-billing. ``True`` for a sub-agent's
        own ``agent-<id>.jsonl``, where every record is a sidechain.
    :returns: Total USD cost across priced assistant messages, or
        ``None`` when the transcript has no assistant message that could
        be priced (missing/empty file, no usage, or pricing unavailable
        for every model present) — distinct from ``0.0``, which means
        priced messages summed to zero.
    """
    read_result = _read_complete_jsonl_records(
        transcript_path,
        byte_offset=0,
        start_line=0,
    )
    from omnigent.llms.context_window import compute_llm_cost

    # Per-``requestId`` cost (USD); last priceable record per id wins so a
    # response written across multiple transcript records is counted once.
    cost_by_request: dict[str, float] = {}
    # Counter minting unique keys for records lacking a ``requestId`` so
    # they each count once instead of collapsing onto a shared key.
    no_request_id_index = 0
    for record in read_result.records:
        if record.text is None:
            continue
        try:
            entry = json.loads(record.text)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        if not include_sidechains and entry.get("isSidechain") is True:
            continue
        usage = _usage_from_transcript_entry(entry)
        if usage is None:
            continue
        model = _model_from_transcript_entry(entry)
        if model is None:
            continue
        pricing = _transcript_model_pricing(model)
        if pricing is None:
            continue
        request_id = entry.get("requestId")
        if not isinstance(request_id, str) or not request_id:
            request_id = f"__no_request_id_{no_request_id_index}"
            no_request_id_index += 1
        cost_by_request[request_id] = compute_llm_cost(usage, pricing)
    if not cost_by_request:
        return None
    return sum(cost_by_request.values())


def count_hook_events(bridge_dir: Path) -> int:
    """
    Count hook records currently written for a bridge.

    :param bridge_dir: Bridge directory path.
    :returns: Number of hook JSONL records.
    """
    path = bridge_dir / _HOOKS_FILE
    try:
        with path.open("r", encoding="utf-8") as handle:
            return sum(1 for _line in handle)
    except FileNotFoundError:
        return 0


def read_hook_events_since(
    bridge_dir: Path,
    start_event_count: int,
) -> tuple[int, list[str]]:
    """
    Read hook event names appended after a hook cursor.

    The transcript forwarder uses this to publish ``session.status``
    events to Omnigent when Claude Code's ``Stop`` / ``StopFailure`` hooks
    fire — those are the only edges the wrapper can observe between
    Claude becoming idle and the JSONL transcript reflecting it.

    :param bridge_dir: Bridge directory path.
    :param start_event_count: One-based cursor; lines at or before
        this count are skipped.
    :returns: ``(new_cursor, hook_event_names)`` — ``new_cursor`` is
        the line count after the read, suitable for the next call.
        Malformed lines are skipped silently but still advance the
        cursor so they are not retried indefinitely.
    """
    result = read_hook_events_since_with_position(bridge_dir, start_event_count)
    names = [record.event_name for record in result.records if record.event_name is not None]
    return result.event_cursor, names


def read_hook_events_since_with_position(
    bridge_dir: Path,
    start_event_count: int,
) -> HookReadResult:
    """
    Read hook records from a line cursor and return byte position.

    This compatibility reader supports existing durable state that
    only stored a hook line cursor. It scans once, returns complete
    records after ``start_event_count``, and reports the byte offset
    so future polls can seek directly.

    :param bridge_dir: Bridge directory path.
    :param start_event_count: One-based cursor; lines at or before
        this count are skipped.
    :returns: Complete hook records plus updated line and byte cursors.
    """
    read_result = _read_complete_jsonl_records(
        bridge_dir / _HOOKS_FILE,
        byte_offset=0,
        start_line=0,
        emit_after_line=start_event_count,
    )
    records = [_hook_record_from_jsonl_record(record) for record in read_result.records]
    return HookReadResult(
        event_cursor=read_result.line_cursor,
        byte_offset=read_result.byte_offset,
        records=records,
    )


def read_hook_events_from_offset(
    bridge_dir: Path,
    byte_offset: int,
    *,
    start_event_count: int,
) -> HookReadResult:
    """
    Read hook records appended after a byte offset.

    Only complete newline-terminated JSONL records are returned. A
    partial trailing hook record leaves the byte offset unchanged so
    the next poll retries it after Claude finishes the write.

    :param bridge_dir: Bridge directory path.
    :param byte_offset: Byte offset already consumed, e.g. ``1024``.
    :param start_event_count: Count of complete hook records already
        consumed. Used to keep the legacy cursor monotonic while byte
        offsets drive the actual seek.
    :returns: Complete hook records plus updated line and byte cursors.
    """
    read_result = _read_complete_jsonl_records(
        bridge_dir / _HOOKS_FILE,
        byte_offset=byte_offset,
        start_line=start_event_count,
    )
    records = [_hook_record_from_jsonl_record(record) for record in read_result.records]
    return HookReadResult(
        event_cursor=read_result.line_cursor,
        byte_offset=read_result.byte_offset,
        records=records,
    )


def stop_hook_seen_since(bridge_dir: Path, start_event_count: int) -> bool:
    """
    Return whether Claude reported a stop event after a hook cursor.

    Only counts stop events from the parent Claude process — subagent
    stop events (whose ``transcript_path`` contains a ``subagents/``
    component) are ignored so a finishing subagent does not
    prematurely signal the parent turn as complete.

    :param bridge_dir: Bridge directory path.
    :param start_event_count: Hook record count captured before a
        message is injected into the Claude terminal.
    :returns: ``True`` once a parent-process ``Stop`` or
        ``StopFailure`` hook has been recorded after the cursor.
    """
    path = bridge_dir / _HOOKS_FILE
    try:
        with path.open("r", encoding="utf-8") as handle:
            for index, line in enumerate(handle, start=1):
                if index <= start_event_count:
                    continue
                try:
                    envelope = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = envelope.get("payload") if isinstance(envelope, dict) else None
                event_name = payload.get("hook_event_name") if isinstance(payload, dict) else None
                if event_name in {"Stop", "StopFailure"}:
                    transcript_path = (
                        payload.get("transcript_path") if isinstance(payload, dict) else None
                    )
                    if isinstance(transcript_path, str) and "/subagents/" in transcript_path:
                        continue
                    return True
    except FileNotFoundError:
        return False
    return False


def _hook_record_from_jsonl_record(record: _JsonlRecord) -> ClaudeHookRecord:
    """
    Convert one complete hook JSONL line into a hook record.

    :param record: Complete JSONL record read from ``hooks.jsonl``.
    :returns: Hook record with an event name when present. Malformed
        complete lines return ``event_name=None`` so callers can
        still advance durable cursors past them.
    """
    event_name: str | None = None
    try:
        envelope = json.loads(record.text) if record.text is not None else None
    except json.JSONDecodeError:
        envelope = None
    payload = envelope.get("payload") if isinstance(envelope, dict) else None
    raw_event_name = payload.get("hook_event_name") if isinstance(payload, dict) else None
    if isinstance(raw_event_name, str) and raw_event_name:
        event_name = raw_event_name
    raw_source = payload.get("source") if isinstance(payload, dict) else None
    raw_recorded_at = envelope.get("recorded_at") if isinstance(envelope, dict) else None
    raw_claude_session_id = payload.get("session_id") if isinstance(payload, dict) else None
    raw_transcript_path = payload.get("transcript_path") if isinstance(payload, dict) else None
    raw_previous_claude_session_id = (
        payload.get("omnigent_previous_claude_session_id") if isinstance(payload, dict) else None
    )
    raw_claude_session_was_seen = (
        payload.get("omnigent_claude_session_was_seen") if isinstance(payload, dict) else None
    )
    raw_clear_rotated_to = (
        payload.get("omnigent_clear_rotated_to") if isinstance(payload, dict) else None
    )
    raw_fork_detected = (
        payload.get("omnigent_fork_detected") if isinstance(payload, dict) else None
    )
    raw_fork_rotated_to = (
        payload.get("omnigent_fork_rotated_to") if isinstance(payload, dict) else None
    )
    # Extract todos from PostToolUse/TodoWrite hook payloads. Claude Code
    # fires this hook after every TodoWrite call with ``tool_input.todos``
    # containing the updated list. Other PostToolUse events have no todos.
    todos: list[dict[str, Any]] | None = None
    task_id: str | None = None
    task_subject: str | None = None
    task_status: str | None = None
    if event_name == "PostToolUse" and isinstance(payload, dict):
        raw_tool_name = payload.get("tool_name")
        if raw_tool_name == "TodoWrite":
            raw_tool_input = payload.get("tool_input")
            if isinstance(raw_tool_input, dict):
                raw_todos = raw_tool_input.get("todos")
                if isinstance(raw_todos, list):
                    todos = [t for t in raw_todos if isinstance(t, dict)]
        elif raw_tool_name == "TaskUpdate":
            raw_tool_input = payload.get("tool_input")
            if isinstance(raw_tool_input, dict):
                raw_task_id = raw_tool_input.get("taskId")
                raw_task_status = raw_tool_input.get("status")
                if isinstance(raw_task_id, str) and raw_task_id:
                    task_id = raw_task_id
                if isinstance(raw_task_status, str) and raw_task_status:
                    task_status = raw_task_status
    elif event_name == "TaskCreated" and isinstance(payload, dict):
        raw_task_id = payload.get("task_id")
        raw_task_subject = payload.get("task_subject")
        if isinstance(raw_task_id, str) and raw_task_id:
            task_id = raw_task_id
        if isinstance(raw_task_subject, str) and raw_task_subject:
            task_subject = raw_task_subject
        task_status = "pending"
    elif event_name == "TaskCompleted" and isinstance(payload, dict):
        raw_task_id = payload.get("task_id")
        if isinstance(raw_task_id, str) and raw_task_id:
            task_id = raw_task_id
        task_status = "completed"
    return ClaudeHookRecord(
        event_cursor=record.line_number,
        byte_offset=record.next_byte_offset,
        event_name=event_name,
        recorded_at=raw_recorded_at
        if isinstance(raw_recorded_at, (int, float)) and not isinstance(raw_recorded_at, bool)
        else None,
        source=raw_source if isinstance(raw_source, str) and raw_source else None,
        claude_session_id=(
            raw_claude_session_id
            if isinstance(raw_claude_session_id, str) and raw_claude_session_id
            else None
        ),
        transcript_path=(
            Path(raw_transcript_path)
            if isinstance(raw_transcript_path, str) and raw_transcript_path
            else None
        ),
        previous_claude_session_id=(
            raw_previous_claude_session_id
            if isinstance(raw_previous_claude_session_id, str) and raw_previous_claude_session_id
            else None
        ),
        claude_session_was_seen=(
            raw_claude_session_was_seen if isinstance(raw_claude_session_was_seen, bool) else None
        ),
        clear_rotated_to=(
            raw_clear_rotated_to
            if isinstance(raw_clear_rotated_to, str) and raw_clear_rotated_to
            else None
        ),
        fork_detected=raw_fork_detected is True,
        fork_rotated_to=(
            raw_fork_rotated_to
            if isinstance(raw_fork_rotated_to, str) and raw_fork_rotated_to
            else None
        ),
        todos=todos,
        task_id=task_id,
        task_subject=task_subject,
        task_status=task_status,
    )


def _read_complete_jsonl_records(
    path: Path,
    *,
    byte_offset: int,
    start_line: int,
    emit_after_line: int | None = None,
) -> _JsonlReadResult:
    """
    Read complete newline-terminated records from a JSONL file.

    The reader seeks to ``byte_offset`` and stops before a trailing
    partial line. That partial line's bytes are retried by the next
    poll after the writer appends its newline.

    :param path: JSONL file path.
    :param byte_offset: Byte offset where reading should begin,
        e.g. ``4096``.
    :param start_line: Count of complete records before
        ``byte_offset``, e.g. ``12``.
    :param emit_after_line: When provided, complete records at or
        before this line number are counted for cursor migration but
        not decoded or stored.
    :returns: Complete records plus updated line and byte cursors.
    """
    if byte_offset < 0:
        raise ValueError(f"byte_offset must be non-negative, got {byte_offset}")
    if start_line < 0:
        raise ValueError(f"start_line must be non-negative, got {start_line}")
    records: list[_JsonlRecord] = []
    cursor = start_line
    position = byte_offset
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            file_size = handle.tell()
            if byte_offset > file_size:
                handle.seek(0)
                cursor = 0
                position = 0
            else:
                handle.seek(byte_offset)
            while True:
                record_start = position
                raw = handle.readline()
                if not raw:
                    break
                if not raw.endswith(b"\n"):
                    break
                position = handle.tell()
                cursor += 1
                if emit_after_line is not None and cursor <= emit_after_line:
                    continue
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    text = None
                records.append(
                    _JsonlRecord(
                        line_number=cursor,
                        byte_offset=record_start,
                        next_byte_offset=position,
                        text=text,
                    )
                )
    except FileNotFoundError:
        return _JsonlReadResult(
            line_cursor=start_line,
            byte_offset=byte_offset,
            records=[],
        )
    return _JsonlReadResult(
        line_cursor=cursor,
        byte_offset=position,
        records=records,
    )


def write_tmux_target(
    bridge_dir: Path,
    *,
    socket_path: Path,
    tmux_target: str,
    pid: int | None = None,
) -> None:
    """
    Advertise the tmux socket + target for the Claude terminal.

    The runner calls this after launching the ``claude/main`` terminal
    so the harness can shell out to ``tmux send-keys`` against the
    same private socket the terminal was launched on.

    :param bridge_dir: Bridge directory path, e.g.
        ``/tmp/omnigent/claude-native/<digest>``.
    :param socket_path: Absolute path to the terminal's private tmux
        socket, e.g. ``Path("/tmp/.../tmux.sock")``.
    :param tmux_target: tmux pane target string, e.g. ``"claude:0.0"``.
    :param pid: Optional Claude process pid, recorded for diagnostics.
    :returns: None.
    """
    _ensure_secure_dir(bridge_dir)
    payload: dict[str, Any] = {
        "socket_path": str(socket_path),
        "tmux_target": tmux_target,
        "updated_at": time.time(),
    }
    if pid is not None:
        payload["pid"] = pid
    _write_json_file(bridge_dir / _TMUX_FILE, payload)


def inject_user_message(
    bridge_dir: Path,
    *,
    content: str,
    timeout_s: float = _TMUX_READY_TIMEOUT_S,
) -> None:
    r"""
    Deliver a user message into the Claude terminal via tmux send-keys.

    Before typing, this waits for two readiness conditions: the runner
    advertising ``tmux.json``, then Claude Code's input box rendering
    (see :func:`_wait_for_claude_prompt_ready`). The second gate closes
    a race on freshly-created sessions where the first message would
    otherwise be typed into a still-booting TUI and silently dropped.

    Delivered as one bracketed paste via ``tmux load-buffer`` (from a
    temp file) + ``paste-buffer -p`` so interior newlines ride as raw CR
    inside the paste markers and Claude Code's TUI keeps multi-line
    input as data rather than submitting on each newline
    (anthropics/claude-code#52126). A trailing newline inside the paste
    absorbs any trailing backslash — otherwise ``\`` + the submit
    ``Enter`` reads as a line-continuation and the message sits unsent.
    ``Enter`` is a separate tmux call. The file-based buffer
    path (not ``send-keys`` argv) matters: tmux caps a single
    client→server command at ~16KB, so a large message — e.g. a PR diff
    in a sub-agent dispatch — failed with "command too long".

    The submit is **verified, not fire-and-forget**: Claude Code
    coalesces rapid stdin bursts into a paste, so an Enter that lands
    while the TUI is still consuming the paste is folded in as a
    newline and the draft sits unsent. This helper first polls
    ``capture-pane`` until the draft is visible in the input box (the
    paste was committed), sends Enter, then polls that the draft left
    the box — re-sending Enter while it hasn't — and raises if the
    message never submits.

    :param bridge_dir: Bridge directory path.
    :param content: User text from the Omnigent web UI. Must be non-empty.
    :param timeout_s: Seconds to wait for each readiness gate
        (``tmux.json`` advertised, then prompt rendered), e.g. ``30.0``.
    :returns: None.
    :raises RuntimeError: If the tmux target is not advertised in time,
        if Claude's input prompt never renders, if a ``tmux send-keys``
        invocation fails, or if the draft never leaves the input box
        after repeated submit Enters (message not delivered).
    """
    info = _wait_for_tmux_info(bridge_dir, timeout_s=timeout_s)
    # tmux.json only means the tmux session exists; Claude Code's input
    # box mounts a few seconds later. Block until the prompt renders so
    # the first message isn't typed into a still-booting TUI and dropped.
    _wait_for_claude_prompt_ready(
        info["socket_path"],
        info["tmux_target"],
        timeout_s=timeout_s,
    )
    # Clear any leftover text in Claude's input field before typing.
    # After Escape-cancel, Claude Code re-populates the prompt area
    # with the previous input for re-editing. Without this clear,
    # the new message appends to the stale buffer (e.g.
    # "old promptnew prompt" with no separator).
    # Ctrl-A (Home) + Ctrl-K (kill-to-end) is the safest pair —
    # Ctrl-U only clears backwards from cursor.
    _run_tmux(info["socket_path"], "send-keys", "-t", info["tmux_target"], "C-a")
    _run_tmux(info["socket_path"], "send-keys", "-t", info["tmux_target"], "C-k")
    # Trailing newline absorbs a trailing "\" so it can't escape the submit Enter.
    # Delivered through a tmux buffer, NOT ``send-keys`` argv: tmux caps one
    # client→server command at ~16KB, so per-byte hex argv blew up with
    # "command too long" on large payloads (a PR diff in a sub-agent
    # dispatch). ``load-buffer`` streams the file without that cap, and
    # ``paste-buffer -p`` wraps it in the same bracketed-paste markers so
    # interior newlines (mapped to CR below) stay data instead of becoming
    # per-line submits. See anthropics/claude-code#52126.
    with tempfile.NamedTemporaryFile(
        dir=bridge_dir, prefix="paste_", suffix=".bin", delete=False
    ) as paste_file:
        paste_file.write(_paste_payload_bytes(content + "\n"))
        paste_path = paste_file.name
    try:
        _run_tmux(info["socket_path"], "load-buffer", "-b", "omnigent-paste", paste_path)
        _run_tmux(
            info["socket_path"],
            "paste-buffer",
            "-p",  # bracketed-paste markers — the TUI keeps newlines as data
            "-d",  # drop the buffer after pasting (no stale copies server-side)
            "-b",
            "omnigent-paste",
            "-t",
            info["tmux_target"],
        )
    finally:
        with contextlib.suppress(OSError):
            os.unlink(paste_path)
    # Wait until the TUI has visibly committed the paste into its input
    # box before submitting. Claude Code coalesces rapid stdin bursts
    # into a paste; an Enter that arrives while it is still consuming
    # the paste becomes a newline inside the draft instead of a submit,
    # and the message sits unsent. A fixed sleep raced this (lost under
    # load / large payloads); polling is deterministic. Best-effort:
    # when the draft never becomes identifiable (e.g. whitespace-only
    # first line, custom statusline containing the glyph), fall through
    # after the timeout and submit blind, matching the old behavior.
    needle = _submit_needle(content)
    draft_seen = False
    deadline = time.monotonic() + _PASTE_COMMIT_TIMEOUT_S
    while time.monotonic() < deadline:
        if _draft_in_input_box(_capture_pane(info["socket_path"], info["tmux_target"]), needle):
            draft_seen = True
            break
        time.sleep(_CLAUDE_READY_POLL_INTERVAL_S)
    time.sleep(_PASTE_SETTLE_S)
    _run_tmux(info["socket_path"], "send-keys", "-t", info["tmux_target"], "Enter")
    if not draft_seen:
        # The draft was never observed, so its absence proves nothing —
        # verification would trivially "pass". Submit blind as before.
        return
    # Verify the submit took: a successful Enter clears the input box.
    # If the draft is still sitting there the Enter was swallowed into
    # the paste burst as a newline — re-send it (the retry lands well
    # after the burst, so it submits). Each Enter only fires while the
    # draft is verifiably still present, so a retry can never hit an
    # empty prompt or a permission dialog of the started turn.
    deadline = time.monotonic() + _SUBMIT_VERIFY_TIMEOUT_S
    last_enter = time.monotonic()
    while time.monotonic() < deadline:
        time.sleep(_CLAUDE_READY_POLL_INTERVAL_S)
        pane = _capture_pane(info["socket_path"], info["tmux_target"])
        if not _draft_in_input_box(pane, needle):
            return
        if time.monotonic() - last_enter >= _SUBMIT_RETRY_INTERVAL_S:
            _run_tmux(info["socket_path"], "send-keys", "-t", info["tmux_target"], "Enter")
            last_enter = time.monotonic()
    raise RuntimeError(
        f"Claude Code did not accept the submitted message within {_SUBMIT_VERIFY_TIMEOUT_S}s "
        "(the draft is still in the input box). The message was not delivered."
    )


def inject_interrupt(
    bridge_dir: Path,
    *,
    timeout_s: float = _TMUX_READY_TIMEOUT_S,
) -> None:
    """
    Send an Escape keystroke into the Claude terminal via tmux send-keys.

    Claude Code's TUI cancels an in-flight response on a single
    ``Escape``. The harness's ``run_turn`` for ``claude-native``
    returns immediately after the tmux paste (the long-running work
    happens inside the ``claude`` binary in the pane, not the
    harness), so the scaffold's interrupt path can't reach it — this
    helper is the analog of :func:`inject_user_message` for the AP
    web stop button / Escape keybind.

    :param bridge_dir: Bridge directory path, e.g.
        ``/tmp/omnigent/claude-native/<digest>``.
    :param timeout_s: Seconds to wait for ``tmux.json`` to be
        advertised by the runner, e.g. ``30.0``.
    :returns: None.
    :raises RuntimeError: If the tmux target is not advertised in
        time, or if the ``tmux send-keys`` invocation fails.
    """
    info = _wait_for_tmux_info(bridge_dir, timeout_s=timeout_s)
    # No ``-l``: tmux must interpret ``Escape`` as a key name.
    _run_tmux(info["socket_path"], "send-keys", "-t", info["tmux_target"], "Escape")


def kill_session(
    bridge_dir: Path,
    *,
    timeout_s: float = _TMUX_READY_TIMEOUT_S,
) -> None:
    """
    Forcefully terminate the Claude tmux session via ``kill-session``.

    Claude-native sessions run the ``claude`` binary inside a tmux
    session on a per-session socket (see
    :class:`omnigent.inner.terminal.TerminalInstance`). The only way
    a user can end such a session today is to re-attach to the tmux in
    their terminal and exit from inside it. This helper is the analog
    of that manual exit for the Omnigent web UI's "Stop session" affordance:
    it kills the tmux session outright, which terminates ``claude`` and
    everything in the pane.

    Unlike :func:`inject_interrupt` (which sends a single ``Escape`` to
    cancel an in-flight response but leaves the session alive), this is
    a hard stop. Once the pane is gone the wrapper's reconnect loop
    observes the terminal resource disappear and tears the session
    down through its normal end-of-session path, so no transcript items
    are synthesized here.

    :param bridge_dir: Bridge directory path, e.g.
        ``/tmp/omnigent/claude-native/<digest>``.
    :param timeout_s: Seconds to wait for ``tmux.json`` to be
        advertised by the runner, e.g. ``30.0``. A short value is
        appropriate for the UI path — a missing ``tmux.json`` means
        there is no live session to kill.
    :returns: None.
    :raises RuntimeError: If the tmux target is not advertised in
        time, or if the ``tmux kill-session`` invocation fails.
    """
    info = _wait_for_tmux_info(bridge_dir, timeout_s=timeout_s)
    _run_tmux(info["socket_path"], "kill-session", "-t", info["tmux_target"])


def inject_slash_command(
    bridge_dir: Path,
    *,
    command: str,
    timeout_s: float = _TMUX_READY_TIMEOUT_S,
    auto_confirm: bool = False,
) -> None:
    """
    Type a Claude Code slash command into the tmux pane and submit it.

    :param bridge_dir: Bridge directory path, e.g.
        ``/tmp/omnigent/claude-native/<digest>``.
    :param command: Single-line slash command including the leading
        ``/``, e.g. ``"/effort high"``.
    :param timeout_s: Seconds to wait for ``tmux.json``, e.g. ``30.0``.
    :param auto_confirm: If ``True``, send an extra ``Enter`` after a
        short delay to accept the default option of any TUI confirmation
        dialog that the command may pop (e.g. ``/effort`` / ``/model``
        prompt when switching invalidates the prompt cache). HACK —
        the chat UI has no way to render the CLI's TUI dialog, so
        without this the command silently stalls. Assumes the default
        option is "accept" (true today for effort + model). When no
        dialog appears, the extra Enter falls on an empty prompt and is
        a no-op. Callers that don't trigger confirmations should leave
        this ``False``.
    :raises ValueError: If *command* is empty, does not start with
        ``/``, or contains a newline.
    :raises RuntimeError: If the tmux target is not advertised in
        time, or if a ``tmux send-keys`` invocation fails.
    """
    if not command or not command.startswith("/"):
        raise ValueError(f"slash command must start with '/'; got {command!r}")
    if "\n" in command:
        raise ValueError("slash command must be a single line")
    info = _wait_for_tmux_info(bridge_dir, timeout_s=timeout_s)
    # ``C-u`` clears any draft the user is mid-typing; otherwise the
    # paste below concatenates with their text and Enter submits
    # ``<their-draft>/effort high`` as a turn. Unlike Escape it does
    # not interrupt an in-flight generation.
    _run_tmux(info["socket_path"], "send-keys", "-t", info["tmux_target"], "C-u")
    # ``-l`` pastes ``/`` and spaces literally; trailing Enter submits.
    _run_tmux(info["socket_path"], "send-keys", "-l", "-t", info["tmux_target"], command)
    _run_tmux(info["socket_path"], "send-keys", "-t", info["tmux_target"], "Enter")
    if auto_confirm:
        # Give the TUI time to render its confirmation dialog before
        # the auto-Enter arrives; otherwise the keystroke races the
        # prompt and gets dropped.
        time.sleep(0.3)
        _run_tmux(info["socket_path"], "send-keys", "-t", info["tmux_target"], "Enter")


def display_cost_approval_popup(
    bridge_dir: Path,
    *,
    session_id: str,
    elicitation_id: str,
    message: str,
    policy_name: str | None = None,
    python_executable: str | None = None,
    timeout_s: float = _TMUX_READY_TIMEOUT_S,
) -> None:
    """
    Overlay a cost-budget approval modal on the Claude Code tmux pane.

    Launches :mod:`omnigent.native_cost_popup` inside a
    ``tmux display-popup``, so a user working in the native terminal —
    not only the web ``ApprovalCard`` — can approve/decline a cost
    checkpoint. The popup script resolves the **same** elicitation Future
    (via the same resolve endpoint the web card uses), so whichever
    surface answers first wins and the other clears. The popup reads AP
    routing (base URL + auth headers) from this bridge's
    ``permission_hook.json`` so no token lands on the command line.

    Fire-and-forget by design: ``tmux display-popup`` blocks its tmux
    client until the popup closes, so it is spawned **detached**
    (``Popen``, not awaited) — the caller returns immediately while the
    modal lives on the attached client until the user answers.

    Claude-native resolver for the harness-agnostic
    :func:`omnigent.native_cost_popup.launch_cost_popup`: it reads the
    pane's tmux socket/target from this bridge's ``tmux.json`` and points
    the popup at this bridge's ``permission_hook.json`` for Omnigent routing
    (base URL + auth headers, so no token lands on the command line), then
    delegates. The launcher pops the modal on every attached client and
    skips silently when none is attached (e.g. the Terminal tab is closed)
    — the web ``ApprovalCard`` remains the answer surface.

    :param bridge_dir: Bridge directory path, e.g.
        ``/tmp/omnigent/claude-native/<digest>``. Supplies both the
        tmux target (``tmux.json``) and the AP-routing config
        (``permission_hook.json``).
    :param session_id: Omnigent session id that owns the elicitation, e.g.
        ``"conv_abc123"``. Used in the resolve URL the popup POSTs to.
    :param elicitation_id: Outstanding elicitation correlation id, e.g.
        ``"elicit_deadbeef"``.
    :param message: Approval reason shown in the popup, e.g.
        ``"Session cost $0.12 crossed the $0.10 checkpoint. Continue?"``.
    :param policy_name: Name of the deciding policy, rendered as the
        modal header. ``None`` falls back to a generic header.
    :param python_executable: Python used to run the popup module;
        ``None`` uses :data:`sys.executable` (the runner's interpreter,
        valid on the host the tmux server runs on).
    :param timeout_s: Seconds to wait for ``tmux.json`` to be advertised,
        e.g. ``30.0``.
    :returns: None.
    :raises RuntimeError: If the tmux target is not advertised within
        *timeout_s* (the pane isn't up yet); the caller treats this as a
        best-effort miss and the web card remains answerable.
    """
    from omnigent.native_cost_popup import launch_cost_popup

    info = _wait_for_tmux_info(bridge_dir, timeout_s=timeout_s)
    launch_cost_popup(
        info["socket_path"],
        info["tmux_target"],
        bridge_dir / _PERMISSION_HOOK_FILE,
        session_id=session_id,
        elicitation_id=elicitation_id,
        message=message,
        policy_name=policy_name,
        python_executable=python_executable,
    )


def post_tools_changed(
    bridge_dir: Path,
    *,
    timeout_s: float = _TOOLS_CHANGED_READY_TIMEOUT_S,
) -> None:
    """
    Notify Claude Code that the MCP tool list changed.

    Standard MCP ``notifications/tools/list_changed`` — the bridge's
    localhost HTTP control endpoint trampolines the POST into the
    MCP stdio writer. Unrelated to Claude's experimental Channels.

    :param bridge_dir: Bridge directory path.
    :param timeout_s: Seconds to wait for the bridge HTTP control
        endpoint to publish itself, e.g. ``30.0``.
    :returns: None.
    :raises RuntimeError: If the bridge server is not ready or
        rejects the notification.
    """
    server = _wait_for_server_info(bridge_dir, timeout_s=timeout_s)
    token = server.get("token")
    url = server.get("url")
    if not isinstance(token, str) or not isinstance(url, str):
        raise RuntimeError("Claude native bridge server file is missing url/token")
    req = request.Request(
        f"{url}/tools-changed",
        data=b"{}",
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with request.urlopen(req, timeout=_TOOLS_CHANGED_POST_TIMEOUT_S) as resp:
            if resp.status >= 400:
                raise RuntimeError(f"tools-changed POST failed with HTTP {resp.status}")
    except error.URLError as exc:
        raise RuntimeError(f"failed to notify Claude tool list change: {exc}") from exc


def _run_tmux(socket_path: str, *args: str) -> None:
    """
    Invoke ``tmux -S <socket_path> <args...>`` and raise on failure.

    :param socket_path: Absolute path to the tmux socket the terminal
        was launched on, e.g. ``"/tmp/.../tmux.sock"``.
    :param args: Arguments after ``tmux -S <socket_path>``, e.g.
        ``("send-keys", "-l", "-t", "claude:0.0", "hello")``.
    :returns: None.
    :raises RuntimeError: If the subprocess exits non-zero or times
        out.
    """
    import subprocess

    cmd = ["tmux", "-S", socket_path, *args]
    try:
        proc = subprocess.run(
            cmd,
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
    """
    Capture the current visible contents of a tmux pane.

    Unlike :func:`_run_tmux`, this returns stdout instead of raising on
    output, and never raises — a transient capture failure during boot
    should be treated as "not ready yet" by the caller, not an error.

    :param socket_path: Absolute path to the tmux socket, e.g.
        ``"/tmp/.../tmux.sock"``.
    :param tmux_target: tmux pane target string, e.g. ``"main"``.
    :returns: The pane's visible text, or ``""`` if capture failed.
    """
    import subprocess

    try:
        proc = subprocess.run(
            ["tmux", "-S", socket_path, "capture-pane", "-t", tmux_target, "-p"],
            check=False,
            capture_output=True,
            text=True,
            timeout=_TMUX_SEND_TIMEOUT_S,
        )
    except (subprocess.SubprocessError, OSError):
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def _claude_prompt_rendered(pane: str) -> bool:
    """
    Return whether Claude Code's input prompt is rendered in a pane.

    Scans the last :data:`_PROMPT_SCAN_TAIL_LINES` non-empty lines for
    :data:`_CLAUDE_PROMPT_GLYPH`. Restricting to the tail avoids false
    positives from the glyph appearing in scrollback (e.g. echoed in a
    prior response), since the live input box always sits at the bottom.

    :param pane: Captured pane text from :func:`_capture_pane`.
    :returns: ``True`` when the input box appears mounted.
    """
    non_empty = [line for line in pane.splitlines() if line.strip()]
    return any(_CLAUDE_PROMPT_GLYPH in line for line in non_empty[-_PROMPT_SCAN_TAIL_LINES:])


def _submit_needle(content: str) -> str:
    r"""
    Derive a short marker string used to spot a draft in the input box.

    Takes the first non-empty line of *content* (after the same
    line-ending normalization the paste payload gets), truncated at the
    first control character and to :data:`_DRAFT_NEEDLE_MAX_CHARS`, so
    it matches what Claude Code renders verbatim on the prompt row.

    :param content: Raw user text, possibly multi-line,
        e.g. ``"fix the bug\nin foo.py"``.
    :returns: The needle, e.g. ``"fix the bug"``. Empty string when no
        usable line exists (whitespace-only content) — callers must
        then skip draft-visibility checks.
    """
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    for line in normalized.split("\n"):
        # Truncate at the first control char (e.g. an interior tab):
        # the TUI renders those differently, so they can't be matched
        # verbatim against the captured pane text.
        for idx, ch in enumerate(line):
            if ord(ch) < 0x20:
                line = line[:idx]
                break
        line = line.strip()
        if line:
            return line[:_DRAFT_NEEDLE_MAX_CHARS]
    return ""


def _draft_in_input_box(pane: str, needle: str) -> bool:
    """
    Return whether the pasted draft is visible in Claude's input box.

    Looks only at the **last** line containing
    :data:`_CLAUDE_PROMPT_GLYPH` — the live input box always sits at
    the bottom of the pane, below the transcript, so this never
    matches the submitted message's transcript echo. The draft counts
    as visible when the text after the glyph contains *needle* (small
    pastes render verbatim) or the
    :data:`_PASTED_PLACEHOLDER_PREFIX` placeholder (Claude Code
    collapses large pastes).

    :param pane: Captured pane text from :func:`_capture_pane`.
    :param needle: Marker from :func:`_submit_needle`, e.g.
        ``"fix the bug"``. Empty means the draft can't be identified;
        only the paste placeholder is then considered.
    :returns: ``True`` when the draft is still sitting in the input box.
    """
    glyph_lines = [line for line in pane.splitlines() if _CLAUDE_PROMPT_GLYPH in line]
    if not glyph_lines:
        return False
    tail = glyph_lines[-1].rsplit(_CLAUDE_PROMPT_GLYPH, 1)[1]
    if _PASTED_PLACEHOLDER_PREFIX in tail:
        return True
    return bool(needle) and needle in tail


def _format_terminal_failure_tail(pane: str) -> str:
    r"""
    Format the tail of a captured tmux pane for a failure message.

    When Claude Code's input prompt never renders, its own on-screen
    output — often a startup error or stack trace (e.g. a ``JSON Parse
    error`` raised when its API client receives an HTML page instead of
    JSON) — is the only signal of the real cause. The readiness gate
    raises into the web UI's error banner, so attaching this tail
    surfaces that cause without the user having to open the terminal.

    :param pane: Captured pane text from :func:`_capture_pane`.
    :returns: A ``" Last terminal output:\n<tail>"`` block — the last
        :data:`_TERMINAL_FAILURE_TAIL_LINES` non-blank lines, capped at
        :data:`_TERMINAL_FAILURE_TAIL_CHARS` characters — or ``""`` when
        the pane has no visible text.
    """
    lines = [line.rstrip() for line in pane.splitlines() if line.strip()]
    if not lines:
        return ""
    tail = "\n".join(lines[-_TERMINAL_FAILURE_TAIL_LINES:])
    if len(tail) > _TERMINAL_FAILURE_TAIL_CHARS:
        tail = "…" + tail[-_TERMINAL_FAILURE_TAIL_CHARS:]
    return f" Last terminal output:\n{tail}"


def _wait_for_claude_prompt_ready(
    socket_path: str,
    tmux_target: str,
    *,
    timeout_s: float,
) -> None:
    """
    Block until Claude Code's TUI input box is ready for keystrokes.

    The runner advertises ``tmux.json`` as soon as the tmux session
    exists, but Claude Code's input box mounts a few seconds later
    (longer on a cold first boot). Keystrokes sent into that gap are
    dropped, so the first web-UI message silently vanishes. This gate
    polls ``capture-pane`` for the input prompt before injection;
    it returns immediately once mounted, so 2nd+ messages are
    unaffected.

    Claude-native only — this is called from :func:`inject_user_message`,
    which exclusively serves the Claude Code terminal. It must never be
    used for generic terminals, whose programs never render
    :data:`_CLAUDE_PROMPT_GLYPH` and would always time out.

    :param socket_path: Absolute path to the tmux socket, e.g.
        ``"/tmp/.../tmux.sock"``.
    :param tmux_target: tmux pane target string, e.g. ``"main"``.
    :param timeout_s: Seconds to wait for the prompt, e.g. ``30.0``.
    :returns: None.
    :raises RuntimeError: If the prompt never renders within
        *timeout_s* (Claude failed to boot). The message carries the
        tail of the captured pane (see :func:`_format_terminal_failure_tail`)
        so Claude Code's own startup output surfaces in the caller's error.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _claude_prompt_rendered(_capture_pane(socket_path, tmux_target)):
            return
        time.sleep(_CLAUDE_READY_POLL_INTERVAL_S)
    # Timed out: Claude Code never rendered its input prompt. Capture the
    # pane one last time and attach its tail so the real cause — often a
    # startup crash like a ``JSON Parse error`` — surfaces in the web UI
    # error banner this raises into, instead of only a generic timeout
    # the user has to open the terminal to diagnose.
    pane = _capture_pane(socket_path, tmux_target)
    raise RuntimeError(
        f"Claude Code terminal did not become ready within {timeout_s}s "
        "(input prompt never rendered). The message was not delivered."
        + _format_terminal_failure_tail(pane)
    )


def _paste_payload_bytes(text: str) -> bytes:
    r"""
    Encode text as the paste-buffer byte payload for ``tmux load-buffer``.

    Returns only the content bytes — ``paste-buffer -p`` adds the
    ``ESC [ 2 0 0 ~`` / ``ESC [ 2 0 1 ~`` bracketed-paste markers
    itself when delivering the buffer to the pane.

    Content bytes are mapped so Claude Code's TUI keeps the paste as
    editable data rather than submitting on each line:

    - ``\n`` and ``\r`` (and a ``\r\n`` pair coalesced) become a single
      carriage return ``0x0d`` — the byte a real paste carries between
      lines inside the markers.
    - ``\t`` becomes ``0x09``.
    - Any other control byte below ``0x20`` is dropped: a stray ``ESC``
      (or BEL, etc.) in the content would otherwise prematurely close
      the bracketed-paste sequence on the agent's side.
    - All other characters pass through as their UTF-8 bytes.

    :param text: Raw user text, possibly multi-line, e.g.
        ``"line one\nline two"`` or ``"a\r\nb"``.
    :returns: The normalized content bytes, e.g. ``b"line one\rline two"``.
    """
    # Normalize line endings to a single "\n" first so CRLF / lone CR
    # pastes don't double up: every line break becomes exactly one CR.
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


def _wait_for_tmux_info(bridge_dir: Path, *, timeout_s: float) -> dict[str, str]:
    """
    Wait for the runner to write ``tmux.json``.

    :param bridge_dir: Bridge directory path.
    :param timeout_s: Seconds to wait, e.g. ``30.0``.
    :returns: ``{"socket_path": ..., "tmux_target": ...}``.
    :raises RuntimeError: If the file never appears with valid
        ``socket_path`` and ``tmux_target`` fields.
    """
    deadline = time.monotonic() + timeout_s
    path = bridge_dir / _TMUX_FILE
    while time.monotonic() < deadline:
        payload = _read_json_file(path)
        socket_path = payload.get("socket_path") if isinstance(payload, dict) else None
        tmux_target = payload.get("tmux_target") if isinstance(payload, dict) else None
        if isinstance(socket_path, str) and isinstance(tmux_target, str):
            return {"socket_path": socket_path, "tmux_target": tmux_target}
        time.sleep(0.05)
    raise RuntimeError(
        "Claude terminal tmux target is not advertised yet. Wait for the "
        "terminal to launch before sending messages from the web UI."
    )


def start_tool_relay(
    *,
    bridge_dir: Path,
    tools: list[dict[str, Any]],
    tool_executor: ToolExecutor,
    loop: asyncio.AbstractEventLoop,
) -> ClaudeNativeToolRelay:
    """
    Start a relay for Omnigent tool calls from Claude.

    Writes ``tool_relay.json`` and starts the localhost HTTP server that
    backs it. The caller owns the relay's lifetime (a single turn or a
    whole session) and must call :meth:`ClaudeNativeToolRelay.close` when
    that scope ends.

    :param bridge_dir: Bridge directory path.
    :param tools: Omnigent tool schemas to advertise, e.g.
        ``[{"name": "sys_os_read", "parameters": {...}}]``.
    :param tool_executor: Callback used to dispatch one tool call through
        AP/runner.
    :param loop: Event loop that owns ``tool_executor``.
    :returns: Started relay handle. Call :meth:`close` when the relay's
        scope ends (e.g. on session delete).
    """
    token = secrets.token_urlsafe(32)
    handler_cls = _tool_relay_handler_factory(token, tool_executor, loop)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    host, port = httpd.server_address
    relay_info = {
        "url": f"http://{host}:{port}",
        "token": token,
        "tools": _normalize_relay_tool_specs(tools),
        "pid": os.getpid(),
        "updated_at": time.time(),
    }
    _write_json_file(bridge_dir / _TOOL_RELAY_FILE, relay_info)
    thread = threading.Thread(
        target=httpd.serve_forever,
        name="claude-native-tool-relay",
        daemon=True,
    )
    thread.start()
    return ClaudeNativeToolRelay(bridge_dir=bridge_dir, httpd=httpd)


def main(argv: list[str] | None = None) -> None:
    """
    CLI entrypoint for bridge helper processes.

    :param argv: Optional argv override excluding program name.
        ``None`` reads :data:`sys.argv`.
    :returns: None.
    """
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    if args.command == "serve-mcp":
        _serve_mcp(Path(args.bridge_dir))
        return
    raise SystemExit(f"unknown command: {args.command}")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    """
    Parse bridge helper CLI arguments.

    :param argv: CLI argv excluding program name, e.g.
        ``["serve-mcp", "--bridge-dir", "/tmp/x"]``.
    :returns: Parsed argparse namespace.
    """
    parser = argparse.ArgumentParser(prog="python -m omnigent.claude_native_bridge")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve-mcp")
    serve.add_argument("--bridge-dir", required=True)
    return parser.parse_args(argv)


def _serve_mcp(bridge_dir: Path) -> None:
    """
    Run the MCP stdio server and the local control HTTP endpoint.

    :param bridge_dir: Bridge directory path.
    :returns: None when stdin closes.
    """
    os.environ[BRIDGE_DIR_ENV_VAR] = str(bridge_dir)
    config = _read_json_file(bridge_dir / _CONFIG_FILE)
    if not isinstance(config, dict):
        raise SystemExit(f"bridge config missing: {bridge_dir / _CONFIG_FILE}")
    token = config.get("token")
    if not isinstance(token, str) or not token:
        raise SystemExit("bridge config missing token")

    notification_queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
    stdout_lock = threading.Lock()
    httpd = _start_http_ingress(bridge_dir, token, notification_queue)
    tools, close_tools = _build_tools(config)
    writer = threading.Thread(
        target=_notification_writer,
        args=(notification_queue, stdout_lock),
        name="claude-native-mcp-writer",
        daemon=True,
    )
    writer.start()
    try:
        _stdio_jsonrpc_loop(tools, stdout_lock, bridge_dir)
    finally:
        notification_queue.put(None)
        httpd.shutdown()
        httpd.server_close()
        close_tools()


def _start_http_ingress(
    bridge_dir: Path,
    token: str,
    notification_queue: queue.Queue[dict[str, Any] | None],
) -> ThreadingHTTPServer:
    """
    Start the localhost control HTTP server.

    Currently only serves ``POST /tools-changed``, which queues a
    standard MCP ``notifications/tools/list_changed`` for the stdio
    writer to emit.

    :param bridge_dir: Bridge directory path.
    :param token: Bearer token used for local requests.
    :param notification_queue: Queue consumed by the MCP stdout
        writer thread.
    :returns: Started :class:`ThreadingHTTPServer`.
    """
    handler_cls = _handler_factory(token, notification_queue)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    host, port = httpd.server_address
    server_info = {
        "url": f"http://{host}:{port}",
        "token": token,
        "pid": os.getpid(),
        "updated_at": time.time(),
    }
    _write_json_file(bridge_dir / _SERVER_FILE, server_info)
    thread = threading.Thread(
        target=httpd.serve_forever,
        name="claude-native-mcp-http",
        daemon=True,
    )
    thread.start()
    return httpd


def _handler_factory(
    token: str,
    notification_queue: queue.Queue[dict[str, Any] | None],
) -> type[BaseHTTPRequestHandler]:
    """
    Create an HTTP handler class bound to the MCP notification queue.

    :param token: Bearer token expected in ``Authorization``.
    :param notification_queue: Queue receiving MCP notification
        payloads.
    :returns: A concrete :class:`BaseHTTPRequestHandler` subclass.
    """

    class _ControlHandler(BaseHTTPRequestHandler):
        """HTTP handler for the local MCP control endpoint."""

        def log_message(self, format: str, *args: Any) -> None:
            """
            Suppress default HTTP server logging.

            :param format: Log format string from
                :class:`BaseHTTPRequestHandler`.
            :param args: Format arguments.
            :returns: None.
            """
            del format, args

        def do_GET(self) -> None:
            """
            Serve the local health endpoint.

            :returns: None.
            """
            if self.path != "/health":
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self._send_json({"status": "ok"})

        def do_POST(self) -> None:
            """
            Accept the local MCP control POST for tools/list_changed.

            :returns: None.
            """
            if self.path != "/tools-changed":
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if self.headers.get("Authorization") != f"Bearer {token}":
                self.send_error(HTTPStatus.UNAUTHORIZED)
                return
            notification_queue.put(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/tools/list_changed",
                    "params": {},
                }
            )
            self._send_json({"ok": True})

        def _send_json(self, payload: dict[str, Any]) -> None:
            """
            Send a JSON response body.

            :param payload: JSON-compatible response object.
            :returns: None.
            """
            raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    return _ControlHandler


def _tool_relay_handler_factory(
    token: str,
    tool_executor: ToolExecutor,
    loop: asyncio.AbstractEventLoop,
) -> type[BaseHTTPRequestHandler]:
    """
    Create an HTTP handler class for active-turn tool calls.

    :param token: Bearer token expected in ``Authorization``.
    :param tool_executor: Existing harness callback used to
        dispatch one tool call.
    :param loop: Event loop that owns ``tool_executor``.
    :returns: A concrete :class:`BaseHTTPRequestHandler` subclass.
    """

    class _ToolRelayHandler(BaseHTTPRequestHandler):
        """HTTP handler for active Omnigent tool relay calls."""

        def log_message(self, format: str, *args: Any) -> None:
            """
            Suppress default HTTP server logging.

            :param format: Log format string from
                :class:`BaseHTTPRequestHandler`.
            :param args: Format arguments.
            :returns: None.
            """
            del format, args

        def do_POST(self) -> None:
            """
            Accept one MCP tool call from the Claude helper process.

            :returns: None.
            """
            if self.path != "/tool":
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if self.headers.get("Authorization") != f"Bearer {token}":
                self.send_error(HTTPStatus.UNAUTHORIZED)
                return
            payload = self._read_json_body()
            if payload is None:
                self.send_error(HTTPStatus.BAD_REQUEST)
                return
            name = payload.get("name")
            arguments = payload.get("arguments")
            if not isinstance(name, str) or not name:
                self._send_json(_mcp_error("tool relay request missing name"))
                return
            if not isinstance(arguments, dict):
                arguments = {}
            self._send_json(_run_relay_tool(tool_executor, loop, name, arguments))

        def _read_json_body(self) -> dict[str, Any] | None:
            """
            Read and decode a JSON request body.

            :returns: Parsed JSON object, or ``None`` when the body
                is malformed.
            """
            length_raw = self.headers.get("Content-Length", "0")
            try:
                length = int(length_raw)
            except ValueError:
                return None
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return None
            return payload if isinstance(payload, dict) else None

        def _send_json(self, payload: dict[str, Any]) -> None:
            """
            Send a JSON response body.

            :param payload: JSON-compatible response object.
            :returns: None.
            """
            raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    return _ToolRelayHandler


def _run_relay_tool(
    tool_executor: ToolExecutor,
    loop: asyncio.AbstractEventLoop,
    name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """
    Execute one relay tool call on the harness event loop.

    :param tool_executor: Existing harness callback used to
        dispatch one tool call.
    :param loop: Event loop that owns ``tool_executor``.
    :param name: Tool name, e.g. ``"sys_os_shell"``.
    :param arguments: Decoded tool arguments.
    :returns: MCP tool-call response.
    """
    future = asyncio.run_coroutine_threadsafe(tool_executor(name, arguments), loop)
    try:
        result = future.result(timeout=_TOOL_CALL_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 - relay converts callback failures to MCP errors.
        return _mcp_error(f"Omnigent tool dispatch failed: {exc}")
    return _mcp_response_from_tool_result(result)


def _mcp_response_from_tool_result(result: Any) -> dict[str, Any]:
    """
    Convert a harness tool result into MCP response shape.

    :param result: Result returned by ``_tool_executor``. Existing
        harnesses usually return a dict, e.g. ``{"result": "ok"}``.
    :returns: MCP tool-call response.
    """
    payload = result if isinstance(result, dict) else {"result": result}
    response: dict[str, Any] = {
        "content": [{"type": "text", "text": json.dumps(payload)}],
    }
    if payload.get("blocked") is True or ("error" in payload and payload.get("error")):
        response["isError"] = True
    return response


def _notification_writer(
    notification_queue: queue.Queue[dict[str, Any] | None],
    stdout_lock: threading.Lock,
) -> None:
    """
    Copy queued MCP notifications to MCP stdout.

    :param notification_queue: Queue populated by the control HTTP
        endpoint.
    :param stdout_lock: Lock protecting JSON-RPC writes to stdout.
    :returns: None after a ``None`` sentinel.
    """
    while True:
        payload = notification_queue.get()
        if payload is None:
            return
        _write_jsonrpc(payload, stdout_lock)


def _stdio_jsonrpc_loop(
    tools: dict[str, Tool],
    stdout_lock: threading.Lock,
    bridge_dir: Path,
) -> None:
    """
    Run the minimal MCP JSON-RPC stdio loop.

    :param tools: Omnigent tools exposed over MCP.
    :param stdout_lock: Lock protecting JSON-RPC writes to stdout.
    :param bridge_dir: Bridge directory path used to read the
        active tool relay.
    :returns: None when stdin reaches EOF.
    """
    use_content_length = False
    while True:
        raw_line = sys.stdin.buffer.readline()
        if raw_line == b"":
            return
        if raw_line.lower().startswith(b"content-length:"):
            use_content_length = True
            try:
                length = int(raw_line.decode("ascii", errors="ignore").split(":", 1)[1].strip())
            except ValueError:
                continue
            while True:
                header = sys.stdin.buffer.readline()
                if header in {b"\r\n", b"\n", b""}:
                    break
            if length <= 0:
                continue
            raw_payload = sys.stdin.buffer.read(length)
            if len(raw_payload) != length:
                return
            line = raw_payload.decode("utf-8", errors="replace").strip()
        else:
            line = raw_line.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict):
            continue
        request_id = message.get("id")
        method = message.get("method")
        if request_id is None or not isinstance(method, str):
            continue
        # Per-request guard: a failure handling ONE request must never tear
        # down the long-lived MCP server (which would surface to Claude Code
        # as ``-32000: Connection closed`` and drop every tool until respawn).
        # Convert any handler exception into a JSON-RPC error response so the
        # offending call fails cleanly and the stdio loop keeps serving. The
        # individual handlers already return ``_mcp_error`` content for
        # expected failures; this catches the unexpected (e.g. a bug in a
        # tool, or an OSError that slipped a narrower except).
        try:
            result = _handle_mcp_request(method, message.get("params"), tools, bridge_dir)
            response: dict[str, Any] = {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": result,
            }
        except Exception as exc:  # noqa: BLE001 - top-level loop guard keeps the server alive.
            response = {
                "jsonrpc": "2.0",
                "id": request_id,
                # -32603 is the JSON-RPC 2.0 "Internal error" code.
                "error": {"code": -32603, "message": f"internal error: {exc}"},
            }
        _write_jsonrpc(response, stdout_lock, framed=use_content_length)


def _handle_mcp_request(
    method: str,
    params: Any,
    tools: dict[str, Tool],
    bridge_dir: Path,
) -> dict[str, Any]:
    """
    Handle one MCP request.

    :param method: JSON-RPC method name, e.g. ``"initialize"``.
    :param params: Request params object.
    :param tools: Omnigent tools exposed over MCP.
    :param bridge_dir: Bridge directory path used to read the
        active tool relay.
    :returns: MCP result object.
    """
    if method == "initialize":
        return {
            "protocolVersion": _MCP_PROTOCOL_VERSION,
            "capabilities": {
                "tools": {"listChanged": True},
            },
            "serverInfo": {
                "name": _MCP_SERVER_NAME,
                "version": "0.1.0",
            },
            "instructions": (
                "Omnigent tools are available as MCP tools when the "
                "active Omnigent turn advertises them; local sys_os_* "
                "tools are available outside an active turn for "
                "workspace file and shell access."
            ),
        }
    if method == "tools/list":
        return {"tools": _combined_mcp_tool_schemas(tools, bridge_dir)}
    if method == "tools/call":
        return _call_mcp_tool(params, tools, bridge_dir)
    if method == "ping":
        return {}
    return {}


def _mcp_tool_schema(tool: Tool) -> dict[str, Any]:
    """
    Convert an Omnigent tool schema into MCP tool-list shape.

    :param tool: Tool instance, e.g. ``SysOsReadTool``.
    :returns: MCP tool descriptor.
    """
    schema = tool.get_schema()["function"]
    return {
        "name": schema["name"],
        "description": schema.get("description", ""),
        "inputSchema": schema.get("parameters", {"type": "object", "properties": {}}),
    }


def _combined_mcp_tool_schemas(
    local_tools: dict[str, Tool],
    bridge_dir: Path,
) -> list[dict[str, Any]]:
    """
    Return local and active-turn relay tools in MCP list shape.

    :param local_tools: Tools the bridge can run directly, e.g.
        ``{"sys_os_read": SysOsReadTool(...)}``.
    :param bridge_dir: Bridge directory path used to read
        ``tool_relay.json``.
    :returns: MCP tool descriptors. Active relay tools override
        local tools with the same name so calls flow through Omnigent and
        appear in the Omnigent event stream during web turns.
    """
    schemas = {name: _mcp_tool_schema(tool) for name, tool in local_tools.items()}
    for tool_spec in _read_relay_tool_specs(bridge_dir):
        name = tool_spec.get("name")
        if not isinstance(name, str) or not name:
            continue
        schemas[name] = _mcp_tool_schema_from_spec(tool_spec)
    return list(schemas.values())


def _mcp_tool_schema_from_spec(tool_spec: dict[str, Any]) -> dict[str, Any]:
    """
    Convert an Omnigent tool schema dict into MCP tool-list shape.

    :param tool_spec: Tool schema from an active harness turn, e.g.
        ``{"name": "sys_os_shell", "parameters": {...}}``.
    :returns: MCP tool descriptor.
    """
    name = tool_spec.get("name")
    description = tool_spec.get("description")
    parameters = tool_spec.get("parameters")
    return {
        "name": name if isinstance(name, str) else "",
        "description": description if isinstance(description, str) else "",
        "inputSchema": parameters if isinstance(parameters, dict) else _empty_object_schema(),
    }


def _call_mcp_tool(
    params: Any,
    tools: dict[str, Tool],
    bridge_dir: Path,
) -> dict[str, Any]:
    """
    Execute one MCP tool call.

    :param params: MCP tool-call params, e.g.
        ``{"name": "sys_os_read", "arguments": {"path": "README.md"}}``.
    :param tools: Omnigent tools exposed over MCP.
    :param bridge_dir: Bridge directory path used to read the
        active tool relay.
    :returns: MCP tool-call result.
    """
    if not isinstance(params, dict):
        return _mcp_error("tool call params must be an object")
    name = params.get("name")
    arguments = params.get("arguments")
    if not isinstance(name, str):
        return _mcp_error(f"unknown tool: {name!r}")
    if not isinstance(arguments, dict):
        arguments = {}
    if name in _read_relay_tool_names(bridge_dir):
        return _call_relay_tool(bridge_dir, name, arguments)
    if name not in tools:
        return _mcp_error(f"unknown tool: {name!r}")
    bridge_config = _read_json_file(bridge_dir / _CONFIG_FILE)
    workspace_raw = bridge_config.get("workspace") if isinstance(bridge_config, dict) else None
    workspace = Path(workspace_raw) if isinstance(workspace_raw, str) and workspace_raw else None
    ctx = ToolContext(
        task_id="claude-native",
        agent_id="claude-native-ui",
        workspace=workspace,
        conversation_id=read_active_session_id(bridge_dir),
    )
    result = tools[name].invoke(json.dumps(arguments), ctx)
    return {"content": [{"type": "text", "text": result}]}


def _read_relay_tool_names(bridge_dir: Path) -> set[str]:
    """
    Return active relay tool names.

    :param bridge_dir: Bridge directory path used to read
        ``tool_relay.json``.
    :returns: Set of tool names currently advertised by the
        per-turn relay, e.g. ``{"sys_terminal_launch"}``.
    """
    return {
        name
        for name in (tool_spec.get("name") for tool_spec in _read_relay_tool_specs(bridge_dir))
        if isinstance(name, str) and name
    }


def _read_relay_tool_specs(bridge_dir: Path) -> list[dict[str, Any]]:
    """
    Return active relay tool schemas.

    :param bridge_dir: Bridge directory path used to read
        ``tool_relay.json``.
    :returns: Normalized tool schema dicts. Missing or malformed
        relay files return an empty list.
    """
    relay = _read_json_file(bridge_dir / _TOOL_RELAY_FILE)
    raw_tools = relay.get("tools") if isinstance(relay, dict) else None
    if not isinstance(raw_tools, list):
        return []
    return [tool for tool in raw_tools if isinstance(tool, dict)]


def _call_relay_tool(
    bridge_dir: Path,
    name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """
    Call the active harness turn's tool relay.

    :param bridge_dir: Bridge directory path used to read
        ``tool_relay.json``.
    :param name: Tool name, e.g. ``"sys_terminal_launch"``.
    :param arguments: Decoded tool arguments.
    :returns: MCP tool-call result.
    """
    relay = _read_json_file(bridge_dir / _TOOL_RELAY_FILE)
    token = relay.get("token")
    url = relay.get("url")
    if not isinstance(token, str) or not isinstance(url, str):
        return _mcp_error("active Omnigent tool relay is missing url/token")
    payload = json.dumps({"name": name, "arguments": arguments}).encode("utf-8")
    req = request.Request(
        f"{url}/tool",
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with request.urlopen(req, timeout=_TOOL_RELAY_POST_TIMEOUT_S) as resp:
            raw = resp.read().decode("utf-8")
            if resp.status >= 400:
                return _mcp_error(f"tool relay POST failed with HTTP {resp.status}")
    # ``OSError`` (the base of ``error.URLError``) also covers the bare
    # timeout / reset errors that ``urlopen`` raises mid-read —
    # ``TimeoutError``, ``socket.timeout``, ``ConnectionResetError`` — which
    # are NOT ``URLError`` instances. Catching the base class returns a clean
    # MCP error for all of them instead of letting the exception propagate up
    # through ``_call_mcp_tool`` → ``_stdio_jsonrpc_loop`` and kill the MCP
    # server.
    except OSError as exc:
        return _mcp_error(f"failed to call Omnigent tool relay: {exc}")
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return _mcp_error("Omnigent tool relay returned malformed JSON")
    if not isinstance(decoded, dict):
        return _mcp_error("Omnigent tool relay returned non-object JSON")
    return decoded


def _mcp_error(message: str) -> dict[str, Any]:
    """
    Build an MCP error-content tool result.

    :param message: Human-readable error message.
    :returns: MCP tool-call result marked as an error.
    """
    return {"content": [{"type": "text", "text": json.dumps({"error": message})}], "isError": True}


def _normalize_relay_tool_specs(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Normalize active-turn tool schemas before advertising them.

    :param tools: Tool schema dicts from the harness request, e.g.
        ``[{"name": "sys_os_read", "parameters": {...}}]``.
    :returns: Schemas containing only fields the MCP bridge needs.
    """
    normalized: list[dict[str, Any]] = []
    for tool in tools:
        name = tool.get("name")
        if not isinstance(name, str) or not name:
            continue
        description = tool.get("description")
        parameters = tool.get("parameters")
        normalized.append(
            {
                "name": name,
                "description": description if isinstance(description, str) else "",
                "parameters": (
                    parameters if isinstance(parameters, dict) else _empty_object_schema()
                ),
            }
        )
    return normalized


def _empty_object_schema() -> dict[str, Any]:
    """
    Return a minimal JSON object schema.

    :returns: ``{"type": "object", "properties": {}}``.
    """
    return {"type": "object", "properties": {}}


def _build_tools(config: dict[str, Any]) -> tuple[dict[str, Tool], Callable[[], None]]:
    """
    Build Omnigent MCP tools served by the bridge.

    :param config: Bridge config JSON object.
    :returns: ``(tools, close_tools)`` where ``close_tools``
        releases any helper processes.
    """
    workspace_raw = config.get("workspace")
    workspace = Path(workspace_raw) if isinstance(workspace_raw, str) and workspace_raw else None
    os_env: OSEnvironment | None = None
    if workspace is not None:
        spec = OSEnvSpec(
            type="caller_process",
            cwd=str(workspace),
            sandbox=OSEnvSandboxSpec(type="none"),
            fork=False,
        )
        os_env = create_os_environment(spec)
    tools = {tool.name(): tool for tool in build_os_env_tools(os_env)} if os_env else {}

    def _close_tools() -> None:
        """Close helper resources owned by this bridge server."""
        if os_env is not None:
            os_env.close()

    return tools, _close_tools


def _write_jsonrpc(
    payload: dict[str, Any],
    stdout_lock: threading.Lock,
    *,
    framed: bool = False,
) -> None:
    """
    Write one JSON-RPC message to stdout.

    :param payload: JSON-RPC object to serialize.
    :param stdout_lock: Lock protecting stdout.
    :param framed: When ``True``, write MCP ``Content-Length`` framed output.
    :returns: None.
    """
    raw = json.dumps(payload, separators=(",", ":"))
    with stdout_lock:
        if framed:
            encoded = raw.encode("utf-8")
            sys.stdout.buffer.write(f"Content-Length: {len(encoded)}\r\n\r\n".encode("ascii"))
            sys.stdout.buffer.write(encoded)
            sys.stdout.buffer.flush()
        else:
            print(raw, flush=True)


def _model_from_transcript_entry(entry: dict[str, Any]) -> str | None:
    """
    Return ``message.model`` from an assistant transcript record.

    Surfaced on :class:`TranscriptReadResult.latest_model` for
    diagnostics. The ring's denominator comes from the statusLine
    stdin (see :func:`read_claude_context_state`); the JSONL model
    name is no longer used to size the ring.

    :param entry: One decoded transcript JSONL record.
    :returns: API model name, or ``None`` for non-assistant entries
        and entries missing the field.
    """
    message = entry.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return None
    model = message.get("model")
    if isinstance(model, str) and model:
        return model
    return None


def read_claude_context_state(bridge_dir: Path) -> dict[str, Any] | None:
    """
    Read the most recent statusLine snapshot from ``context.json``.

    Written atomically by :mod:`omnigent.claude_native_status` each
    time Claude Code invokes the wrapped statusLine command. The file
    is the authoritative source for both the ring's denominator
    (``context_window_size`` — Claude Code knows the real window for
    the active model and beta tier) and an optional fresh
    ``current_usage`` block.

    :param bridge_dir: Bridge directory shared with the forwarder.
    :returns: Parsed dict with keys ``context_window_size`` (int) and
        optionally ``current_usage`` (dict). ``None`` when the file
        doesn't exist yet, is unreadable, or doesn't carry a usable
        window — the forwarder treats that as "no update".
    """
    path = bridge_dir / _CONTEXT_FILE
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    size = parsed.get("context_window_size")
    if not isinstance(size, int) or size <= 0:
        return None
    return parsed


def read_claude_status_model(bridge_dir: Path) -> str | None:
    """
    Read the active model id from the statusLine snapshot ``context.json``.

    Unlike :func:`read_claude_context_state` (which returns ``None`` unless a
    usable ``context_window_size`` is present, since it backs the context
    ring), this returns the model whenever the wrapper captured one — the
    model and the window are written independently, and the cost-budget gate
    needs the model even on a render where the window field was absent. This
    is claude-native's race-free, gate-time source of the live ``/model``
    selection (the analogue of the codex hook reading ``config.toml``).

    :param bridge_dir: Bridge directory shared with the statusLine wrapper.
    :returns: The model id, e.g. ``"claude-sonnet-4-6"``, or ``None`` when
        the file is missing / unreadable / carries no model string.
    """
    path = bridge_dir / _CONTEXT_FILE
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    model = parsed.get("model")
    return model if isinstance(model, str) and model else None


def read_user_status_line_command() -> str | None:
    """
    Return the user's globally-configured statusLine shell command, if any.

    We override Claude Code's statusLine in our per-session ``--settings``
    to capture ``context_window`` stdin. To avoid breaking the user's
    pre-existing status bar (typically claude-hud), the wrapper chains
    to whatever they had configured globally.

    :returns: The command string from
        ``~/.claude/settings.json``'s ``statusLine.command``, or
        ``None`` when no global statusLine is configured / readable.
    """
    try:
        raw = _USER_CLAUDE_SETTINGS_PATH.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    status_line = parsed.get("statusLine")
    if not isinstance(status_line, dict):
        return None
    command = status_line.get("command")
    if isinstance(command, str) and command.strip():
        return command
    return None


def read_user_effort_level() -> str | None:
    """
    Return the user's configured Claude Code effort level, if any.

    Read client-side from ``effortLevel`` in ``~/.claude/settings.json`` —
    the level the wrapped ``claude`` actually runs at (we pass no ``--effort``).

    :returns: A recognized effort, e.g. ``"medium"``; ``None`` when unset,
        unreadable, or not a valid Claude effort (fail-soft, never blocks launch).
    """
    try:
        raw = _USER_CLAUDE_SETTINGS_PATH.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    effort = parsed.get("effortLevel")
    if isinstance(effort, str) and effort in CLAUDE_EFFORTS:
        return effort
    return None


def _usage_from_transcript_entry(entry: dict[str, Any]) -> dict[str, int] | None:
    """
    Extract token-usage from one Claude assistant transcript entry.

    ``context_tokens`` is ``input + cache_creation + cache_read`` — the
    bytes that will reappear in the next call's prompt. Output tokens
    are reported separately since they don't shift the prompt forward.

    :param entry: One decoded transcript JSONL record.
    :returns: ``{"context_tokens", "input_tokens", "output_tokens"}``
        when the record is an assistant entry with usage; ``None``
        otherwise.
    """
    message = entry.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return None
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    cache_creation = usage.get("cache_creation_input_tokens")
    cache_read = usage.get("cache_read_input_tokens")
    if not isinstance(input_tokens, int):
        return None
    if not isinstance(output_tokens, int):
        output_tokens = 0
    cc = cache_creation if isinstance(cache_creation, int) else 0
    cr = cache_read if isinstance(cache_read, int) else 0
    result: dict[str, int] = {
        "context_tokens": input_tokens + cc + cr,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }
    if cc:
        result["cache_creation_input_tokens"] = cc
    if cr:
        result["cache_read_input_tokens"] = cr
    return result


def _assistant_text_from_transcript_line(line: str) -> str | None:
    """
    Extract assistant text from one Claude transcript JSONL line.

    :param line: Raw JSONL record.
    :returns: Assistant text, or ``None`` when the record is not an
        assistant text message.
    """
    try:
        entry = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(entry, dict):
        return None
    message = entry.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
    return "".join(parts) or None


def _transcript_items_from_entry(
    entry: dict[str, Any],
    *,
    line_number: int,
    record_offset: int | None = None,
    agent_name: str,
    current_response_id: str | None,
    include_sidechains: bool = False,
) -> tuple[str | None, list[ClaudeTranscriptItem]]:
    """
    Convert one Claude transcript entry into Omnigent conversation items.

    :param entry: Decoded JSON object from one transcript line.
    :param line_number: One-based transcript line number.
    :param record_offset: Byte offset where the transcript record
        starts. Used for stable fallback source ids when Claude omits
        ``uuid`` and ``requestId``.
    :param agent_name: Agent/model name for assistant/tool items.
    :param current_response_id: Response id for the active Claude
        assistant turn, if a previous poll already started one.
    :param include_sidechains: When ``False`` (the default) any record
        with ``isSidechain: true`` is dropped — that's the right
        behavior when reading the parent's main transcript, where
        sub-agent records are inlined as sidechains and must not
        appear in the parent's Omnigent conversation. When ``True`` the
        flag is ignored — required when reading a sub-agent's own
        ``agent-<id>.jsonl`` (every record there is a sidechain by
        definition) so the sub-agent's items reach the child AP
        conversation. Caller is responsible for matching the flag to
        the file shape.
    :returns: Updated active response id and parsed items.
    """
    if not include_sidechains and entry.get("isSidechain") is True:
        return current_response_id, []
    if entry.get("type") == "attachment":
        return _attachment_transcript_items_from_entry(
            entry,
            line_number=line_number,
            record_offset=record_offset,
            current_response_id=current_response_id,
        )
    if entry.get("subtype") == "local_command":
        return _local_command_transcript_items_from_entry(
            entry,
            line_number=line_number,
            record_offset=record_offset,
            current_response_id=current_response_id,
        )
    message = entry.get("message")
    if not isinstance(message, dict):
        return current_response_id, []
    role = message.get("role")
    if role == "user":
        return _user_transcript_items_from_entry(
            entry,
            line_number=line_number,
            record_offset=record_offset,
            agent_name=agent_name,
            current_response_id=current_response_id,
        )
    if role == "assistant":
        return _assistant_transcript_items_from_entry(
            entry,
            line_number=line_number,
            record_offset=record_offset,
            agent_name=agent_name,
            current_response_id=current_response_id,
        )
    return current_response_id, []


def _attachment_transcript_items_from_entry(
    entry: dict[str, Any],
    *,
    line_number: int,
    record_offset: int | None,
    current_response_id: str | None,
) -> tuple[str | None, list[ClaudeTranscriptItem]]:
    """
    Parse user-visible Claude attachment transcript entries.

    Claude records prompts typed while an assistant turn is busy as
    ``attachment.type == "queued_command"`` rather than a normal
    ``role=user`` message. Treat prompt-mode queued commands as user
    messages so interruption inputs such as ``"STOP"`` appear in the
    Omnigent transcript and reset the active assistant response.

    :param entry: Decoded Claude transcript record.
    :param line_number: One-based transcript line number.
    :param record_offset: Byte offset where the transcript record
        starts, or ``None`` for legacy line-cursor reads.
    :param current_response_id: Response id for the active assistant
        turn. Ignored attachment metadata preserves this value.
    :returns: Updated active response id and parsed items.
    """
    attachment = entry.get("attachment")
    if not isinstance(attachment, dict):
        return current_response_id, []
    if attachment.get("type") != "queued_command":
        return current_response_id, []
    if attachment.get("commandMode") != "prompt":
        return current_response_id, []
    prompt = attachment.get("prompt")
    if not isinstance(prompt, str) or not prompt:
        return current_response_id, []
    source_key = _transcript_source_key(entry, line_number, record_offset)
    item = ClaudeTranscriptItem(
        source_id=_source_id(source_key, 0, "message"),
        item_type="message",
        data={
            "role": "user",
            "content": [{"type": "input_text", "text": prompt}],
        },
        response_id=_response_id_from_source(source_key),
    )
    return None, [item]


# Slash-command marker handling. Claude Code's embedded TUI emits
# multiple ``role=user`` records per operator action: a ``<command-
# name>`` echo, a sibling ``isMeta=true`` ``<local-command-caveat>``,
# a follow-up ``<local-command-stdout>`` (and friends), plus
# ``<bash-*>`` records when the operator types ``!cmd``. All are
# CLI scaffolding, not user-typed content — rendering any of them as
# a user bubble shows raw markup to a web viewer.
# Today: drop isMeta + every CLI-scaffolding-prefixed record; for
# ``<command-name>`` records also surface Skills as ``slash_command``
# items. The original blanket drop was reverted because it
# hid Skills; we keep the broad scaffolding filter and just
# selectively re-surface the Skill case.
_COMMAND_NAME_RE = re.compile(r"<command-name>(.*?)</command-name>", re.DOTALL)
_COMMAND_ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.DOTALL)
_COMMAND_STDOUT_RE = re.compile(r"<local-command-stdout>(.*?)</local-command-stdout>", re.DOTALL)
_BASH_INPUT_RE = re.compile(r"<bash-input>(.*?)</bash-input>", re.DOTALL)
_BASH_STDOUT_RE = re.compile(r"<bash-stdout>(.*?)</bash-stdout>", re.DOTALL)
_BASH_STDERR_RE = re.compile(r"<bash-stderr>(.*?)</bash-stderr>", re.DOTALL)

# Markers that prefix a ``role=user`` record produced by Claude
# Code's CLI scaffolding (not user-typed content). ``<command-
# name>`` is handled separately by the slash-command parser;
# ``<bash-*>`` records are handled separately as ``terminal_command``
# items; the rest must always drop.
_CLI_SCAFFOLDING_MARKERS: tuple[str, ...] = (
    "<command-message>",
    "<command-args>",
    "<local-command-caveat>",
    "<local-command-stdout>",
    "<local-command-stderr>",
)

# Claude Code's CLI built-ins (no leading ``/``). Each name is
# classified as either:
#
# - DROPPED — pure UI affordances (``/help``, ``/login``) and local
#   config (``/permissions``, ``/add-dir``). No conversation-visible
#   effect; surfacing them in the web UI is noise.
# - SURFACED — commands that change the next turn's behavior or the
#   conversation state (``/effort high``, ``/clear``, ``/compact``,
#   ``/model``, ``/ultrareview``). A web observer needs to see these,
#   otherwise the next assistant turn appears to shift unprompted.
#
# Unknown names fall through to the Skill branch — a safer default
# than silently hiding them.
_CLAUDE_CLI_DROPPED_COMMANDS: frozenset[str] = frozenset(
    {
        "add-dir",
        "agents",
        "bug",
        "config",
        "cost",
        "doctor",
        "exit",
        "fast",
        "feedback",
        "help",
        "hooks",
        "ide",
        "login",
        "logout",
        "mcp",
        "memory",
        "onboarding",
        "permissions",
        "plugin",
        "quiet",
        "quit",
        "release-notes",
        "resume",
        "save",
        "status",
        "terminal-setup",
        "upgrade",
        "verbose",
    }
)
_CLAUDE_CLI_SURFACED_COMMANDS: frozenset[str] = frozenset(
    {
        "clear",
        "compact",
        "effort",
        "model",
        "ultrareview",
    }
)


@dataclass(frozen=True)
class _SlashCommandPayload:
    """
    Parsed content of a slash-command ``role=user`` transcript record.

    :param name: Command name with leading ``/`` stripped, e.g.
        ``"dev-productivity:simplify"``.
    :param arguments: Verbatim ``<command-args>`` text; empty when none.
    :param output: Verbatim ``<local-command-stdout>`` text, or ``None``.
    """

    name: str
    arguments: str
    output: str | None


def _parse_slash_command_record(content: str) -> _SlashCommandPayload | None:
    """
    Parse a Claude Code slash-command marker blob.

    Returns ``None`` on a missing/empty/unclosed ``<command-name>``
    tag rather than raising — a single corrupt JSONL line must not
    kill the transcript poll loop.

    :param content: ``message.content`` string from a ``role=user``
        Claude Code JSONL record.
    :returns: Parsed payload, or ``None`` when no name could be
        extracted.
    """
    name_match = _COMMAND_NAME_RE.search(content)
    if name_match is None:
        return None
    raw_name = name_match.group(1).strip()
    if not raw_name:
        return None
    # Strip leading ``/`` so renderers can add their own prefix without double-rendering.
    name = raw_name.lstrip("/")
    if not name:
        return None
    args_match = _COMMAND_ARGS_RE.search(content)
    arguments = args_match.group(1).strip() if args_match else ""
    stdout_match = _COMMAND_STDOUT_RE.search(content)
    output = stdout_match.group(1) if stdout_match else None
    return _SlashCommandPayload(name=name, arguments=arguments, output=output)


def _local_command_transcript_items_from_entry(
    entry: dict[str, Any],
    *,
    line_number: int,
    record_offset: int | None,
    current_response_id: str | None,
) -> tuple[str | None, list[ClaudeTranscriptItem]]:
    """
    Parse a top-level Claude ``local_command`` transcript entry.

    Newer Claude Code builds can record shell-mode ``!cmd`` activity
    as top-level transcript records with ``subtype="local_command"``
    and a string ``content`` field instead of wrapping the same markup
    inside ``message.role=user``. Only ``<bash-*>`` records are
    conversation-visible here; slash-command local records are still
    handled by hook/fork detection and otherwise ignored.

    :param entry: Decoded Claude transcript record.
    :param line_number: One-based transcript line number.
    :param record_offset: Byte offset where the transcript record
        starts, or ``None`` for legacy line-cursor reads.
    :param current_response_id: Response id for an in-progress shell
        command group, if the input record was parsed in an earlier
        line.
    :returns: Updated active response id and parsed terminal-command
        items.
    """
    content = entry.get("content")
    if not isinstance(content, str) or not content:
        return current_response_id, []
    source_key = _transcript_source_key(entry, line_number, record_offset)
    fallback_response_id = _response_id_from_source(source_key)
    response_id = (
        fallback_response_id
        if _BASH_INPUT_RE.search(content) is not None
        else current_response_id or fallback_response_id
    )
    items = _terminal_command_items_from_content(
        content,
        source_key=source_key,
        response_id=response_id,
    )
    if not items:
        return current_response_id, []
    return response_id, items


def _terminal_command_items_from_content(
    content: str,
    *,
    source_key: str,
    response_id: str,
) -> list[ClaudeTranscriptItem]:
    """
    Parse Claude shell-mode markup into terminal-command items.

    Claude may emit shell input and output as separate records or as
    one record containing multiple ``<bash-*>`` tags. This helper
    emits at most one input item and one output item, preserving their
    order in the source record and giving both the same response id so
    the server transcript groups an invocation with its result.

    :param content: Transcript markup, e.g.
        ``"<bash-input>pwd</bash-input><bash-stdout>/tmp</bash-stdout>"``.
    :param source_key: Base transcript record key used to construct
        source ids, e.g. ``"rec_abc123"``.
    :param response_id: Synthetic response id for this terminal
        command group, e.g. ``"resp_claude_abc123"``.
    :returns: Parsed ``terminal_command`` items. Empty when no shell
        markers are present.
    """
    if not any(marker in content for marker in ("<bash-input>", "<bash-stdout>", "<bash-stderr>")):
        return []
    input_match = _BASH_INPUT_RE.search(content)
    stdout_match = _BASH_STDOUT_RE.search(content)
    stderr_match = _BASH_STDERR_RE.search(content)
    items: list[ClaudeTranscriptItem] = []
    item_index = 0
    if input_match is not None:
        items.append(
            ClaudeTranscriptItem(
                source_id=_source_id(source_key, item_index, "terminal_command"),
                item_type="terminal_command",
                data={"kind": "input", "input": input_match.group(1)},
                response_id=response_id,
            )
        )
        item_index += 1
    if stdout_match is not None or stderr_match is not None:
        items.append(
            ClaudeTranscriptItem(
                source_id=_source_id(source_key, item_index, "terminal_command"),
                item_type="terminal_command",
                data={
                    "kind": "output",
                    "stdout": stdout_match.group(1) if stdout_match is not None else None,
                    "stderr": stderr_match.group(1) if stderr_match is not None else None,
                },
                response_id=response_id,
            )
        )
    return items


def _user_transcript_items_from_entry(
    entry: dict[str, Any],
    *,
    line_number: int,
    record_offset: int | None,
    agent_name: str,
    current_response_id: str | None,
) -> tuple[str | None, list[ClaudeTranscriptItem]]:
    """
    Parse a Claude ``role=user`` transcript entry.

    :param entry: Decoded Claude transcript record.
    :param line_number: One-based transcript line number.
    :param record_offset: Byte offset where the transcript record
        starts, or ``None`` for legacy line-cursor reads.
    :param agent_name: Agent/model name attached to ``slash_command``
        items so the web UI can attribute the invocation.
    :param current_response_id: Response id for the active assistant
        turn; tool results keep this id.
    :returns: Updated active response id and parsed user/tool-result
        items.
    """
    # ``isMeta=true`` carries CLI scaffolding like
    # ``<local-command-caveat>``; no user-visible content.
    if entry.get("isMeta") is True:
        return current_response_id, []
    message = entry["message"]
    content = message.get("content") if isinstance(message, dict) else None
    source_key = _transcript_source_key(entry, line_number, record_offset)
    fallback_response_id = _response_id_from_source(source_key)
    items: list[ClaudeTranscriptItem] = []

    if isinstance(content, str):
        if not content:
            return current_response_id, []
        stripped = content.lstrip()
        # Skill invocations with args ship the tag order
        # ``<command-message>…<command-name>…<command-args>…`` — i.e.
        # ``<command-name>`` is NOT the first tag. Detect it anywhere
        # in the content, not just at the start.
        if "<command-name>" in stripped:
            payload = _parse_slash_command_record(content)
            # Drop unparseable markup rather than letting it fall through
            # to the user-bubble path — that rendered the markup verbatim
            # in the original bug.
            if payload is None or payload.name in _CLAUDE_CLI_DROPPED_COMMANDS:
                return current_response_id, []
            kind = "command" if payload.name in _CLAUDE_CLI_SURFACED_COMMANDS else "skill"
            data: dict[str, Any] = {
                "agent": agent_name,
                "kind": kind,
                "name": payload.name,
                "arguments": payload.arguments,
            }
            if payload.output is not None:
                data["output"] = payload.output
            items.append(
                ClaudeTranscriptItem(
                    source_id=_source_id(source_key, 0, "slash_command"),
                    item_type="slash_command",
                    data=data,
                    response_id=fallback_response_id,
                )
            )
            # Slash command opens a new logical turn; subsequent
            # assistant text must inherit this id so it clusters with
            # the indicator, not the prior bubble.
            return fallback_response_id, items
        # ``!cmd`` terminal commands may arrive here in older Claude
        # builds; newer builds use top-level ``local_command`` records.
        # In both shapes, surface the command and result as their own
        # transcript group instead of inheriting the previous assistant
        # response id.
        terminal_response_id = (
            fallback_response_id
            if _BASH_INPUT_RE.search(content) is not None
            else current_response_id or fallback_response_id
        )
        terminal_items = _terminal_command_items_from_content(
            content,
            source_key=source_key,
            response_id=terminal_response_id,
        )
        if terminal_items:
            return terminal_response_id, terminal_items
        # Other CLI-scaffolding records (stdout/stderr from /effort, etc.)
        # arrive as standalone ``role=user`` records and must drop instead
        # of leaking as user bubbles.
        if any(stripped.startswith(m) for m in _CLI_SCAFFOLDING_MARKERS):
            return current_response_id, []
        items.append(
            ClaudeTranscriptItem(
                source_id=_source_id(source_key, 0, "message"),
                item_type="message",
                data={
                    "role": "user",
                    "content": [{"type": "input_text", "text": content}],
                },
                response_id=fallback_response_id,
            )
        )
        return None, items

    if not isinstance(content, list):
        return current_response_id, []

    user_blocks: list[dict[str, Any]] = []
    saw_user_text = False
    item_index = 0
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if not isinstance(text, str) or not text:
                continue
            # Defensively guard against slash-command markup or other
            # CLI-scaffolding markers ever arriving in list-form
            # content. Today these only ship in string content (the
            # branch above), but Claude Code's JSONL format is not
            # under our control — without this filter, a format
            # change would regress to rendering ``<command-name>…``
            # markup as a user bubble.
            stripped = text.lstrip()
            if "<command-name>" in stripped or any(
                stripped.startswith(m) for m in _CLI_SCAFFOLDING_MARKERS
            ):
                continue
            user_blocks.append({"type": "input_text", "text": text})
            saw_user_text = True
            continue
        if block_type != "tool_result":
            continue
        call_id = block.get("tool_use_id")
        if not isinstance(call_id, str) or not call_id:
            continue
        response_id = current_response_id or _response_id_from_source(
            _parent_or_record_source_key(entry, line_number, record_offset)
        )
        items.append(
            ClaudeTranscriptItem(
                source_id=_source_id(source_key, item_index, "function_call_output"),
                item_type="function_call_output",
                data={
                    "call_id": call_id,
                    "output": _tool_result_output(entry, block),
                },
                response_id=response_id,
            )
        )
        item_index += 1

    if user_blocks:
        items.insert(
            0,
            ClaudeTranscriptItem(
                source_id=_source_id(source_key, item_index, "message"),
                item_type="message",
                data={
                    "role": "user",
                    "content": user_blocks,
                },
                response_id=fallback_response_id,
            ),
        )
    return (None if saw_user_text else current_response_id), items


def _assistant_transcript_items_from_entry(
    entry: dict[str, Any],
    *,
    line_number: int,
    record_offset: int | None,
    agent_name: str,
    current_response_id: str | None,
) -> tuple[str | None, list[ClaudeTranscriptItem]]:
    """
    Parse a Claude ``role=assistant`` transcript entry.

    :param entry: Decoded Claude transcript record.
    :param line_number: One-based transcript line number.
    :param record_offset: Byte offset where the transcript record
        starts, or ``None`` for legacy line-cursor reads.
    :param agent_name: Agent/model name for assistant/tool items.
    :param current_response_id: Response id for the active Claude
        assistant turn.
    :returns: Updated active response id and parsed assistant/tool
        items.
    """
    message = entry["message"]
    content = message.get("content") if isinstance(message, dict) else None
    source_key = _transcript_source_key(entry, line_number, record_offset)
    response_id = current_response_id or _response_id_from_source(source_key)
    items: list[ClaudeTranscriptItem] = []

    if isinstance(content, str):
        if content:
            items.append(
                _assistant_message_item(
                    source_key=source_key,
                    item_index=0,
                    agent_name=agent_name,
                    response_id=response_id,
                    text=content,
                )
            )
        return response_id, items

    if not isinstance(content, list):
        return current_response_id, []

    for item_index, block in enumerate(content):
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                items.append(
                    _assistant_message_item(
                        source_key=source_key,
                        item_index=item_index,
                        agent_name=agent_name,
                        response_id=response_id,
                        text=text,
                    )
                )
            continue
        if block_type == "tool_use":
            tool_id = block.get("id")
            name = block.get("name")
            if not isinstance(tool_id, str) or not tool_id:
                continue
            if not isinstance(name, str) or not name:
                continue
            arguments = block.get("input")
            if not isinstance(arguments, dict):
                arguments = {}
            items.append(
                ClaudeTranscriptItem(
                    source_id=_source_id(source_key, item_index, "function_call"),
                    item_type="function_call",
                    data={
                        "agent": agent_name,
                        "name": name,
                        "arguments": json.dumps(arguments, separators=(",", ":")),
                        "call_id": tool_id,
                    },
                    response_id=response_id,
                )
            )
    return response_id if items else current_response_id, items


def _assistant_message_item(
    *,
    source_key: str,
    item_index: int,
    agent_name: str,
    response_id: str,
    text: str,
) -> ClaudeTranscriptItem:
    """
    Build an assistant message item from one Claude text block.

    :param source_key: Base transcript record key.
    :param item_index: Content block index inside the record.
    :param agent_name: Agent/model name for the assistant message.
    :param response_id: Response id grouping the Claude turn.
    :param text: Assistant text block.
    :returns: Parsed transcript item.
    """
    return ClaudeTranscriptItem(
        source_id=_source_id(source_key, item_index, "message"),
        item_type="message",
        data={
            "role": "assistant",
            "agent": agent_name,
            "content": [{"type": "output_text", "text": text}],
        },
        response_id=response_id,
    )


def _tool_result_output(entry: dict[str, Any], block: dict[str, Any]) -> str:
    """
    Return the UI-facing output string for a Claude tool result.

    :param entry: Decoded Claude transcript record containing
        optional ``toolUseResult`` metadata.
    :param block: ``tool_result`` content block from ``message``.
    :returns: String output for a ``function_call_output`` item.
    """
    content = block.get("content")
    if isinstance(content, str):
        return content
    if content is not None:
        return json.dumps(content, separators=(",", ":"))
    tool_use_result = entry.get("toolUseResult")
    if isinstance(tool_use_result, str):
        return tool_use_result
    if tool_use_result is not None:
        return json.dumps(tool_use_result, separators=(",", ":"))
    return ""


def _transcript_source_key(
    entry: dict[str, Any],
    line_number: int,
    record_offset: int | None = None,
) -> str:
    """
    Return the stable key for a Claude transcript record.

    :param entry: Decoded Claude transcript record.
    :param line_number: One-based transcript line number.
    :param record_offset: Byte offset where the transcript record
        starts, or ``None`` when unavailable.
    :returns: Claude UUID/request id, byte-offset fallback, or a
        legacy line-number fallback.
    """
    for key in ("uuid", "requestId"):
        value = entry.get(key)
        if isinstance(value, str) and value:
            return value
    if record_offset is not None:
        return f"byte-{record_offset}"
    return f"line-{line_number}"


def _parent_or_record_source_key(
    entry: dict[str, Any],
    line_number: int,
    record_offset: int | None = None,
) -> str:
    """
    Return a parent key for tool results when Claude supplies one.

    :param entry: Decoded Claude transcript record.
    :param line_number: One-based transcript line number.
    :param record_offset: Byte offset where the transcript record
        starts, or ``None`` when unavailable.
    :returns: Parent UUID when present, otherwise the record key.
    """
    parent = entry.get("parentUuid")
    if isinstance(parent, str) and parent:
        return parent
    return _transcript_source_key(entry, line_number, record_offset)


def _response_id_from_source(source: str) -> str:
    """
    Derive a deterministic Omnigent response id from a Claude source key.

    :param source: Claude UUID/request id/line key.
    :returns: String id with the standard ``resp_`` prefix.
    """
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:32]
    return f"resp_claude_{digest}"


def _source_id(source_key: str, item_index: int, item_type: str) -> str:
    """
    Build a per-item idempotency key for a transcript-derived item.

    :param source_key: Base Claude record key.
    :param item_index: Content block index inside the record.
    :param item_type: Omnigent item type.
    :returns: Stable source id string.
    """
    return f"{source_key}:{item_index}:{item_type}"


def _wait_for_server_info(bridge_dir: Path, *, timeout_s: float) -> dict[str, Any]:
    """
    Wait for the bridge control HTTP endpoint file.

    :param bridge_dir: Bridge directory path.
    :param timeout_s: Seconds to wait, e.g. ``30.0``.
    :returns: Parsed server-info JSON object.
    :raises RuntimeError: If the server file never appears.
    """
    deadline = time.monotonic() + timeout_s
    path = bridge_dir / _SERVER_FILE
    while time.monotonic() < deadline:
        payload = _read_json_file(path)
        if isinstance(payload, dict) and payload.get("url") and payload.get("token"):
            return payload
        time.sleep(0.05)
    raise RuntimeError(
        "Claude native bridge is not ready yet. Wait for Claude Code "
        "startup to finish before notifying tool list changes."
    )


def _read_json_file(path: Path) -> dict[str, Any]:
    """
    Read a JSON object file.

    :param path: JSON file path.
    :returns: Parsed object, or ``{}`` when missing/malformed.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json_file(path: Path, payload: dict[str, Any]) -> None:
    """
    Atomically write a JSON object file with owner-only permissions.

    :param path: JSON file path.
    :param payload: JSON-compatible object.
    :returns: None.
    """
    _ensure_secure_dir(path.parent)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
            handle.write(json.dumps(payload, separators=(",", ":")))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    finally:
        if tmp_path is not None:
            with contextlib.suppress(FileNotFoundError):
                tmp_path.unlink()


if __name__ == "__main__":
    main()
