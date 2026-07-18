"""TUI→web forwarder for the coco-native harness.

The ``omnigent coco`` wrapper launches the real Snowflake CoCo (Cortex Code)
TUI in a runner-owned tmux pane, and :mod:`omnigent.coco_native_bridge` injects
web-UI messages into it. That covers the web→TUI direction; this module is the
missing mirror back — the CoCo analog of
:mod:`omnigent.goose_native_forwarder`.

Unlike goose (live SQLite store) CoCo flushes its transcript lazily, so a bare
store tail would sit empty for whole turns. Instead the bridge registers a
lifecycle hook (:mod:`omnigent.coco_native_hook`) in the per-session
``SNOWFLAKE_HOME`` and this forwarder tails the hook's
``<bridge_dir>/hook_events.jsonl``:

- ``SessionStart`` / ``UserPromptSubmit`` events carry the CoCo
  ``session_id`` + ``transcript_path`` — the *only* reliable discovery in TUI
  mode, where CoCo mints its own id and ignores ``--session-id`` (verified on
  v1.1.1). The id is persisted so cold resume can launch ``cortex -r <id>``.
- ``UserPromptSubmit`` opens a turn (``running`` status edge).
- ``Stop`` closes a turn: CoCo has flushed
  ``conversations/<session_id>.history.jsonl`` by the time the Stop hook fires
  (verified on v1.1.1), so the finished turn is mirrored from that file past a
  persisted line cursor. History rows carry Anthropic-style content blocks —
  ``text`` / ``tool_use`` / ``tool_result`` — which map onto ``message`` /
  ``function_call`` / ``function_call_output`` conversation items.

Because both the event log and the line cursor are durable, a forwarder
restart resumes exactly where it left off with no replay pass: unprocessed
hook events are still in the file.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from omnigent._native_post_delivery import post_external_session_status
from omnigent.coco_native_hook import HOOK_EVENTS_FILE

_logger = logging.getLogger(__name__)

#: Seconds between hook-event polls. Events arrive only at turn boundaries
#: (prompt submit / stop), so this is latency-to-notice, not a hot loop.
_DEFAULT_POLL_INTERVAL_S = 0.4
_POST_TIMEOUT_S = 30.0

#: Seconds of inactivity after which an open turn's live card is closed.
#: Backstop for turns that died without a ``Stop`` hook (TUI killed mid-turn,
#: CoCo crash). Minutes, not seconds: a long tool call emits no hook events
#: while it runs.
_STALLED_TURN_IDLE_S = 300.0

#: After a ``Stop`` event, how long to keep re-reading the transcript for the
#: turn's rows before closing the turn anyway. CoCo flushes before firing the
#: hook, so this only papers over a slow filesystem, not a design gap.
_STOP_FLUSH_GRACE_S = 3.0

# Supervisor backoff (mirrors goose_native_forwarder.supervise_goose_forwarder).
_SUPERVISOR_INITIAL_BACKOFF_S = 1.0
_SUPERVISOR_MAX_BACKOFF_S = 30.0
_SUPERVISOR_HEALTHY_UPTIME_S = 60.0

_STATE_FILE = "coco_forwarder.json"

# The executor injects ``[Attached: <path>]`` markers for web-UI attachments
# before pasting into the TUI; strip them from the mirrored bubble (the path is
# an internal bridge detail).
_ATTACHMENT_MARKER_RE = re.compile(r"\[Attached:[^\]]*\]")

# CoCo prepends its injected context (skills, SQL rules, todo nags, ...) to the
# stored user message as ``<system-reminder>...</system-reminder>`` text blocks
# ahead of the user's own text (verified on v1.1.1). Strip them from the
# mirrored user bubble — they are vendor-internal prompt plumbing.
_SYSTEM_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>\s*", re.DOTALL)


@dataclass
class _ForwardState:
    """Durable forwarder cursor, persisted to ``bridge_dir/coco_forwarder.json``.

    :param coco_session_id: The CoCo session id being mirrored (from the hook
        events), or ``None`` before the first ``SessionStart`` arrives.
    :param history_path: The resolved ``<session_id>.history.jsonl`` path.
    :param last_line: Count of history lines already processed (mirrored or
        skipped). Lines are append-ordered, so the count is sufficient dedup.
    :param events_offset: Byte offset into ``hook_events.jsonl`` already
        consumed. The event log is append-only, so no event is ever lost to a
        forwarder restart.
    :param turn_seq: Monotonic per-session turn counter minting the
        ``coco:turn:<n>`` response ids that group one turn's items.
    :param turn_live: Whether a ``running`` edge for ``coco:turn:<turn_seq>``
        was posted and not yet closed. Persisted so a forwarder restart can
        close (or keep stamping) the open turn instead of orphaning its
        spinner or minting a duplicate turn id for the same prompt.
    """

    coco_session_id: str | None = None
    history_path: str | None = None
    last_line: int = 0
    events_offset: int = 0
    turn_seq: int = 0
    turn_live: bool = False


def _read_state(bridge_dir: Path) -> _ForwardState:
    """Load the persisted forward cursor, or a cold default."""
    try:
        raw = (bridge_dir / _STATE_FILE).read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return _ForwardState()
    if not isinstance(data, dict):
        return _ForwardState()
    state = _ForwardState()
    sid = data.get("coco_session_id")
    state.coco_session_id = sid if isinstance(sid, str) else None
    history_path = data.get("history_path")
    state.history_path = history_path if isinstance(history_path, str) else None
    for attr in ("last_line", "events_offset", "turn_seq"):
        value = data.get(attr)
        if isinstance(value, int) and value >= 0:
            setattr(state, attr, value)
    state.turn_live = data.get("turn_live") is True
    return state


def _write_state(bridge_dir: Path, state: _ForwardState) -> bool:
    """Atomically persist the forward cursor (tmp write + rename)."""
    try:
        bridge_dir.mkdir(parents=True, exist_ok=True)
        tmp = bridge_dir / (_STATE_FILE + ".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "coco_session_id": state.coco_session_id,
                    "history_path": state.history_path,
                    "last_line": state.last_line,
                    "events_offset": state.events_offset,
                    "turn_seq": state.turn_seq,
                    "turn_live": state.turn_live,
                }
            ),
            encoding="utf-8",
        )
        os.replace(tmp, bridge_dir / _STATE_FILE)
        return True
    except OSError:
        _logger.warning("coco forwarder could not persist state to %s", bridge_dir, exc_info=True)
        return False


def clear_coco_bridge_state(bridge_dir: Path) -> None:
    """Drop the persisted cursor and event log so a re-created terminal starts clean."""
    for name in (_STATE_FILE, HOOK_EVENTS_FILE):
        with contextlib.suppress(OSError):
            (bridge_dir / name).unlink()


def _history_path_for_transcript(transcript_path: str) -> str | None:
    """Derive the message-history JSONL path from a hook ``transcript_path``.

    The hook payload points at the session's metadata file
    (``<session_id>.json``); the messages live in the sibling
    ``<session_id>.history.jsonl``.
    """
    if not transcript_path:
        return None
    if transcript_path.endswith(".history.jsonl"):
        return transcript_path
    if transcript_path.endswith(".json"):
        return transcript_path[: -len(".json")] + ".history.jsonl"
    return None


def _read_new_events(events_path: Path, offset: int) -> tuple[list[dict[str, Any]], int]:
    """Read whole JSONL events past *offset*; return them and the new offset.

    A trailing partial line (hook mid-write) is left unconsumed so the next
    poll re-reads it complete.
    """
    try:
        with open(events_path, "rb") as fh:
            fh.seek(offset)
            chunk = fh.read()
    except OSError:
        return [], offset
    if not chunk:
        return [], offset
    events: list[dict[str, Any]] = []
    consumed = 0
    for raw_line in chunk.splitlines(keepends=True):
        if not raw_line.endswith(b"\n"):
            break
        consumed += len(raw_line)
        try:
            obj = json.loads(raw_line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events, offset + consumed


@dataclass
class _MirrorItem:
    """One conversation item ready to POST."""

    item_type: str
    item_data: dict[str, object]
    response_id: str


def _block_text(block: dict[str, Any]) -> str:
    """Extract prose from a ``text`` content block."""
    text = block.get("text")
    return text if isinstance(text, str) else ""


def _tool_result_text(inner: Any) -> str:
    """Flatten a ``tool_result`` block's ``content`` into plain text."""
    if isinstance(inner, str):
        return inner
    if isinstance(inner, dict):
        content = inner.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for part in content:
                if isinstance(part, str):
                    parts.append(part)
                elif isinstance(part, dict):
                    text = part.get("text")
                    if isinstance(text, str):
                        parts.append(text)
            return "".join(parts)
    return ""


def _message_to_items(
    line_no: int,
    obj: dict[str, Any],
    agent_name: str,
    turn_response_id: str | None,
) -> list[_MirrorItem]:
    """Convert one history row to zero or more mirror items.

    CoCo rows are ``{"role": "user"|"assistant", "content": [blocks], ...}``
    with Anthropic-style blocks. ``role=user`` rows carry both real user turns
    (``text`` blocks) and tool-result carrier messages (``tool_result``
    blocks); each block maps independently.

    :param turn_response_id: The open turn's response id, or ``None`` when no
        turn is open (items then get a per-line fallback id).
    """
    role = obj.get("role")
    content = obj.get("content")
    if role not in ("user", "assistant") or not isinstance(content, list):
        return []

    per_line_id = f"coco:{line_no}"
    rid = turn_response_id or per_line_id
    items: list[_MirrorItem] = []
    text_parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text_parts.append(_block_text(block))
        elif block_type == "tool_use" and role == "assistant":
            inner = block.get("tool_use")
            if not isinstance(inner, dict):
                continue
            call_id = inner.get("tool_use_id")
            if not isinstance(call_id, str) or not call_id:
                continue
            name = inner.get("name")
            raw_input = inner.get("input", {})
            try:
                args_json = json.dumps(raw_input) if not isinstance(raw_input, str) else raw_input
            except (TypeError, ValueError):
                args_json = "{}"
            items.append(
                _MirrorItem(
                    item_type="function_call",
                    item_data={
                        "agent": agent_name,
                        "name": name if isinstance(name, str) else str(name),
                        "arguments": args_json,
                        "call_id": call_id,
                    },
                    response_id=rid,
                )
            )
        elif block_type == "tool_result":
            inner = block.get("tool_result")
            if not isinstance(inner, dict):
                continue
            call_id = inner.get("tool_use_id")
            if not isinstance(call_id, str) or not call_id:
                continue
            items.append(
                _MirrorItem(
                    item_type="function_call_output",
                    item_data={"call_id": call_id, "output": _tool_result_text(inner)},
                    response_id=rid,
                )
            )

    text = "".join(text_parts)
    if role == "user":
        text = _SYSTEM_REMINDER_RE.sub("", text)
    text = _ATTACHMENT_MARKER_RE.sub("", text).strip()
    if text:
        if role == "user":
            items.insert(
                0,
                _MirrorItem(
                    item_type="message",
                    item_data={"role": "user", "content": [{"type": "input_text", "text": text}]},
                    # User bubbles are standalone, never part of the reply's
                    # streaming group.
                    response_id=per_line_id,
                ),
            )
        else:
            items.append(
                _MirrorItem(
                    item_type="message",
                    item_data={
                        "role": "assistant",
                        "agent": agent_name,
                        "content": [{"type": "output_text", "text": text}],
                    },
                    response_id=rid,
                )
            )
    return items


def _read_history_lines(history_path: str, last_line: int) -> list[tuple[int, dict[str, Any]]]:
    """Read parsed history rows past the *last_line* cursor.

    Returns ``(line_no, row)`` pairs with 1-based line numbers. A trailing
    line that fails to parse is treated as mid-write and excluded (the next
    read retries it); an interior unparsable line is skipped for good.
    """
    try:
        with open(history_path, encoding="utf-8") as fh:
            raw_lines = fh.readlines()
    except OSError:
        return []
    rows: list[tuple[int, dict[str, Any]]] = []
    for idx, raw in enumerate(raw_lines[last_line:], start=last_line + 1):
        stripped = raw.strip()
        if not stripped:
            rows.append((idx, {}))
            continue
        try:
            obj = json.loads(stripped)
        except ValueError:
            if idx == len(raw_lines) and not raw.endswith("\n"):
                break
            rows.append((idx, {}))
            continue
        rows.append((idx, obj if isinstance(obj, dict) else {}))
    return rows


def _count_history_lines(history_path: str) -> int:
    """Return the current number of lines in *history_path* (0 when unreadable)."""
    try:
        with open(history_path, "rb") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0


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


class _TurnTracker:
    """Live-card lifecycle for the turn currently being mirrored (in-memory).

    Durable dedup lives in :class:`_ForwardState`; this only tracks whether a
    ``running`` edge is open and when hook activity was last seen, for the
    stalled-turn backstop.
    """

    def __init__(self) -> None:
        self.response_id: str | None = None
        self.live = False
        self.last_activity_ts: float | None = None

    def touch(self) -> None:
        self.last_activity_ts = time.monotonic()

    def reset(self) -> None:
        self.response_id = None
        self.live = False
        self.last_activity_ts = None


async def forward_coco_events_to_session(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    bridge_dir: Path,
    agent_name: str,
    on_coco_session_id: Any | None = None,
    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
    auth: httpx.Auth | None = None,
) -> None:
    """Tail the hook event log and mirror finished CoCo turns into the session.

    :param base_url: Omnigent server base URL.
    :param headers: Static HTTP headers (auth normally via ``auth``).
    :param session_id: Omnigent session/conversation id.
    :param bridge_dir: The coco-native bridge dir (event log + cursor).
    :param agent_name: Agent label stamped on mirrored assistant items.
    :param on_coco_session_id: Optional async callback invoked once per newly
        discovered CoCo session id (used to persist ``external_session_id``
        for resume).
    :param poll_interval_s: Seconds between event-log polls.
    :param auth: Optional refresh-capable httpx Auth for remote deployments.
    :returns: Never normally returns; cancel the task to stop it.
    """
    events_path = bridge_dir / HOOK_EVENTS_FILE
    state = _read_state(bridge_dir)
    turn = _TurnTracker()
    # Restore an open turn across a forwarder restart: without this the
    # previous run's ``running`` edge could never be closed, and re-processing
    # a not-yet-persisted UserPromptSubmit would mint a duplicate turn id.
    if state.turn_live:
        turn.response_id = f"coco:turn:{state.turn_seq}"
        turn.live = True
        turn.touch()
    timeout = httpx.Timeout(_POST_TIMEOUT_S)

    async def _mirror_new_rows(client: httpx.AsyncClient) -> int:
        """Mirror history rows past the cursor; returns how many were posted."""
        if state.history_path is None:
            return 0
        # CoCo can rewrite the history file shorter (compaction / summarize).
        # Snap the cursor down so the mirror resumes from the rewritten tail
        # instead of going silently dead for the rest of the session; the
        # skipped span was already mirrored in its pre-rewrite form.
        current_lines = await asyncio.to_thread(_count_history_lines, state.history_path)
        if current_lines < state.last_line:
            _logger.warning(
                "coco history shrank (%d -> %d lines); resetting mirror cursor; session=%s",
                state.last_line,
                current_lines,
                session_id,
            )
            state.last_line = current_lines
            _write_state(bridge_dir, state)
        rows = await asyncio.to_thread(_read_history_lines, state.history_path, state.last_line)
        posted = 0
        for line_no, obj in rows:
            items = _message_to_items(line_no, obj, agent_name, turn.response_id)
            for item in items:
                await _post_conversation_item(client, session_id=session_id, item=item)
                posted += 1
            state.last_line = line_no
            _write_state(bridge_dir, state)
        return posted

    async def _close_turn(client: httpx.AsyncClient) -> None:
        if turn.live:
            await post_external_session_status(
                client, session_id=session_id, status="idle", response_id=turn.response_id
            )
        turn.reset()
        if state.turn_live:
            state.turn_live = False
            _write_state(bridge_dir, state)

    async def _adopt_session(client: httpx.AsyncClient, event: dict[str, Any]) -> None:
        """Bind to the CoCo session a hook event announced, if new."""
        coco_sid = event.get("session_id")
        if not isinstance(coco_sid, str) or not coco_sid or coco_sid == state.coco_session_id:
            return
        history_path = _history_path_for_transcript(str(event.get("transcript_path") or ""))
        if history_path is None:
            return
        # A turn left open by the previous CoCo session can never close now.
        await _close_turn(client)
        state.coco_session_id = coco_sid
        state.history_path = history_path
        # Resumed/forked sessions open with their prior history already in the
        # transcript; Omnigent already holds those messages, so mirror only
        # rows appended from here on. A fresh session's file has 0 lines (or
        # does not exist yet), so this is also the correct fresh-start cursor.
        state.last_line = await asyncio.to_thread(_count_history_lines, history_path)
        _write_state(bridge_dir, state)
        if on_coco_session_id is not None:
            try:
                await on_coco_session_id(coco_sid)
            except Exception:  # noqa: BLE001 — persistence is best-effort; resume self-heals
                _logger.warning(
                    "coco forwarder external-session-id callback failed; session=%s",
                    session_id,
                    exc_info=True,
                )

    async with httpx.AsyncClient(
        base_url=base_url, headers=headers, auth=auth, timeout=timeout
    ) as client:
        while True:
            try:
                events, new_offset = await asyncio.to_thread(
                    _read_new_events, events_path, state.events_offset
                )
                for event in events:
                    kind = event.get("hook_event_name")
                    if kind in ("SessionStart", "UserPromptSubmit", "Stop"):
                        await _adopt_session(client, event)
                    if kind == "UserPromptSubmit":
                        # Mirror the just-submitted user bubble (and any rows
                        # CoCo flushed since the last turn) before opening the
                        # new turn, so pre-turn rows keep standalone ids.
                        await _mirror_new_rows(client)
                        # A submit while a turn is live is mid-turn steering
                        # (CoCo queues it); the open turn keeps running, so
                        # only refresh activity — never close or re-mint.
                        if not turn.live:
                            state.turn_seq += 1
                            state.turn_live = True
                            turn.response_id = f"coco:turn:{state.turn_seq}"
                            _write_state(bridge_dir, state)
                            await post_external_session_status(
                                client,
                                session_id=session_id,
                                status="running",
                                response_id=turn.response_id,
                            )
                            turn.live = True
                        turn.touch()
                    elif kind == "Stop":
                        # CoCo flushes the transcript before firing Stop;
                        # retry briefly in case the write is still landing.
                        deadline = time.monotonic() + _STOP_FLUSH_GRACE_S
                        posted = await _mirror_new_rows(client)
                        while posted == 0 and time.monotonic() < deadline:
                            await asyncio.sleep(0.2)
                            posted = await _mirror_new_rows(client)
                        await _close_turn(client)
                # Advance past malformed-only chunks too, or the same bytes
                # would be re-read (and re-fail) on every poll forever.
                if new_offset != state.events_offset:
                    state.events_offset = new_offset
                    _write_state(bridge_dir, state)

                # Backstop: a turn that died without its Stop hook (killed
                # pane, CoCo crash) must not leave a spinner forever. Hook
                # events are the only mid-turn activity signal, so a
                # legitimately long turn also trips this — like goose, keep
                # ``response_id`` so the eventual Stop's rows rejoin the same
                # streaming group even though the spinner closed early.
                if (
                    turn.live
                    and turn.last_activity_ts is not None
                    and time.monotonic() - turn.last_activity_ts > _STALLED_TURN_IDLE_S
                ):
                    await _mirror_new_rows(client)
                    await post_external_session_status(
                        client,
                        session_id=session_id,
                        status="idle",
                        response_id=turn.response_id,
                    )
                    turn.live = False
                    turn.last_activity_ts = None
                    state.turn_live = False
                    _write_state(bridge_dir, state)

            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception(
                    "coco forwarder poll failed; session=%s coco_session=%s",
                    session_id,
                    state.coco_session_id,
                )
            await asyncio.sleep(poll_interval_s)


def _supervisor_monotonic() -> float:
    """Indirection so tests can stub the supervisor's clock."""
    return time.monotonic()


async def _supervisor_sleep(seconds: float) -> None:
    """Indirection so tests can stub the supervisor's backoff sleep."""
    await asyncio.sleep(seconds)


async def supervise_coco_forwarder(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    bridge_dir: Path,
    agent_name: str,
    on_coco_session_id: Any | None = None,
    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
    auth: httpx.Auth | None = None,
) -> None:
    """Run :func:`forward_coco_events_to_session` under a restart supervisor.

    Mirrors :func:`omnigent.goose_native_forwarder.supervise_goose_forwarder`:
    bounded exponential backoff, :class:`asyncio.CancelledError` propagates for
    clean teardown, and the persisted cursors mean restarts resume exactly
    where they left off.

    :returns: Never normally returns; cancel the task to stop it.
    """
    backoff_s = _SUPERVISOR_INITIAL_BACKOFF_S
    while True:
        run_started_at = _supervisor_monotonic()
        crash_exc: Exception | None = None
        try:
            await forward_coco_events_to_session(
                base_url=base_url,
                headers=headers,
                session_id=session_id,
                bridge_dir=bridge_dir,
                agent_name=agent_name,
                on_coco_session_id=on_coco_session_id,
                poll_interval_s=poll_interval_s,
                auth=auth,
            )
            _logger.warning(
                "coco forwarder returned unexpectedly; restarting; session=%s bridge_dir=%s",
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
                "coco forwarder crashed; restarting in %.1fs; session=%s bridge_dir=%s",
                backoff_s,
                session_id,
                bridge_dir,
                exc_info=crash_exc,
            )
        await _supervisor_sleep(backoff_s)
        backoff_s = min(backoff_s * 2.0, _SUPERVISOR_MAX_BACKOFF_S)
