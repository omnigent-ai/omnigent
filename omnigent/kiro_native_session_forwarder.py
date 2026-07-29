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
import time
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
    """

    kind: str
    message_id: str
    text: str
    tool_calls: tuple[KiroToolCall, ...] = ()
    tool_results: tuple[KiroToolResult, ...] = ()


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


# kiro-cli is closed-source and no public capture pins the exact key spellings
# inside its tool blocks (the record kinds and the ``tooluse_`` id shape are
# corroborated; the block-level keys are not). Until a captured transcript
# locks them, accept the tag/key spellings of both the transcript's own
# ``kind``/``data`` block style and the ACP wire style kiro also speaks.
_TOOL_USE_BLOCK_TAGS = frozenset({"toolUse", "tool_use"})
_TOOL_RESULT_BLOCK_TAGS = frozenset({"toolResult", "tool_result"})
_TOOL_ID_KEYS = ("tool_use_id", "toolUseId", "id", "toolCallId")
_TOOL_NAME_KEYS = ("name", "toolName", "tool_name")
_TOOL_ARGS_KEYS = ("input", "args", "arguments")
_TOOL_OUTPUT_KEYS = ("content", "output", "text", "data")


def _block_tag(block: dict[str, object]) -> str | None:
    """Return a content block's discriminator under either tag style."""
    for key in ("kind", "type"):
        tag = block.get(key)
        if isinstance(tag, str) and tag:
            return tag
    return None


def _block_payload(block: dict[str, object]) -> dict[str, object]:
    """Return the fields of a tool block, unwrapping a ``data`` envelope.

    Text blocks nest their value under ``data`` (``{"kind": "text", "data":
    ...}``), so tool blocks plausibly nest an object the same way; a flat block
    carries its fields directly.
    """
    data = block.get("data")
    return data if isinstance(data, dict) else block


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
    """Return a tool-result block's output as display text."""
    for key in _TOOL_OUTPUT_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, dict | list):
            nested = _kiro_content_text(value)
            if nested:
                return nested
            return json.dumps(value, ensure_ascii=True)
    return ""


def _extract_tool_calls(content: object) -> tuple[KiroToolCall, ...]:
    """Extract ``toolUse`` blocks from an ``AssistantMessage`` record's content."""
    if not isinstance(content, list):
        return ()
    calls: list[KiroToolCall] = []
    for block in content:
        if not isinstance(block, dict) or _block_tag(block) not in _TOOL_USE_BLOCK_TAGS:
            continue
        payload = _block_payload(block)
        call_id = _first_str(payload, _TOOL_ID_KEYS)
        name = _first_str(payload, _TOOL_NAME_KEYS)
        if not call_id or not name:
            continue
        calls.append(
            KiroToolCall(call_id=call_id, name=name, arguments=_tool_arguments_json(payload))
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
        payload = _block_payload(block)
        call_id = _first_str(payload, _TOOL_ID_KEYS)
        if not call_id:
            continue
        results.append(KiroToolResult(call_id=call_id, output=_tool_output_text(payload)))
    return tuple(results)


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


async def _mirror_record(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    agent_name: str,
    record: KiroTranscriptRecord,
    state: _ForwardState,
) -> None:
    """Mirror one transcript record, advancing the turn's card lifecycle.

    Turn shape (mirrors the goose-native forwarder): a ``Prompt`` record
    authoritatively closes the previous turn and anchors the next turn's id;
    ``running`` is posted once per turn before its first mirrored item; the
    ledger of pending ``toolUse`` ids decides when an assistant prose record is
    the turn's final message (none emitted, none outstanding); prose posts
    before the record's tool calls so the web spinner sits on the trailing
    item.
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
            await _post_conversation_message(
                client, session_id=session_id, agent_name=agent_name, message=message
            )
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
            await _post_conversation_message(
                client,
                session_id=session_id,
                agent_name=agent_name,
                message=message,
                response_id=response_id,
            )
        for call in record.tool_calls:
            state.pending_call_ids.add(call.call_id)
            await _post_function_call(
                client,
                session_id=session_id,
                agent_name=agent_name,
                response_id=response_id,
                call=call,
            )
        # Kiro's agent loop keeps stepping while the model returns tool uses
        # and stops on a plain reply, so an assistant prose record with no tool
        # uses (and none outstanding) is the authoritative end of the turn.
        if not record.tool_calls and not state.pending_call_ids:
            await post_external_session_status(
                client, session_id=session_id, status="idle", response_id=response_id
            )
            # Keep turn_response_id: a late resume rejoins the same turn.
            state.turn_live = False
        return

    for result in record.tool_results:
        state.pending_call_ids.discard(result.call_id)
        await _post_function_call_output(
            client, session_id=session_id, response_id=response_id, result=result
        )


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
    """Tail Kiro's session JSONL and mirror messages + live tool-call cards."""
    state = _read_state(bridge_dir)
    jsonl_path: Path | None = None
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
                if state.session_id is None or jsonl_path is None or not jsonl_path.exists():
                    discovered: tuple[str, Path] | None = None
                    if expected_session_id:
                        expected_path = await asyncio.to_thread(
                            _kiro_session_jsonl_for_id,
                            expected_session_id,
                            workspace=workspace,
                        )
                        if expected_path is not None:
                            discovered = (expected_session_id, expected_path)
                    elif discovered is None:
                        discovered = await asyncio.to_thread(
                            _discover_kiro_session_jsonl,
                            workspace=workspace,
                            launch_epoch_ms=launch_epoch_ms,
                        )
                    if discovered is not None:
                        discovered_session_id, discovered_path = discovered
                        if state.session_id != discovered_session_id:
                            if state.turn_live and state.turn_response_id:
                                # Rebinding to a different Kiro session strands
                                # the old turn's card; settle it before switching.
                                await post_external_session_status(
                                    client,
                                    session_id=session_id,
                                    status="idle",
                                    response_id=state.turn_response_id,
                                )
                            state = _ForwardState(session_id=discovered_session_id, byte_offset=0)
                            _write_state(bridge_dir, state)
                            last_turn_activity_ts = None
                        jsonl_path = discovered_path
                if jsonl_path is not None and state.session_id is not None:
                    if mirrored_external_session_id != state.session_id:
                        await _patch_external_session_id(
                            client,
                            session_id=session_id,
                            external_session_id=state.session_id,
                        )
                        mirrored_external_session_id = state.session_id
                    records, batch_end_offset = await asyncio.to_thread(
                        _read_new_kiro_records,
                        jsonl_path,
                        state.byte_offset,
                    )
                    for record, record_end_offset in records:
                        # Mirror the transcript plus id-bearing card edges only.
                        # The id-less running/idle session badge stays owned by
                        # the PTY watcher's ``emit_status``
                        # (``runner/resource_registry.py``); double-sourcing it
                        # was #1137. The edges posted here always carry the
                        # turn's ``response_id``, which is what drives the live
                        # tool-call bubbles (the goose-native split).
                        await _mirror_record(
                            client,
                            session_id=session_id,
                            agent_name=agent_name,
                            record=record,
                            state=state,
                        )
                        if state.turn_response_id is not None:
                            # Any transcript record while a turn is open proves
                            # kiro is alive; only true silence trips the backstop.
                            last_turn_activity_ts = _turn_monotonic()
                        state.byte_offset = record_end_offset
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
                    # cost. It lives in the ``.json`` snapshot (sibling of the
                    # tailed ``.jsonl``), not the transcript, and is cumulative;
                    # the server treats ``cumulative_cost_usd`` as monotonic, so
                    # only post when it advances.
                    cumulative_cost, cost_model = await asyncio.to_thread(
                        _read_kiro_cumulative_credits, jsonl_path.with_suffix(".json")
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
                    # Mirror kiro's current model so the web picker shows the real
                    # model (not the harness name) at launch and after a live
                    # ``/model`` switch. Read independently of metering so it is
                    # available before the first turn; posted only when it changes.
                    current_model = await asyncio.to_thread(
                        _read_kiro_current_model, jsonl_path.with_suffix(".json")
                    )
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
