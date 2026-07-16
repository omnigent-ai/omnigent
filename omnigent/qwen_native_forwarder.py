"""TUI→web forwarder for the qwen-native harness.

The ``omnigent qwen`` wrapper launches the real ``qwen`` TUI in a runner-owned
tmux pane with ``--json-file`` pointed at the bridge dir, and
:mod:`omnigent.qwen_native_bridge` appends web-UI messages to its ``--input-file``.
That covers the web→TUI direction, but the *embedded terminal* is then the only
surface that reflects the agent's work — the Omnigent conversation view stays
empty because nothing mirrors the transcript back into the session.

This module is that missing mirror — the qwen analog of
:mod:`omnigent.goose_native_forwarder`. Where goose has to scrape a SQLite store,
qwen emits a structured **stream-json event stream** (verified Anthropic-shaped
against ``qwen`` v0.18.1): we tail the ``--json-file`` NDJSON by byte offset and
POST each new ``user`` / ``assistant`` message as an ``external_conversation_item``
event (which also seeds the session title).

Event shapes consumed (others are ignored defensively):

- ``{"type":"user","message":{"role":"user","content":[{"type":"text","text":...}|
  {"type":"tool_result","tool_use_id":...,"content":...}]}}`` — prose blocks become
  a user bubble; ``tool_result`` blocks become ``function_call_output`` items.
- ``{"type":"assistant","message":{"role":"assistant","content":[{"type":"text"|
  "thinking"|"tool_use",...}]}}`` — ``text`` blocks become the assistant bubble and
  ``tool_use`` blocks become ``function_call`` items; ``thinking`` is skipped.
- ``{"type":"result","subtype":"success"|...}`` — qwen's explicit turn terminator.
- ``{"type":"control_request","request":{"subtype":"can_use_tool",...},
  "request_id":...}`` and the matching ``control_response`` — the permission
  control plane. NOT handled here: the tool-approval mirror
  (:mod:`omnigent.qwen_native_permissions`) tails the same stream and surfaces
  these as web elicitation cards. This forwarder ignores them (they carry no
  transcript prose to mirror).

Turn lifecycle: a prose-bearing ``user`` event opens a turn, minting a
``qwen:turn:{uuid}`` response id that every item of the turn carries. The
forwarder POSTs an id-bearing ``external_session_status`` ``running`` edge before
the turn's first assistant item and an ``idle``/``failed`` edge when it closes, so
ap-web renders the turn's mirrored tool cards LIVE (spinner + elapsed timer) rather
than as static completed cards. These id-bearing edges drive only that streaming
lifecycle — the coarse, id-less session badge stays owned by the runner's
PTY-activity watcher (see :mod:`omnigent.runner.app`), as for goose-/cursor-native.

This module also hosts the **compaction mirror** (:func:`supervise_qwen_compaction_mirror`).
qwen compaction (its *compression*) is invisible on the ``--json-file`` stream
(``session_start``'s ``supported_events`` omits it — verified live, ``qwen``
v0.18.2), but qwen writes a ``{"type":"system","subtype":"chat_compression",
"systemPayload":{"info":{originalTokenCount,newTokenCount,compressionStatus}}}``
record to its on-disk chat recording the instant compression finishes. The mirror
tails that recording and POSTs ``external_compaction_status`` (``completed`` on
success, ``failed`` otherwise) — the completion half of the web ``/compact`` →
qwen ``/compress`` flow whose ``in_progress`` edge the runner raises on injection.
It fires for both explicit ``/compress`` and auto-compaction.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import time
from collections.abc import Container, Iterable
from dataclasses import dataclass
from pathlib import Path

import httpx

from omnigent._native_post_delivery import post_external_session_status
from omnigent.qwen_native_bridge import events_file_path

_logger = logging.getLogger(__name__)

#: Seconds between event-file polls. qwen flushes events per streaming step, so a
#: sub-second cadence keeps the mirrored chat tracking the terminal step by step.
_DEFAULT_POLL_INTERVAL_S = 0.4
_POST_TIMEOUT_S = 30.0

# Supervisor backoff (mirrors goose_native_forwarder.supervise_goose_forwarder).
_SUPERVISOR_INITIAL_BACKOFF_S = 1.0
_SUPERVISOR_MAX_BACKOFF_S = 30.0
_SUPERVISOR_HEALTHY_UPTIME_S = 60.0

_STATE_FILE = "qwen_forwarder.json"

#: How long an open turn may go without a single stream event before the
#: forwarder closes it anyway. Only a turn killed without its terminator (TUI
#: interrupt, qwen crash) reaches this; a live turn writes events constantly.
#: Minutes-scale so a genuinely slow tool call never trips it.
_STALLED_TURN_IDLE_S = 300.0

# Dedup window: the number of most-recently-posted event uuids persisted so a
# truncation/relaunch re-read (offset rewinds to 0) doesn't re-post them. The
# window must keep the *most recent* uuids, so ``seen`` is an insertion-ordered
# mapping (a ``dict`` used as an ordered set), not a ``set`` — ``list(set)`` is
# hash-ordered, which would make the ``[-_DEDUP_WINDOW:]`` cap keep an arbitrary
# subset and re-post recent history on a long-session relaunch.
_DEDUP_WINDOW = 512


def _monotonic() -> float:
    """Indirection so tests can stub the stalled-turn backstop's clock."""
    return time.monotonic()


def _new_seen(uuids: Iterable[str] | None = None) -> dict[str, None]:
    """Build the insertion-ordered dedup set (``dict`` used as an ordered set)."""
    return dict.fromkeys(uuids or [])


# The executor injects ``[Attached: <path>]`` markers for web-UI attachments
# before submitting; strip them from the mirrored bubble (the path is an internal
# bridge detail).
_ATTACHMENT_MARKER_RE = re.compile(r"\[Attached:[^\]]*\]")


@dataclass
class _ForwardState:
    """Durable forwarder cursor, persisted to ``bridge_dir/qwen_forwarder.json``.

    :param offset: Byte offset into the ``--json-file`` already consumed. The
        event file is append-only within a TUI lifetime; a relaunched terminal
        truncates it (see :func:`~omnigent.qwen_native_bridge.prepare_bridge_files`),
        which we detect as ``size < offset`` and reset to 0.
    :param seen_uuids: Recently posted event uuids, for idempotent dedup across a
        truncation/restart. Bounded to the most recent entries.
    :param active_turn_id: Response id of the turn still open at the cursor, so a
        supervisor restart mid-turn rejoins it instead of splitting the streaming
        group across two ids. ``None`` when no turn is open.
    :param turn_live: Whether a ``running`` edge for :attr:`active_turn_id` is still
        unclosed. Persisted so a restart still emits the closing ``idle`` for a
        running the prior process posted.
    """

    offset: int = 0
    seen_uuids: list[str] | None = None
    active_turn_id: str | None = None
    turn_live: bool = False


def _read_state(bridge_dir: Path) -> _ForwardState:
    """Load the persisted forward cursor, or a cold default."""
    try:
        raw = (bridge_dir / _STATE_FILE).read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return _ForwardState(offset=0, seen_uuids=[])
    offset = data.get("offset")
    seen = data.get("seen_uuids")
    turn_id = data.get("active_turn_id")
    return _ForwardState(
        offset=offset if isinstance(offset, int) and offset >= 0 else 0,
        seen_uuids=[u for u in seen if isinstance(u, str)] if isinstance(seen, list) else [],
        active_turn_id=turn_id if isinstance(turn_id, str) and turn_id else None,
        turn_live=bool(data.get("turn_live")),
    )


def _write_state(bridge_dir: Path, state: _ForwardState) -> bool:
    """Atomically persist the forward cursor (tmp write + rename)."""
    try:
        bridge_dir.mkdir(parents=True, exist_ok=True)
        tmp = bridge_dir / (_STATE_FILE + ".tmp")
        # Cap the dedup window so the state file can't grow unbounded. The list
        # is insertion-ordered (see _new_seen), so this keeps the most recent
        # _DEDUP_WINDOW uuids — the ones a relaunch re-read is most likely to hit.
        seen = (state.seen_uuids or [])[-_DEDUP_WINDOW:]
        tmp.write_text(
            json.dumps(
                {
                    "offset": state.offset,
                    "seen_uuids": seen,
                    "active_turn_id": state.active_turn_id,
                    "turn_live": state.turn_live,
                }
            ),
            encoding="utf-8",
        )
        os.replace(tmp, bridge_dir / _STATE_FILE)
        return True
    except OSError:
        _logger.warning("qwen forwarder could not persist state to %s", bridge_dir, exc_info=True)
        return False


def clear_qwen_bridge_state(bridge_dir: Path) -> None:
    """Remove the persisted forward cursor so a re-created terminal starts clean."""
    with contextlib.suppress(OSError):
        (bridge_dir / _STATE_FILE).unlink()


@dataclass
class _MirrorItem:
    """One conversation item ready to POST, plus the event uuid that produced it."""

    uuid: str
    item_type: str
    item_data: dict[str, object]
    #: The turn's ``qwen:turn:{uuid}`` id, or ``None`` for a stray item whose turn
    #: could not be identified (e.g. a tool result read before any turn opened).
    response_id: str | None


@dataclass
class _TurnAction:
    """One ordered step the poll loop performs for a batch of stream events.

    :param kind: ``"item"`` (POST :attr:`item`) or ``"status"`` (POST
        :attr:`status` carrying :attr:`turn_id`).
    :param turn_id: Response id the action belongs to.
    :param item: The mirror item, for ``kind == "item"``.
    :param status: The session status, for ``kind == "status"``.
    :param output: Optional detail attached to a ``failed`` status.
    """

    kind: str
    turn_id: str | None
    item: _MirrorItem | None = None
    status: str | None = None
    output: str | None = None


def _blocks_of_type(content: object, block_type: str) -> list[dict[str, object]]:
    """Return the *block_type* blocks of a stream-json message ``content`` array."""
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict) and b.get("type") == block_type]


def _text_from_content(content: object) -> str:
    """Join the ``text`` blocks of a stream-json message ``content`` array.

    ``thinking``, ``tool_use`` and ``tool_result`` blocks are skipped — only
    user-facing prose belongs in the chat bubble; the tool blocks get their own
    items. Tolerant of a bare string or odd shapes so a schema tweak degrades to
    "best available text" rather than dropping the row.
    """
    if isinstance(content, str):
        return content.strip()
    parts = [b.get("text") for b in _blocks_of_type(content, "text")]
    return "".join(p for p in parts if isinstance(p, str)).strip()


def _tool_result_output(content: object) -> str:
    """Flatten a ``tool_result`` block's ``content`` to the text the card shows.

    qwen nests it as a string or an Anthropic block array; anything else yields
    an empty output rather than a dropped card.
    """
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(str(block["text"]))
    return "".join(parts).strip()


def _event_prose(event: dict[str, object]) -> str:
    """The mirrorable prose of a ``user``/``assistant`` event, attachment markers stripped."""
    message = event.get("message")
    if not isinstance(message, dict):
        return ""
    return _ATTACHMENT_MARKER_RE.sub("", _text_from_content(message.get("content"))).strip()


def _event_to_items(
    event: dict[str, object], agent_name: str, response_id: str | None
) -> list[_MirrorItem]:
    """Convert one qwen stream-json event to its mirror items (possibly none).

    An ``assistant`` event yields its prose bubble followed by one
    ``function_call`` per ``tool_use`` block; a ``user`` event yields its prose
    bubble and/or a ``function_call_output`` per ``tool_result`` block. Every item
    carries *response_id* so the web groups the turn's cards under one response.
    """
    etype = event.get("type")
    if etype not in ("user", "assistant"):
        # control_request / control_response (the permission control plane) carry
        # no transcript prose; the tool-approval mirror
        # (omnigent.qwen_native_permissions) owns them off the same stream.
        return []
    uuid = event.get("uuid")
    if not isinstance(uuid, str) or not uuid:
        return []
    message = event.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    text = _event_prose(event)
    items: list[_MirrorItem] = []

    if etype == "user":
        if text:
            items.append(
                _MirrorItem(
                    uuid=uuid,
                    item_type="message",
                    item_data={"role": "user", "content": [{"type": "input_text", "text": text}]},
                    response_id=response_id,
                )
            )
        for block in _blocks_of_type(content, "tool_result"):
            call_id = block.get("tool_use_id")
            if not isinstance(call_id, str) or not call_id:
                continue
            items.append(
                _MirrorItem(
                    uuid=uuid,
                    item_type="function_call_output",
                    item_data={
                        "call_id": call_id,
                        "output": _tool_result_output(block.get("content")),
                    },
                    response_id=response_id,
                )
            )
        return items

    # Emit the prose FIRST, then the tool calls made in the same step. That's the
    # natural reading order ("I'll run X…" then the call), and the web only spins
    # the TRAILING tool phase — prose emitted after the calls kills the spinner.
    if text:
        items.append(
            _MirrorItem(
                uuid=uuid,
                item_type="message",
                item_data={
                    "role": "assistant",
                    "agent": agent_name,
                    "content": [{"type": "output_text", "text": text}],
                },
                response_id=response_id,
            )
        )
    for block in _blocks_of_type(content, "tool_use"):
        call_id = block.get("id")
        name = block.get("name")
        if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or not name:
            continue
        try:
            arguments = json.dumps(block.get("input") or {})
        except (TypeError, ValueError):
            arguments = "{}"  # unserializable input degrades to an argument-less card
        items.append(
            _MirrorItem(
                uuid=uuid,
                item_type="function_call",
                item_data={
                    "agent": agent_name,
                    "name": name,
                    "arguments": arguments,
                    "call_id": call_id,
                },
                response_id=response_id,
            )
        )
    return items


def _event_has_tool_use(event: dict[str, object]) -> bool:
    """Whether an ``assistant`` event carries at least one ``tool_use`` block."""
    message = event.get("message")
    if not isinstance(message, dict):
        return False
    return bool(_blocks_of_type(message.get("content"), "tool_use"))


def _result_failure_detail(event: dict[str, object]) -> str | None:
    """The text a non-success ``result`` event surfaces as the session's last error."""
    for key in ("result", "subtype"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _records_to_actions(
    records: Iterable[dict[str, object]],
    agent_name: str,
    seen: Container[str],
    turn_id: str | None,
    live: bool,
) -> tuple[list[_TurnAction], str | None, bool]:
    """Turn a batch of stream events into ordered post actions + the turn state after it.

    A turn is opened by a **prose-bearing** ``user`` event — tool results also
    arrive as ``user`` events in the Anthropic envelope, and treating those as
    delimiters would split every turn at each tool call. ``running`` is posted once
    per turn, before its first assistant item; ``result`` closes the turn, as does a
    prose-only assistant step (the belt-and-braces close, for a ``result`` that
    never lands).

    :param seen: Event uuids already posted, for dedup across a truncation rewind.
    :param turn_id: Response id of the turn open at the start of the batch.
    :param live: Whether ``running`` was already posted for *turn_id*.
    """
    actions: list[_TurnAction] = []
    for event in records:
        etype = event.get("type")
        uuid = event.get("uuid")

        if etype == "result":
            if live:
                if event.get("subtype") == "success":
                    actions.append(_TurnAction("status", turn_id, status="idle"))
                else:
                    actions.append(
                        _TurnAction(
                            "status",
                            turn_id,
                            status="failed",
                            output=_result_failure_detail(event),
                        )
                    )
                live = False
            elif turn_id is not None and event.get("subtype") != "success":
                # The prose belt-and-braces already settled the card to idle; a
                # failure landing after it must override that premature success.
                actions.append(
                    _TurnAction(
                        "status",
                        turn_id,
                        status="failed",
                        output=_result_failure_detail(event),
                    )
                )
            turn_id = None
            continue

        if etype == "user" and _event_prose(event) and isinstance(uuid, str) and uuid:
            # A new prose user event authoritatively closes the previous turn.
            if live:
                actions.append(_TurnAction("status", turn_id, status="idle"))
                live = False
            turn_id = f"qwen:turn:{uuid}"
        elif etype == "assistant" and turn_id is None and isinstance(uuid, str) and uuid:
            # Missed-start recovery: a forwarder that attached mid-turn still gets
            # an id, so the rest of the turn's cards render live.
            turn_id = f"qwen:turn:{uuid}"

        items = [
            item for item in _event_to_items(event, agent_name, turn_id) if item.uuid not in seen
        ]
        if items and etype == "assistant" and not live and turn_id is not None:
            actions.append(_TurnAction("status", turn_id, status="running"))
            live = True
        actions.extend(_TurnAction("item", turn_id, item=item) for item in items)

        # A prose assistant step with no tool call is the turn's final answer —
        # settle its card now rather than waiting on ``result``. The id is kept so
        # a further step of the same turn re-opens under it.
        if etype == "assistant" and live and items and not _event_has_tool_use(event):
            actions.append(_TurnAction("status", turn_id, status="idle"))
            live = False

    return actions, turn_id, live


def _read_new_records(events_file: Path, offset: int) -> tuple[list[dict[str, object]], int]:
    """Read NDJSON lines past *offset*, returning the parsed events + the new offset.

    Detects a truncated/recreated event file (``size < offset``) and rewinds to 0.
    Only fully terminated lines (ending in ``\\n``) are consumed; a trailing
    partial line is left for the next poll by not advancing past it.
    """
    try:
        size = events_file.stat().st_size
    except OSError:
        return [], offset
    if size < offset:
        offset = 0  # file truncated by a relaunched terminal
    if size == offset:
        return [], offset
    try:
        with open(events_file, "rb") as fh:
            fh.seek(offset)
            data = fh.read(size - offset)
    except OSError:
        return [], offset
    # Only consume up to the last newline; keep any trailing partial line.
    last_nl = data.rfind(b"\n")
    if last_nl == -1:
        return [], offset  # no complete line yet
    consumed = data[: last_nl + 1]
    new_offset = offset + len(consumed)
    records: list[dict[str, object]] = []
    for raw in consumed.split(b"\n"):
        raw = raw.strip()
        if not raw:
            continue
        try:
            event = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue  # tolerate a malformed line rather than stalling the tail
        if isinstance(event, dict):
            records.append(event)
    return records, new_offset


async def _post_conversation_item(
    client: httpx.AsyncClient, *, session_id: str, item: _MirrorItem
) -> None:
    """POST one mirrored item as an ``external_conversation_item`` event."""
    resp = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "external_conversation_item",
            "data": {
                "item_type": item.item_type,
                "item_data": item.item_data,
                "response_id": item.response_id,
            },
        },
    )
    resp.raise_for_status()


async def forward_qwen_events_to_session(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    bridge_dir: Path,
    agent_name: str,
    events_file: Path | None = None,
    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
    auth: httpx.Auth | None = None,
) -> None:
    """Tail qwen's ``--json-file`` and mirror new messages + tool calls into the AP session.

    Polls the event file past a persisted byte offset, posting each new
    user/assistant message and tool call as an ``external_conversation_item``,
    bracketed by the turn's id-bearing ``running``/``idle`` status edges so the
    tool cards render live. The offset, dedup set and open turn id are persisted to
    ``bridge_dir`` so a supervisor restart resumes without re-posting or splitting
    the turn.

    :param base_url: Omnigent server base URL.
    :param headers: Static HTTP headers (auth normally via ``auth``).
    :param session_id: Omnigent session/conversation id.
    :param bridge_dir: The qwen-native bridge dir (holds the persisted cursor).
    :param agent_name: Agent label stamped on mirrored assistant items.
    :param events_file: qwen ``--json-file`` path; defaults to the bridge dir's.
    :param poll_interval_s: Seconds between event-file polls.
    :param auth: Optional refresh-capable httpx Auth for remote deployments.
    :returns: Never normally returns; cancel the task to stop it.
    """
    target = events_file or events_file_path(bridge_dir)
    persisted = _read_state(bridge_dir)
    offset = persisted.offset
    seen = _new_seen(persisted.seen_uuids)
    # Resume the open turn and its live/idle state so the closing ``idle`` still
    # fires for a ``running`` the prior process posted before a crash/restart.
    turn_id = persisted.active_turn_id
    live = persisted.turn_live
    # Seed the stall clock when resuming a live turn so the backstop can still fire
    # if the crashed qwen never grows the event file again post-restart.
    last_activity_ts: float | None = _monotonic() if live else None
    timeout = httpx.Timeout(_POST_TIMEOUT_S)
    async with httpx.AsyncClient(
        base_url=base_url, headers=headers, auth=auth, timeout=timeout
    ) as client:
        while True:
            try:
                records, new_offset = await asyncio.to_thread(_read_new_records, target, offset)
                actions, turn_id, live = _records_to_actions(
                    records, agent_name, seen, turn_id, live
                )
                for action in actions:
                    if action.kind == "item" and action.item is not None:
                        await _post_conversation_item(
                            client, session_id=session_id, item=action.item
                        )
                        seen[action.item.uuid] = None
                    elif action.kind == "status" and action.status is not None:
                        await post_external_session_status(
                            client,
                            session_id=session_id,
                            status=action.status,
                            output=action.output,
                            response_id=action.turn_id,
                        )
                if records:
                    # Any stream event proves qwen is alive; only true silence
                    # should trip the stalled-turn backstop below.
                    last_activity_ts = _monotonic()
                if new_offset != offset or actions:
                    offset = new_offset
                    _write_state(
                        bridge_dir,
                        _ForwardState(
                            offset=offset,
                            seen_uuids=list(seen),
                            active_turn_id=turn_id,
                            turn_live=live,
                        ),
                    )
                # Backstop: a turn killed without its terminator (TUI interrupt,
                # qwen crash) must not leave a spinner running forever.
                if (
                    live
                    and last_activity_ts is not None
                    and _monotonic() - last_activity_ts > _STALLED_TURN_IDLE_S
                ):
                    await post_external_session_status(
                        client, session_id=session_id, status="idle", response_id=turn_id
                    )
                    live = False  # keep turn_id: a late resume rejoins the same turn
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception(
                    "qwen forwarder poll failed; session=%s bridge_dir=%s",
                    session_id,
                    bridge_dir,
                )
            await asyncio.sleep(poll_interval_s)


def _supervisor_monotonic() -> float:
    """Indirection so tests can stub the supervisor's clock."""
    return time.monotonic()


async def _supervisor_sleep(seconds: float) -> None:
    """Indirection so tests can stub the supervisor's backoff sleep."""
    await asyncio.sleep(seconds)


async def supervise_qwen_forwarder(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    bridge_dir: Path,
    agent_name: str,
    events_file: Path | None = None,
    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
    auth: httpx.Auth | None = None,
) -> None:
    """Run :func:`forward_qwen_events_to_session` under a restart supervisor.

    Mirrors :func:`omnigent.goose_native_forwarder.supervise_goose_forwarder`:
    bounded exponential backoff, :class:`asyncio.CancelledError` propagates for
    clean teardown, and the persisted offset means restarts resume exactly where
    they left off.

    :returns: Never normally returns; cancel the task to stop it.
    """
    backoff_s = _SUPERVISOR_INITIAL_BACKOFF_S
    while True:
        run_started_at = _supervisor_monotonic()
        crash_exc: Exception | None = None
        try:
            await forward_qwen_events_to_session(
                base_url=base_url,
                headers=headers,
                session_id=session_id,
                bridge_dir=bridge_dir,
                agent_name=agent_name,
                events_file=events_file,
                poll_interval_s=poll_interval_s,
                auth=auth,
            )
            _logger.warning(
                "qwen forwarder returned unexpectedly; restarting; session=%s bridge_dir=%s",
                session_id,
                bridge_dir,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — supervisor restarts on any Exception
            crash_exc = exc
        if _supervisor_monotonic() - run_started_at >= _SUPERVISOR_HEALTHY_UPTIME_S:
            backoff_s = _SUPERVISOR_INITIAL_BACKOFF_S
        if crash_exc is not None:
            _logger.error(
                "qwen forwarder crashed; restarting in %.1fs; session=%s bridge_dir=%s",
                backoff_s,
                session_id,
                bridge_dir,
                exc_info=crash_exc,
            )
        await _supervisor_sleep(backoff_s)
        backoff_s = min(backoff_s * 2.0, _SUPERVISOR_MAX_BACKOFF_S)


# --- Compaction mirror (chat-recording tail → external_compaction_status) ------

#: qwen's CompressionStatus enum (verified, qwen v0.18.2): 1 = COMPRESSED (success),
#: 2/3 = COMPRESSION_FAILED_*. 1 → completed; anything else → failed.
_COMPRESSION_STATUS_OK = 1


def _compaction_status_from_record(record: dict[str, object]) -> str | None:
    """Map a chat-recording line to a compaction status, or ``None`` to skip it.

    Returns ``"completed"`` for a successful ``chat_compression`` record,
    ``"failed"`` for a failed one, and ``None`` for any other line.
    """
    if record.get("type") != "system" or record.get("subtype") != "chat_compression":
        return None
    payload = record.get("systemPayload")
    info = payload.get("info") if isinstance(payload, dict) else None
    status = info.get("compressionStatus") if isinstance(info, dict) else None
    return "completed" if status == _COMPRESSION_STATUS_OK else "failed"


def _read_new_compaction_statuses(recording: Path, offset: int) -> tuple[list[str], int]:
    """Read NDJSON lines past *offset*, returning new compaction statuses + offset.

    Same tail discipline as :func:`_read_new_events` (truncation rewind, only
    newline-terminated lines consumed), but scoped to ``chat_compression`` records.
    """
    try:
        size = recording.stat().st_size
    except OSError:
        return [], offset  # recording not created yet — retry next poll
    if size < offset:
        offset = 0  # truncated/recreated
    if size == offset:
        return [], offset
    try:
        with open(recording, "rb") as fh:
            fh.seek(offset)
            data = fh.read(size - offset)
    except OSError:
        return [], offset
    last_nl = data.rfind(b"\n")
    if last_nl == -1:
        return [], offset
    consumed = data[: last_nl + 1]
    new_offset = offset + len(consumed)
    statuses: list[str] = []
    for raw in consumed.split(b"\n"):
        raw = raw.strip()
        if not raw:
            continue
        try:
            record = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(record, dict):
            continue
        status = _compaction_status_from_record(record)
        if status is not None:
            statuses.append(status)
    return statuses, new_offset


async def _post_external_compaction_status(
    client: httpx.AsyncClient, *, session_id: str, status: str
) -> None:
    """POST one ``external_compaction_status`` event; the server republishes the SSE."""
    resp = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={"type": "external_compaction_status", "data": {"status": status}},
    )
    resp.raise_for_status()


async def supervise_qwen_compaction_mirror(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    recording_path: Path,
    auth: httpx.Auth | None = None,
    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
) -> None:
    """Tail qwen's chat recording and mirror compaction completions to the session.

    Seeds the read offset at the recording's current end of file so only
    compactions that happen *after* launch are posted — a resumed session's
    recording already holds prior ``chat_compression`` records, and re-posting them
    would flash stale "Conversation compacted" dividers. Self-healing: any error is
    logged and the loop continues (a transient blip never abandons the mirror);
    cancellation propagates for clean teardown. Best-effort, like the approval
    mirror — the offset is in-memory, so a forwarder restart may miss a compaction
    that lands during the gap, which only drops one divider.

    :param recording_path: qwen's chat recording for this session (see
        :func:`omnigent.qwen_native_bridge.qwen_session_recording_path`).
    """
    try:
        offset = recording_path.stat().st_size
    except OSError:
        offset = 0  # not created yet; first poll reads from the start
    timeout = httpx.Timeout(_POST_TIMEOUT_S)
    async with httpx.AsyncClient(
        base_url=base_url, headers=headers, auth=auth, timeout=timeout
    ) as client:
        while True:
            try:
                statuses, offset = await asyncio.to_thread(
                    _read_new_compaction_statuses, recording_path, offset
                )
                for status in statuses:
                    await _post_external_compaction_status(
                        client, session_id=session_id, status=status
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception(
                    "qwen compaction mirror poll failed; session=%s recording=%s",
                    session_id,
                    recording_path,
                )
            await asyncio.sleep(poll_interval_s)
