"""Stdlib-only readers for the Claude-native bridge directory's state files.

Claude Code blocks on every command hook it spawns, and each spawn is a fresh
interpreter, so :mod:`omnigent.harnesses.claude_native.hook` reads bridge state
through this module rather than :mod:`omnigent.harnesses.claude_native.bridge`,
whose import pulls in asyncio, filelock, the MCP relay's HTTP server and the
tool graph. Keep this module free of anything heavier than the standard
library; the import-graph guards in the hook tests pin it.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from datetime import datetime
from pathlib import Path

from omnigent.util.json_types import JsonObject as _JsonObject

CLAUDE_FRAMEWORK_CONTEXT_FILE = "pending_framework_context.txt"
BRIDGE_ID_LABEL_KEY = "omnigent.claude_native.bridge_id"
_CONFIG_FILE = "bridge.json"
_STATE_FILE = "state.json"
_PERMISSION_HOOK_FILE = "permission_hook.json"
_CONTEXT_FILE = "context.json"
_RECENT_LOCAL_COMMAND_LINE_LIMIT = 200
_RECENT_LOCAL_COMMAND_WINDOW_S = 10.0
_FORKED_FROM_LINE_LIMIT = 200
_COMMAND_NAME_RE = re.compile(r"<command-name>(.*?)</command-name>", re.DOTALL)


def _read_json_file(path: Path) -> _JsonObject:
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


def read_active_session_id(bridge_dir: Path) -> str | None:
    """
    Read the Omnigent session currently receiving bridge-originated events.

    :param bridge_dir: Bridge directory path.
    :returns: Active Omnigent session id, e.g. ``"conv_abc123"``, or
        ``None`` when the bridge config is absent or malformed.
    """
    config = _read_json_file(bridge_dir / _CONFIG_FILE)
    active = config.get("active_session_id")
    if isinstance(active, str) and active:
        return active
    legacy = config.get("conversation_id")
    return legacy if isinstance(legacy, str) and legacy else None


def read_bridge_id(bridge_dir: Path) -> str | None:
    """
    Read the opaque bridge id from bridge config.

    :param bridge_dir: Bridge directory path.
    :returns: Opaque bridge id, e.g. ``"bridge_abc123"``, or
        ``None`` when the bridge config is absent or malformed.
    """
    config = _read_json_file(bridge_dir / _CONFIG_FILE)
    bridge_id = config.get("bridge_id")
    return bridge_id if isinstance(bridge_id, str) and bridge_id else None


def read_permission_hook_config(bridge_dir: Path) -> _JsonObject:
    """
    Read Omnigent routing details for the permission command hook.

    :param bridge_dir: Bridge directory path.
    :returns: Permission hook config, e.g.
        ``{"ap_server_url": "http://127.0.0.1:8787",
        "ap_auth_headers": {"Authorization": "Bearer token"}}``.
        Empty dict when the file is absent or malformed.
    """
    return _read_json_file(bridge_dir / _PERMISSION_HOOK_FILE)


def read_claude_session_id(bridge_dir: Path) -> str | None:
    """
    Return the Claude-native session id captured from hook events.

    Set by :func:`omnigent.harnesses.claude_native.bridge.record_hook_event`
    whenever a hook payload carries a ``session_id`` field (every event Claude
    Code emits does). Wrapper code reads it back to mirror the value into
    AP-side conversation state (e.g. ``external_session_id`` on the
    ``conversations`` row) so ``--resume`` can recover the prior Claude
    transcript on a fresh runner without the user having to know Claude's own
    id.

    :param bridge_dir: Bridge directory path.
    :returns: Claude session uuid string,
        e.g. ``"a1b2c3d4-1234-5678-9abc-def012345678"``, or ``None``
        when no hook has yet reported one (the first poll after a
        cold launch).
    """
    raw = _read_json_file(bridge_dir / _STATE_FILE).get("claude_session_id")
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
    seen: set[str] = set()
    raw_seen = state.get("seen_claude_session_ids")
    if isinstance(raw_seen, list):
        seen.update(value for value in raw_seen if isinstance(value, str) and value)
    raw_current = state.get("claude_session_id")
    if isinstance(raw_current, str) and raw_current:
        seen.add(raw_current)
    return seen


def read_claude_status_model(bridge_dir: Path) -> str | None:
    """
    Read the active model id from the statusLine snapshot ``context.json``.

    Unlike :func:`omnigent.harnesses.claude_native.bridge.read_claude_context_state`
    (which returns ``None`` unless a usable ``context_window_size`` is present,
    since it backs the context ring), this returns the model whenever the
    wrapper captured one — the model and the window are written independently,
    and the cost-budget gate needs the model even on a render where the window
    field was absent. This is claude-native's race-free, gate-time source of the
    live ``/model`` selection (the analogue of the codex hook reading
    ``config.toml``).

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


def url_component(value: str) -> str:
    """
    Percent-encode one URL path component.

    :param value: Raw path component, e.g. ``"conv_abc123"``.
    :returns: URL-safe component with slashes escaped.
    """
    return urllib.parse.quote(value, safe="")


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
