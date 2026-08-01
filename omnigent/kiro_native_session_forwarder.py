"""Structured session forwarder for the kiro-native harness.

Kiro CLI persists chat turns under ``~/.kiro/sessions/cli`` as session metadata
plus JSONL message records. The native Kiro terminal path injects web prompts
into the TUI; this forwarder mirrors Kiro's persisted assistant messages back
into the Omnigent conversation with ``external_conversation_item`` events.

**Live tool-call cards**: when a persisted ``AssistantMessage`` record carries
``toolUse`` content blocks the forwarder emits ``function_call`` /
``function_call_output`` items stamped with a per-turn ``response_id``
(``kiro:turn:{prompt_message_id}``, anchored to the turn's opening ``Prompt``
record — Kiro's transcript has an explicit turn opener, so the id is known
before any assistant activity). A ``running`` status edge carrying the same id
is POSTed before the turn's first mirrored item. The closing ``idle`` edge is
posted when the turn's final prose record lands (Kiro's agent loop ends on an
assistant reply with no tool uses and none outstanding), when the next
``Prompt`` record arrives, or — as a backstop for turns that died without
either (TUI interrupt, kiro-cli crash) — after :data:`_STALLED_TURN_IDLE_S` of
transcript inactivity. Open-turn state is persisted alongside the byte cursor,
so a supervisor restart resumes the turn under its original id instead of
splitting the streaming group or stranding a spinner.

The PTY-activity watcher continues to drive the generic session-level
running/idle badge (id-less); the id-bearing edges here drive only the
streaming lifecycle of the individual tool-call bubbles, matching the
goose-native forwarder. The #1137 rule stands as: this forwarder never posts
an *id-less* session status (double-sourcing the session badge was that bug);
every edge it posts carries a ``response_id``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sqlite3
import sys
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx

from omnigent._native_post_delivery import post_external_session_status
from omnigent.kiro_native_bridge import write_forwarder_ready

_logger = logging.getLogger(__name__)

_DEFAULT_POLL_INTERVAL_S = 0.7
_POST_TIMEOUT_S = 30.0
_DISCOVERY_SKEW_MS = 10_000
_STATE_FILE = "kiro_session_forwarder.json"

#: Seconds of transcript inactivity after which an open turn's live card is
#: closed. This is only a backstop for turns that died without their normal
#: close (the final prose record or the next ``Prompt``) — e.g. a TUI interrupt
#: or a kiro-cli crash. Minutes, not seconds: a legitimately long tool call
#: appends no transcript records while it runs, and closing early makes the
#: spinner flicker on exactly the calls the live card is most useful for.
_STALLED_TURN_IDLE_S = 300.0

_SUPERVISOR_INITIAL_BACKOFF_S = 1.0
_SUPERVISOR_MAX_BACKOFF_S = 30.0
_SUPERVISOR_HEALTHY_UPTIME_S = 60.0


@dataclass
class _ForwardState:
    """Durable cursor + open-turn card state for one Kiro JSONL session file.

    The turn fields persist with the byte cursor so a restart mid-turn neither
    mints a fresh turn id (splitting the streaming group of items already
    posted under the original id) nor re-posts ``running`` for a turn whose
    edge already went out — and a ``running`` the previous run posted can still
    be closed. Kiro's transcript is the only store, so persisting the derived
    state is simpler and cheaper than a goose-style open-turn replay.
    """

    session_id: str | None = None
    byte_offset: int = 0
    turn_response_id: str | None = None
    turn_live: bool = False
    pending_call_ids: set[str] = field(default_factory=set)
    # Which store the bound session lives in: ``sqlite`` (current kiro's
    # conversations_v2, the ``chat --tui`` path), ``legacy_jsonl``
    # (~/.kiro/sessions/cli/*.jsonl, kiro 2.8.1), or ``v3_jsonl``
    # (~/.kiro/sessions/<hash>/sess_*/messages.jsonl). ``byte_offset`` is a byte
    # cursor for the JSONL sources and a history index for sqlite.
    source_kind: str | None = None
    # Keys of conversation items already posted for the record currently being
    # mirrored, persisted after each post and cleared when the cursor advances
    # past the record. Conversation items are not server-deduped, so a POST that
    # throws partway through a multi-item record must not re-post the items that
    # already landed on the in-process retry or a restart mid-record.
    posted_item_keys: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class KiroConversationMessage:
    """Stable parsed-message contract shared by forwarding and offline import."""

    message_id: str
    role: str
    text: str


@dataclass(frozen=True)
class KiroToolCall:
    """One ``toolUse`` content block from an ``AssistantMessage`` record."""

    call_id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class KiroToolResult:
    """One tool-result content block from a ``ToolResults`` record."""

    call_id: str
    output: str


@dataclass(frozen=True)
class KiroTranscriptRecord:
    """One parsed transcript record — the superset the live mirror consumes.

    :func:`parse_kiro_jsonl_line` remains the message-only projection of this
    (the stable contract offline import pins); tool vocabulary only appears
    here.

    :param closes_turn: turn-close override for the mirror. ``None`` (grouped
        formats: legacy JSONL, SQLite conversations_v2) infers the close from
        "assistant record with no tool calls and none pending". ``False`` marks
        a record that must never close the turn (kiro V3 emits prose as its own
        ungrouped event, so intermediate narration must not settle the card).
        The V3 ``turn_end`` uses the dedicated ``TurnEnd`` kind instead.
    """

    kind: str
    message_id: str
    text: str
    tool_calls: tuple[KiroToolCall, ...] = ()
    tool_results: tuple[KiroToolResult, ...] = ()
    closes_turn: bool | None = None


_KiroConversationMessage = KiroConversationMessage


def kiro_cli_sessions_dir(home: Path | None = None) -> Path:
    """Return Kiro CLI's session directory for this user."""
    return (home or Path.home()) / ".kiro" / "sessions" / "cli"


_kiro_cli_sessions_dir = kiro_cli_sessions_dir


def _read_state(bridge_dir: Path) -> _ForwardState:
    """Load the persisted forward cursor, or a cold default."""
    try:
        data = json.loads((bridge_dir / _STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _ForwardState()
    session_id = data.get("session_id")
    byte_offset = data.get("byte_offset")
    turn_response_id = data.get("turn_response_id")
    pending = data.get("pending_call_ids")
    posted = data.get("posted_item_keys")
    source_kind = data.get("source_kind")
    return _ForwardState(
        session_id=session_id if isinstance(session_id, str) and session_id else None,
        byte_offset=byte_offset if isinstance(byte_offset, int) and byte_offset >= 0 else 0,
        turn_response_id=(
            turn_response_id if isinstance(turn_response_id, str) and turn_response_id else None
        ),
        turn_live=data.get("turn_live") is True,
        pending_call_ids=(
            {entry for entry in pending if isinstance(entry, str) and entry}
            if isinstance(pending, list)
            else set()
        ),
        posted_item_keys=(
            {entry for entry in posted if isinstance(entry, str) and entry}
            if isinstance(posted, list)
            else set()
        ),
        source_kind=(
            source_kind if source_kind in ("sqlite", "legacy_jsonl", "v3_jsonl") else None
        ),
    )


def _write_state(bridge_dir: Path, state: _ForwardState) -> None:
    """Persist the forward cursor atomically."""
    bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(bridge_dir, 0o700)
    tmp = bridge_dir / (_STATE_FILE + ".tmp")
    tmp.write_text(
        json.dumps(
            {
                "session_id": state.session_id,
                "byte_offset": state.byte_offset,
                "turn_response_id": state.turn_response_id,
                "turn_live": state.turn_live,
                "pending_call_ids": sorted(state.pending_call_ids),
                "posted_item_keys": sorted(state.posted_item_keys),
                "source_kind": state.source_kind,
            }
        ),
        encoding="utf-8",
    )
    os.replace(tmp, bridge_dir / _STATE_FILE)


def _parse_iso_epoch_ms(value: object) -> int:
    """Parse Kiro's ISO timestamp string into epoch milliseconds."""
    if not isinstance(value, str) or not value:
        return 0
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def _same_workspace(left: object, right: str) -> bool:
    """Return whether Kiro metadata cwd matches the runner workspace."""
    if not isinstance(left, str) or not left:
        return False
    try:
        return Path(left).expanduser().resolve() == Path(right).expanduser().resolve()
    except OSError:
        return left == right


# Competing-candidate sets already warned about, so the ambiguous branch logs
# once per distinct collision instead of every poll tick for the session's life.
# ponytail: grows by one entry per distinct concurrent collision — too rare to
# bound; add a cap only if a pathological churn ever shows up.
_ambiguous_discovery_warned: set[frozenset[str]] = set()


def _warn_ambiguous_discovery(workspace: str, session_ids: list[str]) -> None:
    """Warn once per distinct set of competing same-workspace Kiro sessions.

    Discovery returns ``None`` for both "no candidate yet" (transient) and ">=2
    candidates" (won't ever bind until one goes away). This makes the latter
    diagnosable without flooding logs on every ~0.7s poll.
    """
    key = frozenset(session_ids)
    if key in _ambiguous_discovery_warned:
        return
    _ambiguous_discovery_warned.add(key)
    _logger.warning(
        "kiro session discovery ambiguous; %d same-workspace sessions above the "
        "launch floor, binding none to avoid cross-talk; workspace=%s sessions=%s",
        len(session_ids),
        workspace,
        sorted(session_ids),
    )


def _discover_kiro_session_jsonl(
    *,
    workspace: str,
    launch_epoch_ms: int,
    sessions_dir: Path | None = None,
) -> tuple[str, Path] | None:
    """Find this Omnigent session's Kiro JSONL file — only when it's unambiguous.

    Candidates are Kiro sessions in the same workspace (``cwd``) with a parseable
    ``created_at`` at/after the launch floor (``launch_epoch_ms`` minus a small
    skew, which already drops pre-existing sessions). We bind **only when exactly
    one** session qualifies.

    Each Kiro session is its own JSONL keyed by Kiro's minted id, so two fresh
    sessions launched in the same workspace within the skew window both qualify.
    Picking newest-by-``updated_at`` (the old behaviour) would latch onto
    whichever session most recently emitted a turn — i.e. it can bind the *other*
    session's transcript and silently cross-talk it into this conversation. With
    two or more candidates we can't tell which JSONL is ours, so we return
    ``None`` and retry rather than guess (logged once via
    :func:`_warn_ambiguous_discovery` so the ambiguous case is distinct from "not
    written yet"). A brief delay is safe; mirroring the wrong conversation is not.
    Mirrors cursor-native's "bind only when exactly one chat qualifies"
    (:func:`omnigent.cursor_native_forwarder._discover_store`).

    The resume/fork path doesn't reach here: when the Kiro id is already known the
    caller binds it directly via :func:`_kiro_session_jsonl_for_id`.
    """
    root = sessions_dir or kiro_cli_sessions_dir()
    if not root.is_dir():
        return None
    floor_ms = max(0, launch_epoch_ms - _DISCOVERY_SKEW_MS)
    candidates: list[tuple[str, Path]] = []
    for metadata_path in root.glob("*.json"):
        session_id = metadata_path.stem
        jsonl_path = root / f"{session_id}.jsonl"
        if not jsonl_path.is_file():
            continue
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(metadata, dict) or not _same_workspace(metadata.get("cwd"), workspace):
            continue
        created_ms = _parse_iso_epoch_ms(metadata.get("created_at"))
        # Require a parseable created_at at/after the floor. A fresh session always
        # stamps created_at, so a missing/garbled one can't be confirmed in-window;
        # dropping it stops an undateable straggler from poisoning the "exactly one"
        # count and silently blocking discovery forever.
        if not created_ms or created_ms < floor_ms:
            continue
        candidates.append((session_id, jsonl_path))
    if len(candidates) > 1:
        _warn_ambiguous_discovery(workspace, [session_id for session_id, _ in candidates])
        return None
    return candidates[0] if candidates else None


def _kiro_session_jsonl_for_id(
    session_id: str,
    *,
    workspace: str,
    sessions_dir: Path | None = None,
) -> Path | None:
    """Return the JSONL path for a known Kiro session id, if it is usable."""
    root = sessions_dir or kiro_cli_sessions_dir()
    metadata_path = root / f"{session_id}.json"
    jsonl_path = root / f"{session_id}.jsonl"
    if not jsonl_path.is_file():
        return None
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(metadata, dict) or not _same_workspace(metadata.get("cwd"), workspace):
        return None
    return jsonl_path


def _read_new_kiro_records(
    jsonl_path: Path,
    byte_offset: int,
) -> tuple[list[tuple[KiroTranscriptRecord, int]], int]:
    """Read parsed records after *byte_offset*, each paired with its end offset.

    The per-record end offsets let the mirror loop persist the cursor after
    each record's items are posted (goose's per-row cursor discipline), so a
    crash mid-batch re-reads at most one record instead of the whole batch.
    """
    records: list[tuple[KiroTranscriptRecord, int]] = []
    try:
        with jsonl_path.open("rb") as handle:
            handle.seek(byte_offset)
            # Advance only past newline-terminated lines: Kiro appends to this
            # JSONL live, so the final line may be a record mid-write (no
            # trailing ``\n``). Persisting ``handle.tell()`` past such a partial
            # line would skip the record once Kiro finishes writing it. Hold the
            # offset at the last complete line and re-read the tail next poll.
            offset = byte_offset
            for raw_line in handle:
                if not raw_line.endswith(b"\n"):
                    break
                offset += len(raw_line)
                try:
                    line = raw_line.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                record = parse_kiro_jsonl_record(line)
                if record is not None:
                    records.append((record, offset))
            return records, offset
    except OSError:
        return [], byte_offset


def _read_new_kiro_messages(
    jsonl_path: Path,
    byte_offset: int,
) -> tuple[list[KiroConversationMessage], int]:
    """Read conversation messages after *byte_offset* from Kiro's JSONL file.

    Message-only projection of :func:`_read_new_kiro_records`, retained for the
    offline-import surface and existing tests.
    """
    records, offset = _read_new_kiro_records(jsonl_path, byte_offset)
    messages: list[KiroConversationMessage] = []
    for record, _end_offset in records:
        message = _record_to_message(record)
        if message is not None:
            messages.append(message)
    return messages, offset


def _record_to_message(record: KiroTranscriptRecord) -> KiroConversationMessage | None:
    """Project a transcript record onto the stable message-only contract."""
    if record.kind == "ToolResults" or not record.text:
        return None
    role = "user" if record.kind == "Prompt" else "assistant"
    return KiroConversationMessage(message_id=record.message_id, role=role, text=record.text)


def parse_kiro_jsonl_record(line: str) -> KiroTranscriptRecord | None:
    """Parse one Kiro JSONL line into the full transcript-record contract.

    Superset of :func:`parse_kiro_jsonl_line`: also surfaces ``toolUse``
    content blocks on assistant records and ``ToolResults`` records, which the
    message-only contract drops. Any other record kind still parses to ``None``.
    """
    try:
        record = json.loads(line)
    except ValueError:
        return None
    if not isinstance(record, dict):
        return None
    kind = record.get("kind")
    if kind not in ("Prompt", "AssistantMessage", "ToolResults"):
        return None
    data = record.get("data")
    if not isinstance(data, dict):
        return None
    raw_message_id = data.get("message_id")
    message_id = raw_message_id if isinstance(raw_message_id, str) else ""
    if kind == "ToolResults":
        # A ``ToolResults`` record carries its items under ``results`` (kiro-cli
        # 2.15.1 serde metadata: ``ToolResults{results}``), with ``content`` kept
        # as a tolerant fallback for a variant/older shape.
        tool_results = _extract_tool_results(data.get("results"))
        if not tool_results:
            tool_results = _extract_tool_results(data.get("content"))
        if not tool_results:
            return None
        return KiroTranscriptRecord(
            kind=kind, message_id=message_id, text="", tool_results=tool_results
        )
    if not message_id:
        return None
    text = _kiro_content_text(data.get("content")).strip()
    tool_calls = _extract_tool_calls(data.get("content")) if kind == "AssistantMessage" else ()
    if not text and not tool_calls:
        return None
    return KiroTranscriptRecord(kind=kind, message_id=message_id, text=text, tool_calls=tool_calls)


def parse_kiro_jsonl_line(line: str) -> KiroConversationMessage | None:
    """Parse one Kiro JSONL line into the stable shared message contract."""
    record = parse_kiro_jsonl_record(line)
    if record is None:
        return None
    return _record_to_message(record)


_parse_kiro_jsonl_line = parse_kiro_jsonl_line


def _kiro_content_text(content: object) -> str:
    """Join text blocks from Kiro's persisted message content."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("kind") == "text" and isinstance(block.get("data"), str):
            parts.append(block["data"])
        elif block.get("type") in {"text", "output_text"} and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts)


# Tag/key spellings taken from kiro-cli 2.15.1 serde type metadata (record
# kinds, the ``ContentBlock`` enum tags ``toolUse``/``toolResult``, and the
# field clusters ``ToolUseBlock{toolUseId|tool_use_id, name, input}`` /
# ``ToolResultContent{toolUseId, content, structuredContent, isError}``). The
# binary carries two co-existing representations (the camelCase ACP/content
# form and a snake_case bedrock form), and which one lands in the on-disk JSONL
# is not yet pinned by a captured transcript, so both spellings are accepted
# until a live capture locks the shape.
_TOOL_USE_BLOCK_TAGS = frozenset({"toolUse", "tool_use"})
_TOOL_RESULT_BLOCK_TAGS = frozenset({"toolResult", "tool_result"})
_TOOL_ID_KEYS = ("toolUseId", "tool_use_id", "id", "toolCallId")
_TOOL_NAME_KEYS = ("name", "toolName", "tool_name")
_TOOL_ARGS_KEYS = ("input", "args", "arguments")
_TOOL_OUTPUT_KEYS = ("content", "structuredContent", "output", "text", "data")


def _block_tag(block: dict[str, object]) -> str | None:
    """Return a content block's discriminator under either tag style."""
    for key in ("kind", "type"):
        tag = block.get(key)
        if isinstance(tag, str) and tag:
            return tag
    return None


def _block_fields(block: dict[str, object]) -> dict[str, object]:
    """Flatten a content block's addressable fields across both levels.

    Text blocks nest their value under ``data`` (``{"kind": "text", "data":
    ...}``), and a tool block may carry its id at the block level while the rest
    of the payload sits under ``data`` (``{"kind": "toolResult", "toolUseId":
    ..., "data": {...}}``) or carry everything flat. Reading only the unwrapped
    ``data`` envelope would then silently lose a block-level id and drop the
    whole block. So merge: block-level keys are the base, the ``data`` envelope
    overlays them, and a field found at either level is addressable.
    """
    fields = {k: v for k, v in block.items() if k not in ("kind", "type", "data")}
    data = block.get("data")
    if isinstance(data, dict):
        fields.update(data)
    return fields


def _first_str(payload: dict[str, object], keys: tuple[str, ...]) -> str:
    """Return the first non-empty string among *keys* in *payload*."""
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _tool_arguments_json(payload: dict[str, object]) -> str:
    """Return a tool block's arguments as a JSON-encoded string."""
    for key in _TOOL_ARGS_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, dict | list):
            return json.dumps(value, ensure_ascii=True)
    return "{}"


def _tool_output_text(payload: dict[str, object]) -> str:
    """Return a tool-result block's output as display text.

    An empty container under an earlier key (e.g. the MCP ``content: []`` a
    structured-only tool writes) is skipped rather than serialized to ``"[]"``,
    so a later key that carries the real output (``structuredContent``) still
    wins. A non-empty structure with no text blocks is remembered as a fallback
    but a plain-text key is preferred over it.
    """
    fallback = ""
    for key in _TOOL_OUTPUT_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, dict | list):
            if not value:
                continue
            nested = _kiro_content_text(value)
            if nested:
                return nested
            if not fallback:
                fallback = json.dumps(value, ensure_ascii=True)
    return fallback


def _extract_tool_calls(content: object) -> tuple[KiroToolCall, ...]:
    """Extract ``toolUse`` blocks from an ``AssistantMessage`` record's content."""
    if not isinstance(content, list):
        return ()
    calls: list[KiroToolCall] = []
    for block in content:
        if not isinstance(block, dict) or _block_tag(block) not in _TOOL_USE_BLOCK_TAGS:
            continue
        fields = _block_fields(block)
        call_id = _first_str(fields, _TOOL_ID_KEYS)
        if not call_id:
            continue
        # The tag already proved this is a tool-use block, so require only the
        # id. A missing/unrecognized name key must not drop the call: dropping
        # it also empties the turn's tool-call ledger, and a nameless-but-real
        # call would then let the next prose record close the turn early and
        # vanish the spinner mid-tool. Render it with a placeholder name.
        name = _first_str(fields, _TOOL_NAME_KEYS) or "tool"
        calls.append(
            KiroToolCall(call_id=call_id, name=name, arguments=_tool_arguments_json(fields))
        )
    return tuple(calls)


def _extract_tool_results(content: object) -> tuple[KiroToolResult, ...]:
    """Extract tool-result blocks from a ``ToolResults`` record's content."""
    if not isinstance(content, list):
        return ()
    results: list[KiroToolResult] = []
    for block in content:
        if not isinstance(block, dict) or _block_tag(block) not in _TOOL_RESULT_BLOCK_TAGS:
            continue
        fields = _block_fields(block)
        call_id = _first_str(fields, _TOOL_ID_KEYS)
        if not call_id:
            continue
        results.append(KiroToolResult(call_id=call_id, output=_tool_output_text(fields)))
    return tuple(results)


# ── Current kiro (>= ~2.9): SQLite conversations_v2 source ──────────────────
#
# kiro-cli 2.8.1 wrote ``~/.kiro/sessions/cli/<id>.jsonl`` records shaped
# ``{kind: Prompt|AssistantMessage|ToolResults}`` (the JSONL source above).
# Current kiro-cli (2.15.1, the ``chat --tui`` engine omnigent launches)
# persists to SQLite instead: ``%LOCALAPPDATA%/kiro-cli/data.sqlite3`` (macOS
# ``~/Library/Application Support/kiro-cli``, Linux ``~/.local/share/kiro-cli``),
# table ``conversations_v2`` keyed by the workspace ``cwd``, ``value`` a
# ConversationState JSON blob whose ``history`` is a list of turns:
#   turn.user.content = {"Prompt": {"prompt": str}}
#                     | {"ToolUseResults": {"tool_use_results":
#                         [{"tool_use_id", "content", "status"}]}}
#   turn.assistant    = {"ToolUse": {"message_id", "content",
#                         "tool_uses": [{"id", "name", "args", ...}]}}
#                     | {"Response": {"message_id", "content": str}}
# Each turn normalizes to the same KiroTranscriptRecord stream the mirror
# consumes, so the card/turn-lifecycle logic stays format-agnostic.

_KIRO_DB_FILE = "data.sqlite3"
_KIRO_DB_APP_DIR = "kiro-cli"
_SQLITE_CALL_ID_KEYS = ("id", "tool_use_id", "toolUseId")
_SQLITE_RESULT_ID_KEYS = ("tool_use_id", "id", "toolUseId")
_SQLITE_NAME_KEYS = ("name", "orig_name", "toolName")


def kiro_cli_database_path(home: Path | None = None, env: Mapping[str, str] | None = None) -> Path:
    """Return kiro-cli's SQLite session store for this platform/user.

    Honors ``KIRO_HOME`` first, then the per-platform app-data dir. Overridable
    end-to-end via ``KIRO_HOME`` (tests, non-standard installs).
    """
    env = env if env is not None else os.environ
    home = home or Path.home()
    override = env.get("KIRO_HOME", "").strip()
    if override:
        return Path(override) / _KIRO_DB_APP_DIR / _KIRO_DB_FILE
    if os.name == "nt":
        base = env.get("LOCALAPPDATA", "").strip()
        root = Path(base) if base else home / "AppData" / "Local"
        return root / _KIRO_DB_APP_DIR / _KIRO_DB_FILE
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / _KIRO_DB_APP_DIR / _KIRO_DB_FILE
    xdg = env.get("XDG_DATA_HOME", "").strip()
    root = Path(xdg) if xdg else home / ".local" / "share"
    return root / _KIRO_DB_APP_DIR / _KIRO_DB_FILE


def _connect_kiro_db_ro(db_path: Path) -> sqlite3.Connection | None:
    """Open the kiro SQLite store read-only, reading the live WAL, or ``None``.

    ``mode=ro`` (not ``immutable=1``) so a live session's ``-wal`` sidecar is
    read via the ``-shm``; a plain connection is the fallback for the rare
    window where ``-shm`` is momentarily absent. Only SELECTs are issued.
    Mirrors :func:`omnigent.goose_native_forwarder._connect_ro`.
    """
    for uri, use_uri in ((f"file:{db_path}?mode=ro", True), (str(db_path), False)):
        try:
            if use_uri:
                return sqlite3.connect(uri, timeout=5.0, uri=True)
            return sqlite3.connect(uri, timeout=5.0)
        except sqlite3.Error:
            continue
    return None


def _sqlite_tool_result_text(content: object) -> str:
    """Flatten a conversations_v2 tool result's ``content`` into display text.

    ``content`` is a list of tagged blocks: ``{"Text": str}`` or ``{"Json":
    <obj>}`` (e.g. a shell result ``{"exit_status","stdout","stderr"}``). A bare
    string is returned as is.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        text = block.get("Text")
        if isinstance(text, str) and text:
            parts.append(text)
        elif "Json" in block:
            parts.append(json.dumps(block["Json"], ensure_ascii=True))
    return "\n".join(parts)


def _sqlite_tool_calls(tool_uses: object) -> tuple[KiroToolCall, ...]:
    """Normalize conversations_v2 ``tool_uses`` into the shared call contract."""
    if not isinstance(tool_uses, list):
        return ()
    calls: list[KiroToolCall] = []
    for tu in tool_uses:
        if not isinstance(tu, dict):
            continue
        call_id = _first_str(tu, _SQLITE_CALL_ID_KEYS)
        if not call_id:
            continue
        name = _first_str(tu, _SQLITE_NAME_KEYS) or "tool"
        args = tu.get("args")
        if args is None:
            args = tu.get("orig_args")
        if isinstance(args, dict | list):
            arguments = json.dumps(args, ensure_ascii=True)
        elif isinstance(args, str) and args:
            arguments = args
        else:
            arguments = "{}"
        calls.append(KiroToolCall(call_id=call_id, name=name, arguments=arguments))
    return tuple(calls)


def _sqlite_tool_results(tool_use_results: object) -> tuple[KiroToolResult, ...]:
    """Normalize conversations_v2 ``tool_use_results`` into the result contract."""
    if not isinstance(tool_use_results, list):
        return ()
    results: list[KiroToolResult] = []
    for r in tool_use_results:
        if not isinstance(r, dict):
            continue
        call_id = _first_str(r, _SQLITE_RESULT_ID_KEYS)
        if not call_id:
            continue
        results.append(
            KiroToolResult(call_id=call_id, output=_sqlite_tool_result_text(r.get("content")))
        )
    return tuple(results)


def _sqlite_assistant_record(assistant: dict[str, object]) -> KiroTranscriptRecord | None:
    """Normalize a conversations_v2 turn's assistant side into a record."""
    tool_use = assistant.get("ToolUse")
    if isinstance(tool_use, dict):
        text = tool_use.get("content")
        return KiroTranscriptRecord(
            kind="AssistantMessage",
            message_id=tool_use.get("message_id")
            if isinstance(tool_use.get("message_id"), str)
            else "",
            text=text if isinstance(text, str) else "",
            tool_calls=_sqlite_tool_calls(tool_use.get("tool_uses")),
        )
    response = assistant.get("Response")
    if isinstance(response, dict):
        text = response.get("content")
        return KiroTranscriptRecord(
            kind="AssistantMessage",
            message_id=response.get("message_id")
            if isinstance(response.get("message_id"), str)
            else "",
            text=text if isinstance(text, str) else "",
        )
    return None


def _kiro_records_from_conversation(
    conversation_id: str,
    history: object,
    start_index: int,
) -> tuple[list[tuple[KiroTranscriptRecord, int]], int]:
    """Normalize conversations_v2 ``history`` turns past *start_index*.

    Emits the user side then the assistant side of each finished turn (one with
    both sides written), which maps onto the same Prompt / AssistantMessage /
    ToolResults stream the JSONL source produces. Stops at the first turn whose
    assistant side is not yet written (a turn still streaming), holding the
    cursor there so it is re-read once kiro finishes it.

    Returns the shared ``(list[(record, cursor_after)], batch_end)`` contract
    where the cursor is the history index. Within one entry only the LAST
    record advances the index; earlier records carry the entry's start index so
    a mid-entry POST failure re-reads the whole entry (item dedup skips what
    already landed) rather than skipping its unposted tail. The Prompt id is
    synthesized from the stable ``{conversation_id}:{index}`` position so the
    turn anchor is deterministic across re-reads even though the user side
    carries no id of its own.
    """
    if not isinstance(history, list):
        return [], start_index
    out: list[tuple[KiroTranscriptRecord, int]] = []
    index = max(0, start_index)
    for turn in history[index:]:
        if not isinstance(turn, dict):
            index += 1
            continue
        assistant = turn.get("assistant")
        if not isinstance(assistant, dict) or not assistant:
            # Turn still being written (no assistant side yet): hold the cursor.
            break
        entry: list[KiroTranscriptRecord] = []
        user = turn.get("user")
        user_content = user.get("content") if isinstance(user, dict) else None
        if isinstance(user_content, dict):
            prompt = user_content.get("Prompt")
            tool_results_wrap = user_content.get("ToolUseResults")
            if isinstance(prompt, dict) and isinstance(prompt.get("prompt"), str):
                entry.append(
                    KiroTranscriptRecord(
                        kind="Prompt",
                        message_id=f"{conversation_id}:{index}",
                        text=prompt["prompt"],
                    )
                )
            elif isinstance(tool_results_wrap, dict):
                tool_results = _sqlite_tool_results(tool_results_wrap.get("tool_use_results"))
                if tool_results:
                    entry.append(
                        KiroTranscriptRecord(
                            kind="ToolResults", message_id="", text="", tool_results=tool_results
                        )
                    )
        assistant_record = _sqlite_assistant_record(assistant)
        if assistant_record is not None:
            entry.append(assistant_record)
        for offset_in_entry, record in enumerate(entry):
            cursor_after = index if offset_in_entry < len(entry) - 1 else index + 1
            out.append((record, cursor_after))
        index += 1
    return out, index


def _kiro_conversation_row(con: sqlite3.Connection, workspace: str) -> tuple[str, str] | None:
    """Return ``(conversation_id, value)`` for the conversation bound to *workspace*.

    ``conversations_v2.key`` is the conversation's cwd. Match it to the runner
    workspace by resolved path (via :func:`_same_workspace`) so a trailing-slash
    or case difference still binds.
    """
    try:
        rows = con.execute(
            "SELECT key, conversation_id, value FROM conversations_v2 ORDER BY updated_at DESC"
        ).fetchall()
    except sqlite3.Error:
        return None
    for key, conversation_id, value in rows:
        if _same_workspace(key, workspace) and isinstance(value, str):
            return (conversation_id if isinstance(conversation_id, str) else "", value)
    return None


def _read_kiro_sqlite_records(
    db_path: Path,
    workspace: str,
    history_index: int,
) -> tuple[list[KiroTranscriptRecord], int]:
    """Read new conversations_v2 turns for *workspace* past *history_index*.

    Returns ``([], history_index)`` on any read error or when the DB / row is
    absent (the caller retries next poll), mirroring the JSONL reader's
    hold-and-retry contract.
    """
    con = _connect_kiro_db_ro(db_path)
    if con is None:
        return [], history_index
    try:
        row = _kiro_conversation_row(con, workspace)
        if row is None:
            return [], history_index
        conversation_id, value = row
        try:
            state = json.loads(value)
        except (TypeError, ValueError):
            return [], history_index
        history = state.get("history") if isinstance(state, dict) else None
        return _kiro_records_from_conversation(conversation_id, history, history_index)
    except sqlite3.Error:
        return [], history_index
    finally:
        con.close()


# ── kiro V3 agent (ACP): messages.jsonl source ──────────────────────────────
#
# The V3 agent (``chat --agent-engine v3``, the shared harness with Kiro
# IDE/Web) streams an append-only event log to
# ``~/.kiro/sessions/<workspace-hash>/sess_<uuid>/messages.jsonl``, one record
# per line shaped ``{id, timestamp, payload: {type, ...}}``. Relevant payload
# types map onto the shared record stream, but V3 emits prose, tool calls, and
# tool results as SEPARATE events (ungrouped), so an assistant prose event
# carries ``closes_turn=False`` and the explicit ``turn_end`` becomes a
# ``TurnEnd`` record; the grouped-format close heuristic would otherwise settle
# the card on intermediate narration.
#   user        -> Prompt (content)                       [opens the turn]
#   assistant   -> AssistantMessage (content, no close)   [prose]
#   tool_call   -> AssistantMessage + one function_call   [toolCallId,toolName,args]
#   tool_result -> ToolResults (one result)               [toolCallId, content]
#   turn_end    -> TurnEnd                                 [settles the card]
# Other types (turn_start, session_*, *_interaction, usage_summary) are skipped.

_V3_TOOL_ID_KEY = "toolCallId"


def kiro_v3_sessions_dir(home: Path | None = None) -> Path:
    """Return the root of kiro V3's per-session directories for this user."""
    return (home or Path.home()) / ".kiro" / "sessions"


def parse_kiro_v3_line(line: str) -> KiroTranscriptRecord | None:
    """Parse one kiro V3 ``messages.jsonl`` line into a transcript record."""
    try:
        record = json.loads(line)
    except ValueError:
        return None
    if not isinstance(record, dict):
        return None
    record_id = record.get("id") if isinstance(record.get("id"), str) else ""
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    ptype = payload.get("type")
    if ptype == "user":
        content = payload.get("content")
        if not isinstance(content, str) or not content:
            return None
        return KiroTranscriptRecord(kind="Prompt", message_id=record_id, text=content)
    if ptype == "assistant":
        content = payload.get("content")
        return KiroTranscriptRecord(
            kind="AssistantMessage",
            message_id=record_id,
            text=content if isinstance(content, str) else "",
            closes_turn=False,
        )
    if ptype == "tool_call":
        call_id = payload.get(_V3_TOOL_ID_KEY)
        if not isinstance(call_id, str) or not call_id:
            return None
        name = payload.get("toolName")
        args = payload.get("args")
        if isinstance(args, dict | list):
            arguments = json.dumps(args, ensure_ascii=True)
        elif isinstance(args, str) and args:
            arguments = args
        else:
            arguments = "{}"
        return KiroTranscriptRecord(
            kind="AssistantMessage",
            message_id=record_id,
            text="",
            tool_calls=(
                KiroToolCall(
                    call_id=call_id,
                    name=name if isinstance(name, str) and name else "tool",
                    arguments=arguments,
                ),
            ),
        )
    if ptype == "tool_result":
        call_id = payload.get(_V3_TOOL_ID_KEY)
        if not isinstance(call_id, str) or not call_id:
            return None
        content = payload.get("content")
        if isinstance(content, str):
            output = content
        elif content:
            output = _kiro_content_text(content) or json.dumps(content, ensure_ascii=True)
        else:
            output = ""
        return KiroTranscriptRecord(
            kind="ToolResults",
            message_id="",
            text="",
            tool_results=(KiroToolResult(call_id=call_id, output=output),),
        )
    if ptype == "turn_end":
        return KiroTranscriptRecord(kind="TurnEnd", message_id=record_id, text="")
    return None


def _read_new_kiro_v3_records(
    jsonl_path: Path,
    byte_offset: int,
) -> tuple[list[tuple[KiroTranscriptRecord, int]], int]:
    """Read new V3 ``messages.jsonl`` records after *byte_offset*.

    Same ``(list[(record, cursor_after)], batch_end)`` contract and
    hold-at-last-complete-line discipline as :func:`_read_new_kiro_records`, so
    a record still being written is re-read once complete.
    """
    records: list[tuple[KiroTranscriptRecord, int]] = []
    try:
        with jsonl_path.open("rb") as handle:
            handle.seek(byte_offset)
            offset = byte_offset
            for raw_line in handle:
                if not raw_line.endswith(b"\n"):
                    break
                offset += len(raw_line)
                try:
                    line = raw_line.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                record = parse_kiro_v3_line(line)
                if record is not None:
                    records.append((record, offset))
            return records, offset
    except OSError:
        return [], byte_offset


def _discover_kiro_v3_messages(
    *,
    workspace: str,
    launch_epoch_ms: int,
    sessions_dir: Path | None = None,
) -> Path | None:
    """Find this workspace's newest V3 ``messages.jsonl``, unambiguously.

    V3 nests sessions as ``<workspace-hash>/sess_<uuid>/messages.jsonl`` with a
    sibling ``session.json`` carrying ``cwd``. Bind the unique in-window session
    whose ``cwd`` matches the workspace; return ``None`` on zero or ambiguous
    matches (the caller retries), matching the legacy discovery's caution.
    """
    root = sessions_dir or kiro_v3_sessions_dir()
    if not root.is_dir():
        return None
    floor_ms = max(0, launch_epoch_ms - _DISCOVERY_SKEW_MS)
    matches: list[Path] = []
    for session_json in root.glob("*/sess_*/session.json"):
        messages = session_json.with_name("messages.jsonl")
        if not messages.is_file():
            continue
        try:
            meta = json.loads(session_json.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(meta, dict) or not _same_workspace(meta.get("cwd"), workspace):
            continue
        created_ms = _parse_iso_epoch_ms(meta.get("created_at") or meta.get("createdAt"))
        if created_ms and created_ms < floor_ms:
            continue
        matches.append(messages)
    if len(matches) != 1:
        return None
    return matches[0]


async def _post_conversation_message(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    agent_name: str,
    message: KiroConversationMessage,
    response_id: str | None = None,
) -> None:
    """POST one Kiro message as an external conversation item.

    :param response_id: The open turn's shared id for assistant prose (so the
        bubble joins the turn's streaming group). ``None`` keeps the historic
        per-message id — user prompts and the offline-import surface.
    """
    if message.role == "assistant":
        item_data = {
            "role": "assistant",
            "agent": agent_name,
            "content": [{"type": "output_text", "text": message.text}],
        }
    else:
        item_data = {
            "role": "user",
            "content": [{"type": "input_text", "text": message.text}],
        }
    resp = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "external_conversation_item",
            "data": {
                "item_type": "message",
                "item_data": item_data,
                "response_id": response_id or f"kiro:{message.message_id}",
            },
        },
    )
    resp.raise_for_status()


async def _post_function_call(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    agent_name: str,
    response_id: str,
    call: KiroToolCall,
) -> None:
    """POST one ``toolUse`` block as a ``function_call`` item."""
    resp = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "external_conversation_item",
            "data": {
                "item_type": "function_call",
                "item_data": {
                    "agent": agent_name,
                    "name": call.name,
                    "arguments": call.arguments,
                    "call_id": call.call_id,
                },
                "response_id": response_id,
            },
        },
    )
    resp.raise_for_status()


async def _post_function_call_output(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    response_id: str,
    result: KiroToolResult,
) -> None:
    """POST one tool-result block as a ``function_call_output`` item."""
    resp = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "external_conversation_item",
            "data": {
                "item_type": "function_call_output",
                "item_data": {"call_id": result.call_id, "output": result.output},
                "response_id": response_id,
            },
        },
    )
    resp.raise_for_status()


def _turn_monotonic() -> float:
    """Indirection so tests can stub the stalled-turn clock."""
    return time.monotonic()


async def _patch_external_session_id(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    external_session_id: str,
) -> None:
    """Persist Kiro's native CLI session id onto the Omnigent session."""
    resp = await client.patch(
        f"/v1/sessions/{session_id}",
        json={"external_session_id": external_session_id},
    )
    # The server rejects overwrites with a different id. Forwarding must keep
    # running in that case; losing chat mirroring would be worse than failing to
    # improve cold resume for an already-conflicted session.
    if resp.status_code >= 400:
        _logger.warning(
            "AP rejected Kiro external_session_id PATCH (%s); session=%s kiro_session=%s",
            resp.status_code,
            session_id,
            external_session_id,
        )
        return


def _read_kiro_cumulative_credits(metadata_path: Path) -> tuple[float | None, str | None]:
    """Sum the per-turn credit metering from a Kiro session ``.json`` snapshot.

    kiro-cli meters in credits, not tokens: each turn under
    ``session_state.conversation_metadata.user_turn_metadatas`` carries a
    ``metering_usage`` list of ``{"value": <float>, "unit": "credit"}`` entries
    (token counts are 0), and the CLI shows a per-turn ``Credits:`` line. The
    cumulative session cost is the sum of every turn's credit values. This data
    lives only in the ``.json`` snapshot, not the ``.jsonl`` transcript the
    forwarder tails.

    :returns: ``(cumulative_credits, model_id)``, or ``(None, None)`` when the
        file is missing/unparseable or carries no metering yet.
    """
    try:
        raw = metadata_path.read_text(encoding="utf-8")
    except OSError:
        return None, None
    try:
        data = json.loads(raw)
    except ValueError:
        return None, None
    if not isinstance(data, dict):
        return None, None
    session_state = data.get("session_state")
    if not isinstance(session_state, dict):
        return None, None
    conversation_metadata = session_state.get("conversation_metadata")
    turns = (
        conversation_metadata.get("user_turn_metadatas")
        if isinstance(conversation_metadata, dict)
        else None
    )
    if not isinstance(turns, list):
        return None, None
    total = 0.0
    saw_credit = False
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        metering = turn.get("metering_usage")
        if not isinstance(metering, list):
            continue
        for entry in metering:
            if not isinstance(entry, dict):
                continue
            value = entry.get("value")
            if isinstance(value, int | float) and not isinstance(value, bool):
                total += float(value)
                saw_credit = True
    if not saw_credit:
        return None, None
    model_id: str | None = None
    rts_model_state = session_state.get("rts_model_state")
    if isinstance(rts_model_state, dict):
        model_info = rts_model_state.get("model_info")
        if isinstance(model_info, dict):
            candidate = model_info.get("model_id")
            if isinstance(candidate, str) and candidate:
                model_id = candidate
    return total, model_id


def _read_kiro_current_model(metadata_path: Path) -> str | None:
    """Return kiro's current model id from the session ``.json`` snapshot.

    Reads ``session_state.rts_model_state.model_info.model_id`` (e.g. ``"auto"``
    or ``"claude-haiku-4.5"``), independent of the metering read above so it is
    available at launch before any turn. kiro updates it in place on a ``/model``
    switch, so polling it mirrors both the launched model and live TUI switches.

    :returns: The model id, or ``None`` when the file is missing/unparseable or
        carries no model state yet.
    """
    try:
        raw = metadata_path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    session_state = data.get("session_state")
    if not isinstance(session_state, dict):
        return None
    rts_model_state = session_state.get("rts_model_state")
    if not isinstance(rts_model_state, dict):
        return None
    model_info = rts_model_state.get("model_info")
    if not isinstance(model_info, dict):
        return None
    model_id = model_info.get("model_id")
    return model_id if isinstance(model_id, str) and model_id else None


async def _post_external_model_change(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    model: str,
) -> None:
    """Mirror kiro's current model to the web as ``external_model_change``.

    The server persists this as ``model_override`` (so the picker shows the real
    model instead of falling back to the harness name) and deliberately does NOT
    forward a ``/model`` back to the runner, so mirroring the model the TUI is
    already on cannot loop. Mirrors cursor-native's terminal->web model mirror.
    """
    resp = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={"type": "external_model_change", "data": {"model": model}},
    )
    resp.raise_for_status()


async def _post_session_cost(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    cumulative_cost_usd: float,
    model: str | None,
) -> None:
    """POST Kiro's cumulative credit spend as authoritative session cost.

    kiro-cli reports cost in credits and there is no credit->USD conversion
    available, so credits are forwarded 1:1 into ``cumulative_cost_usd`` (the
    same convention the Copilot relay uses for its AI-credit total). The server
    treats this value as authoritative and monotonic, in preference to
    token x catalog pricing, via the ``external_session_usage`` event used by
    the claude-/codex-native forwarders.
    """
    data: dict[str, object] = {"cumulative_cost_usd": cumulative_cost_usd}
    if model:
        data["model"] = model
    resp = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={"type": "external_session_usage", "data": data},
    )
    resp.raise_for_status()


async def _post_item_once(
    *,
    state: _ForwardState,
    bridge_dir: Path,
    key: str,
    poster: Callable[[], Awaitable[None]],
) -> None:
    """Post a conversation item unless its key already landed.

    Conversation items are not server-deduped, so on an in-process retry or a
    restart mid-record an already-posted item must not repeat. The key is added
    and the state persisted only after the post succeeds, so a failed post is
    retried (never skipped) and a succeeded one is never re-sent.

    This is exactly-once on the in-process retry. Across a restart it holds
    except the one window inherent to any post-then-persist design without
    server-side dedup: if this ``_write_state`` fails after the post already
    landed and the process restarts before the next successful write, the key
    is not on disk and that single item re-posts. The floor is at-least-once (a
    lone duplicate, never a dropped item), same as the goose forwarder.
    """
    if key in state.posted_item_keys:
        return
    await poster()
    state.posted_item_keys.add(key)
    _write_state(bridge_dir, state)


async def _mirror_record(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    agent_name: str,
    record: KiroTranscriptRecord,
    state: _ForwardState,
    bridge_dir: Path,
) -> None:
    """Mirror one transcript record, advancing the turn's card lifecycle.

    Turn shape (mirrors the goose-native forwarder): a ``Prompt`` record
    authoritatively closes the previous turn and anchors the next turn's id;
    ``running`` is posted once per turn before its first mirrored item; the
    ledger of pending ``toolUse`` ids decides when an assistant prose record is
    the turn's final message (none emitted, none outstanding); prose posts
    before the record's tool calls so the web spinner sits on the trailing
    item.

    Each conversation item posts through :func:`_post_item_once`, which skips an
    item whose key already landed and persists the posted-key set after each
    post, so a POST that throws partway through a multi-item record does not
    re-post the earlier items on the in-process retry or a restart mid-record.
    Status edges (running/idle) carry no key: ``running`` is already gated by
    ``turn_live`` and a repeated ``idle`` for the same id is benign.
    """
    if record.kind == "Prompt":
        # A new user turn authoritatively closes the previous one.
        if state.turn_live and state.turn_response_id:
            await post_external_session_status(
                client,
                session_id=session_id,
                status="idle",
                response_id=state.turn_response_id,
            )
        state.turn_response_id = f"kiro:turn:{record.message_id}"
        state.turn_live = False
        state.pending_call_ids.clear()
        message = _record_to_message(record)
        if message is not None:
            await _post_item_once(
                state=state,
                bridge_dir=bridge_dir,
                key=f"msg:{record.message_id}",
                poster=lambda: _post_conversation_message(
                    client, session_id=session_id, agent_name=agent_name, message=message
                ),
            )
        return

    if record.kind == "TurnEnd":
        # An explicit end-of-turn signal (kiro V3 ``turn_end``). Settle the card
        # without re-opening it (unlike an empty assistant record, this never
        # posts ``running`` first) and only when no tool call is still pending;
        # an interrupted turn with an outstanding call falls to the backstop.
        if state.turn_live and state.turn_response_id and not state.pending_call_ids:
            await post_external_session_status(
                client, session_id=session_id, status="idle", response_id=state.turn_response_id
            )
            state.turn_live = False
        return

    if state.turn_response_id is None:
        # Forwarder attached mid-turn (its Prompt is behind the cursor or was
        # never seen): anchor the turn to the first record observed instead.
        anchor = record.message_id or (state.session_id or "unknown")
        state.turn_response_id = f"kiro:turn:{anchor}"
    response_id = state.turn_response_id

    if not state.turn_live:
        await post_external_session_status(
            client, session_id=session_id, status="running", response_id=response_id
        )
        state.turn_live = True

    if record.kind == "AssistantMessage":
        message = _record_to_message(record)
        if message is not None:
            await _post_item_once(
                state=state,
                bridge_dir=bridge_dir,
                key=f"msg:{record.message_id}",
                poster=lambda: _post_conversation_message(
                    client,
                    session_id=session_id,
                    agent_name=agent_name,
                    message=message,
                    response_id=response_id,
                ),
            )
        for call in record.tool_calls:
            state.pending_call_ids.add(call.call_id)
            await _post_item_once(
                state=state,
                bridge_dir=bridge_dir,
                key=f"call:{call.call_id}",
                poster=lambda call=call: _post_function_call(
                    client,
                    session_id=session_id,
                    agent_name=agent_name,
                    response_id=response_id,
                    call=call,
                ),
            )
        # Grouped formats (legacy JSONL, SQLite): kiro's agent loop keeps
        # stepping while the model returns tool uses and stops on a plain reply,
        # so an assistant prose record with no tool uses (and none outstanding)
        # is the authoritative end of the turn. Ungrouped V3 sets
        # ``closes_turn=False`` on its prose events (its ``turn_end`` closes via
        # the TurnEnd kind), so intermediate narration never settles the card.
        infer_close = not record.tool_calls and not state.pending_call_ids
        should_close = infer_close if record.closes_turn is None else record.closes_turn
        if should_close and not state.pending_call_ids:
            await post_external_session_status(
                client, session_id=session_id, status="idle", response_id=response_id
            )
            # Keep turn_response_id: a late resume rejoins the same turn.
            state.turn_live = False
        return

    for result in record.tool_results:
        state.pending_call_ids.discard(result.call_id)
        await _post_item_once(
            state=state,
            bridge_dir=bridge_dir,
            key=f"out:{result.call_id}",
            poster=lambda result=result: _post_function_call_output(
                client, session_id=session_id, response_id=response_id, result=result
            ),
        )


# ── Source detection + dispatch ─────────────────────────────────────────────


@dataclass(frozen=True)
class _KiroBinding:
    """A bound kiro session source: which store and how to reach it.

    :param source_kind: ``sqlite`` | ``legacy_jsonl`` | ``v3_jsonl``.
    :param session_id: conversation_id (sqlite) or kiro session id (jsonl).
    :param path: the transcript path for the JSONL sources; ``None`` for sqlite
        (the conversation is re-resolved by cwd against the DB each read).
    """

    source_kind: str
    session_id: str
    path: Path | None


def _discover_kiro_sqlite_conversation(
    workspace: str, *, db_path: Path | None = None
) -> str | None:
    """Return the conversation_id bound to *workspace* in the SQLite store, else None."""
    db = db_path if db_path is not None else kiro_cli_database_path()
    if not db.exists():
        return None
    con = _connect_kiro_db_ro(db)
    if con is None:
        return None
    try:
        row = _kiro_conversation_row(con, workspace)
        return row[0] if row is not None and row[0] else None
    except sqlite3.Error:
        return None
    finally:
        con.close()


def _bind_kiro_source(
    *,
    workspace: str,
    launch_epoch_ms: int,
    expected_session_id: str | None,
    known_session_id: str | None,
    known_source_kind: str | None,
    db_path: Path | None = None,
    sessions_dir: Path | None = None,
    v3_sessions_dir: Path | None = None,
) -> _KiroBinding | None:
    """Detect and bind the kiro session source for this workspace.

    A resume id (``expected_session_id``) is always a legacy JSONL session and
    is bound directly (waiting for it rather than falling back, so a resume
    never latches a different session). A restart rebinds the already-bound
    source kind by id (legacy JSONL) or by cwd (sqlite / v3) before general
    detection, so a second same-workspace session cannot strand the open card.
    Fresh detection priority reflects current kiro: legacy 2.8.1 ``cli/*.jsonl``
    (launch-window filtered, so a stale old session is skipped), then the
    SQLite ``conversations_v2`` store the ``chat --tui`` engine writes, then the
    V3 ``messages.jsonl``.
    """
    if expected_session_id:
        path = _kiro_session_jsonl_for_id(
            expected_session_id, workspace=workspace, sessions_dir=sessions_dir
        )
        return _KiroBinding("legacy_jsonl", expected_session_id, path) if path else None
    if known_source_kind == "legacy_jsonl" and known_session_id:
        path = _kiro_session_jsonl_for_id(
            known_session_id, workspace=workspace, sessions_dir=sessions_dir
        )
        if path is not None:
            return _KiroBinding("legacy_jsonl", known_session_id, path)
    if known_source_kind == "sqlite":
        conv = _discover_kiro_sqlite_conversation(workspace, db_path=db_path)
        if conv is not None:
            return _KiroBinding("sqlite", conv, None)
    if known_source_kind == "v3_jsonl":
        v3 = _discover_kiro_v3_messages(
            workspace=workspace, launch_epoch_ms=launch_epoch_ms, sessions_dir=v3_sessions_dir
        )
        if v3 is not None:
            return _KiroBinding("v3_jsonl", v3.parent.name, v3)
    legacy = _discover_kiro_session_jsonl(
        workspace=workspace, launch_epoch_ms=launch_epoch_ms, sessions_dir=sessions_dir
    )
    if legacy is not None:
        return _KiroBinding("legacy_jsonl", legacy[0], legacy[1])
    conv = _discover_kiro_sqlite_conversation(workspace, db_path=db_path)
    if conv is not None:
        return _KiroBinding("sqlite", conv, None)
    v3 = _discover_kiro_v3_messages(
        workspace=workspace, launch_epoch_ms=launch_epoch_ms, sessions_dir=v3_sessions_dir
    )
    if v3 is not None:
        return _KiroBinding("v3_jsonl", v3.parent.name, v3)
    return None


def _read_kiro_source_records(
    state: _ForwardState,
    *,
    workspace: str,
    source_path: Path | None,
    db_path: Path | None = None,
) -> tuple[list[tuple[KiroTranscriptRecord, int]], int]:
    """Read new records from the bound source, in the shared cursor contract."""
    if state.source_kind == "sqlite":
        db = db_path if db_path is not None else kiro_cli_database_path()
        return _read_kiro_sqlite_records(db, workspace, state.byte_offset)
    if state.source_kind == "v3_jsonl" and source_path is not None:
        return _read_new_kiro_v3_records(source_path, state.byte_offset)
    if source_path is not None:
        return _read_new_kiro_records(source_path, state.byte_offset)
    return [], state.byte_offset


async def forward_kiro_session_to_omnigent(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    bridge_dir: Path,
    agent_name: str,
    workspace: str,
    launch_epoch_ms: int,
    expected_session_id: str | None = None,
    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
    auth: httpx.Auth | None = None,
) -> None:
    """Tail kiro's session store and mirror messages + live tool-call cards.

    Reads whichever format the running kiro writes: the SQLite
    ``conversations_v2`` store (current kiro's ``chat --tui`` engine), the
    legacy ``~/.kiro/sessions/cli/*.jsonl`` (kiro 2.8.1), or the V3
    ``messages.jsonl``. All normalize to one KiroTranscriptRecord stream, so the
    turn-lifecycle / card logic below is format-agnostic.
    """
    state = _read_state(bridge_dir)
    source_path: Path | None = None
    timeout = httpx.Timeout(_POST_TIMEOUT_S)
    mirrored_external_session_id: str | None = None
    last_posted_cost: float | None = None
    last_posted_model: str | None = None
    # Monotonic clocks don't survive a restart; a restored open turn restarts
    # its inactivity window from now.
    last_turn_activity_ts: float | None = _turn_monotonic() if state.turn_live else None
    async with httpx.AsyncClient(
        base_url=base_url, headers=headers, auth=auth, timeout=timeout
    ) as client:
        while True:
            try:
                # Rebind when unbound, or when a JSONL source's file went away (a
                # restart drops the local path). SQLite stays bound once found
                # (it is re-resolved by cwd against the DB on every read).
                needs_bind = state.source_kind is None or state.session_id is None
                if state.source_kind in ("legacy_jsonl", "v3_jsonl") and (
                    source_path is None or not source_path.exists()
                ):
                    needs_bind = True
                if needs_bind:
                    binding = await asyncio.to_thread(
                        _bind_kiro_source,
                        workspace=workspace,
                        launch_epoch_ms=launch_epoch_ms,
                        expected_session_id=expected_session_id,
                        known_session_id=state.session_id,
                        known_source_kind=state.source_kind,
                    )
                    if binding is not None:
                        if (
                            state.session_id != binding.session_id
                            or state.source_kind != binding.source_kind
                        ):
                            if state.turn_live and state.turn_response_id:
                                # Rebinding to a different session strands the
                                # old turn's card; settle it before switching.
                                await post_external_session_status(
                                    client,
                                    session_id=session_id,
                                    status="idle",
                                    response_id=state.turn_response_id,
                                )
                            state = _ForwardState(
                                session_id=binding.session_id,
                                byte_offset=0,
                                source_kind=binding.source_kind,
                            )
                            _write_state(bridge_dir, state)
                            last_turn_activity_ts = None
                        source_path = binding.path
                bound = state.session_id is not None and state.source_kind is not None
                if bound and (state.source_kind == "sqlite" or source_path is not None):
                    if mirrored_external_session_id != state.session_id:
                        await _patch_external_session_id(
                            client,
                            session_id=session_id,
                            external_session_id=state.session_id,
                        )
                        mirrored_external_session_id = state.session_id
                    records, batch_end_offset = await asyncio.to_thread(
                        _read_kiro_source_records,
                        state,
                        workspace=workspace,
                        source_path=source_path,
                    )
                    for record, record_end_offset in records:
                        # Mirror the transcript plus id-bearing card edges only.
                        # The id-less running/idle session badge stays owned by
                        # the PTY watcher's ``emit_status``
                        # (``runner/resource_registry.py``); double-sourcing it
                        # was #1137. The edges posted here always carry the
                        # turn's ``response_id``, which is what drives the live
                        # tool-call bubbles (the goose-native split).
                        #
                        # The cursor advances once per record, AFTER its items
                        # post. A POST that throws partway through a multi-item
                        # record unwinds to the poll's outer handler without
                        # advancing the cursor, so the next poll re-reads the
                        # record; _post_item_once (via state.posted_item_keys,
                        # persisted per item) skips the items that already landed
                        # so the retry posts only the failed one. Exactly-once on
                        # the in-process retry; across a restart the floor is
                        # at-least-once only in the narrow post-then-persist
                        # window (see _post_item_once), a lone duplicate never a
                        # lost item.
                        await _mirror_record(
                            client,
                            session_id=session_id,
                            agent_name=agent_name,
                            record=record,
                            state=state,
                            bridge_dir=bridge_dir,
                        )
                        if state.turn_response_id is not None:
                            # Any transcript record while a turn is open proves
                            # kiro is alive; only true silence trips the backstop.
                            last_turn_activity_ts = _turn_monotonic()
                        cursor_advanced = record_end_offset != state.byte_offset
                        state.byte_offset = record_end_offset
                        # Drop the posted-item keys only once the cursor has moved
                        # PAST the record's resumable boundary, so the set is
                        # bounded but nothing is cleared while its unit can still
                        # be re-read. For JSONL each record is its own boundary
                        # (the offset always advances). For SQLite a history entry
                        # spans two records sharing one boundary: the first
                        # record's cursor does not advance, so its keys must
                        # survive until the entry's last record clears them.
                        if cursor_advanced:
                            state.posted_item_keys.clear()
                        _write_state(bridge_dir, state)
                    if batch_end_offset != state.byte_offset:
                        # Skipped tail bytes (unparseable/foreign records) still
                        # advance the cursor once their lines are complete.
                        state.byte_offset = batch_end_offset
                        _write_state(bridge_dir, state)
                    # Backstop: a turn that died without its normal close (TUI
                    # interrupt, kiro-cli crash) must not leave a spinner forever.
                    if (
                        state.turn_live
                        and last_turn_activity_ts is not None
                        and _turn_monotonic() - last_turn_activity_ts > _STALLED_TURN_IDLE_S
                    ):
                        await post_external_session_status(
                            client,
                            session_id=session_id,
                            status="idle",
                            response_id=state.turn_response_id,
                        )
                        # Keep turn_response_id: a late resume rejoins the turn.
                        state.turn_live = False
                        _write_state(bridge_dir, state)
                    write_forwarder_ready(bridge_dir)
                    # Forward kiro's credit metering as authoritative session
                    # cost and mirror its current model. Both live in the legacy
                    # ``.json`` snapshot beside the tailed ``.jsonl`` (kiro 2.8.1
                    # only). Current kiro carries the same fields inside the
                    # SQLite / V3 stores; wiring cost+model there is a follow-up,
                    # so skip it for those sources rather than read a sidecar
                    # that does not exist.
                    sidecar = (
                        source_path.with_suffix(".json")
                        if state.source_kind == "legacy_jsonl" and source_path is not None
                        else None
                    )
                    if sidecar is not None:
                        cumulative_cost, cost_model = await asyncio.to_thread(
                            _read_kiro_cumulative_credits, sidecar
                        )
                        if cumulative_cost is not None and (
                            last_posted_cost is None or cumulative_cost > last_posted_cost
                        ):
                            await _post_session_cost(
                                client,
                                session_id=session_id,
                                cumulative_cost_usd=cumulative_cost,
                                model=cost_model,
                            )
                            last_posted_cost = cumulative_cost
                        current_model = await asyncio.to_thread(_read_kiro_current_model, sidecar)
                        if current_model is not None and current_model != last_posted_model:
                            await _post_external_model_change(
                                client, session_id=session_id, model=current_model
                            )
                            last_posted_model = current_model
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception(
                    "kiro session forwarder poll failed; session=%s bridge_dir=%s",
                    session_id,
                    bridge_dir,
                )
            await asyncio.sleep(poll_interval_s)


def _supervisor_monotonic() -> float:
    """Indirection so tests can stub the supervisor clock."""
    return time.monotonic()


async def _supervisor_sleep(seconds: float) -> None:
    """Indirection so tests can stub supervisor sleep."""
    await asyncio.sleep(seconds)


async def supervise_kiro_session_forwarder(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    bridge_dir: Path,
    agent_name: str,
    workspace: str,
    launch_epoch_ms: int,
    expected_session_id: str | None = None,
    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
    auth: httpx.Auth | None = None,
) -> None:
    """Run the Kiro session forwarder under a restart supervisor."""
    backoff_s = _SUPERVISOR_INITIAL_BACKOFF_S
    while True:
        run_started_at = _supervisor_monotonic()
        crash_exc: Exception | None = None
        try:
            await forward_kiro_session_to_omnigent(
                base_url=base_url,
                headers=headers,
                session_id=session_id,
                bridge_dir=bridge_dir,
                agent_name=agent_name,
                workspace=workspace,
                launch_epoch_ms=launch_epoch_ms,
                expected_session_id=expected_session_id,
                poll_interval_s=poll_interval_s,
                auth=auth,
            )
            _logger.warning(
                "kiro session forwarder returned unexpectedly; restarting; "
                "session=%s bridge_dir=%s",
                session_id,
                bridge_dir,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - supervisor restarts on any crash
            crash_exc = exc
        if _supervisor_monotonic() - run_started_at >= _SUPERVISOR_HEALTHY_UPTIME_S:
            backoff_s = _SUPERVISOR_INITIAL_BACKOFF_S
        if crash_exc is not None:
            _logger.error(
                "kiro session forwarder crashed; restarting in %.1fs; session=%s bridge_dir=%s",
                backoff_s,
                session_id,
                bridge_dir,
                exc_info=crash_exc,
            )
        await _supervisor_sleep(backoff_s)
        backoff_s = min(backoff_s * 2, _SUPERVISOR_MAX_BACKOFF_S)


__all__ = [
    "forward_kiro_session_to_omnigent",
    "supervise_kiro_session_forwarder",
]
