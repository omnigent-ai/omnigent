"""Mirror a kimi-native TUI session's transcript into the Omnigent web chat.

The kimi-native harness launches the interactive ``kimi`` TUI in a tmux pane and
injects web-UI turns into it (see :mod:`omnigent.kimi_native_bridge`). The TUI's
reply renders live in the embedded terminal, but — unlike the SDK ``KimiExecutor``
— nothing flows the assistant's response back into Omnigent's conversation
transcript (the chat bubbles). This module closes that gap, the kimi analog of
:mod:`omnigent.cursor_native_forwarder`.

Data source: kimi persists each session to an append-only JSONL "wire" log at
``$KIMI_CODE_HOME/sessions/<wd_…>/<session_…>/agents/main/wire.jsonl``. The
native harness points ``KIMI_CODE_HOME`` at ``<bridge_dir>/kimi-code-home`` whose
``sessions/`` store is session-private (see
:mod:`omnigent.kimi_native_credentials`), so discovery only ever sees this
session's own wire logs; ``workDir`` (via ``session_index.jsonl``) and recency
narrow it further. Relevant wire events:

- ``{"type": "turn.prompt", "input": [{"type":"text","text":…}], "origin": {"kind":"user"}}``
  → a user message.
- ``{"type": "context.append_loop_event", "event": {"type": "content.part",
  "part": {"type": "text", "text": …}, "uuid": …}}`` → an assistant message.
  (``part.type == "think"`` is reasoning, mirrored as a transient
  ``external_output_reasoning_delta`` from ``part["think"]``; ``tool.call`` /
  ``tool.result`` events are still skipped — the embedded terminal shows them.)

Each mirrored turn is POSTed as an ``external_conversation_item`` to
``/v1/sessions/{id}/events`` (the same shape :mod:`omnigent.kimi_native_hook`
uses for its read-only approval surface). A per-session byte offset (plus the
line count that keys stable response ids) is persisted in
``<bridge_dir>/kimi_forwarder.json`` so restarts resume without double-posting
and each poll reads only the log's new tail.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from omnigent._native_forwarder_health import (
    note_post_success as note_native_post_success,
)
from omnigent._native_forwarder_health import (
    record_post_failure as record_native_post_failure,
)

_logger = logging.getLogger(__name__)

#: Poll cadence for new wire-log lines (matches cursor_native_forwarder).
_POLL_INTERVAL_S = 0.25
#: Persisted forwarder state (discovered wire path + high-water line count).
_STATE_FILE = "kimi_forwarder.json"
#: Clock-skew tolerance when matching a session created at/after launch.
_DISCOVER_SKEW_MS = 10_000
#: Supervisor backoff bounds.
_BACKOFF_INITIAL_S = 1.0
_BACKOFF_MAX_S = 30.0
#: Consecutive 4xx rejections of one item before it is dropped so a single
#: poison line cannot stall the tail (and any turn edge behind it) forever.
_POISON_MAX_ATTEMPTS = 3
#: Quiet-wire window before an in-flight turn is closed as idle. Deliberately
#: long: kimi appends wire rows only at part/step boundaries, so a slow model
#: step can be legitimately silent for minutes — closing a live turn early
#: would falsely complete a sub-agent, which is worse than closing late.
_TURN_QUIESCENCE_S = 300.0


@dataclass
class _ForwardState:
    """Durable cursor for the wire-log tail.

    ``offset`` is the consumed byte count; ``-1`` marks state persisted by a
    line-only build, from which the offset is re-derived on the next poll.
    """

    wire_path: str
    last_line: int
    offset: int = -1


@dataclass
class KimiWireItem:
    """Stable parsed-wire contract shared by forwarding and offline import."""

    line_no: int
    role: str
    text: str
    response_id: str
    # "message" (a user/assistant turn → external_conversation_item),
    # "reasoning" (a think block → external_output_reasoning_delta),
    # "turn_end" (an ``end_turn``/``length`` step → external_session_status:
    # idle), or "turn_failed" (an error/abort step → status: failed).
    kind: str = "message"
    # Byte offset just past this item's wire line; the tail cursor after it posts.
    offset_after: int = 0


_MirrorItem = KimiWireItem


def clear_kimi_bridge_state(bridge_dir: Path) -> None:
    """Drop any stale forwarder state so a new terminal starts a fresh tail.

    Mirrors ``cursor_native_forwarder.clear_cursor_bridge_state``: without this,
    a re-created terminal would resume the prior session's line offset against a
    different wire log.
    """
    with contextlib.suppress(OSError):
        (bridge_dir / _STATE_FILE).unlink()


def _read_state(bridge_dir: Path) -> _ForwardState | None:
    try:
        raw = (bridge_dir / _STATE_FILE).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    wire_path = data.get("wire_path")
    last_line = data.get("last_line")
    offset = data.get("offset")
    if isinstance(wire_path, str) and isinstance(last_line, int):
        return _ForwardState(
            wire_path=wire_path,
            last_line=last_line,
            offset=offset if isinstance(offset, int) else -1,
        )
    return None


def _write_state(bridge_dir: Path, state: _ForwardState) -> None:
    payload = {"wire_path": state.wire_path, "last_line": state.last_line, "offset": state.offset}
    tmp = bridge_dir / (_STATE_FILE + ".tmp")
    with contextlib.suppress(OSError):
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(bridge_dir / _STATE_FILE)


def workdirs_for_kimi_sessions(kimi_home: Path) -> dict[str, str]:
    """Map each session dir → its ``workDir`` from ``session_index.jsonl``.

    Returns ``{}`` when the index is absent/unreadable (a brand-new home before
    kimi has written any session).
    """
    index = kimi_home / "session_index.jsonl"
    mapping: dict[str, str] = {}
    try:
        text = index.read_text(encoding="utf-8")
    except OSError:
        return mapping
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            session_dir = row.get("sessionDir")
            work_dir = row.get("workDir")
            if isinstance(session_dir, str) and isinstance(work_dir, str):
                mapping[session_dir] = work_dir
    return mapping


_workdirs_for_sessions = workdirs_for_kimi_sessions


def _discover_wire(kimi_home: Path, workspace: str, launch_epoch_ms: int) -> Path | None:
    """Locate the wire log for *workspace*'s newest session created at/after launch.

    Globs ``sessions/*/session_*/agents/main/wire.jsonl`` under *kimi_home*,
    keeps only sessions whose ``session_index`` ``workDir`` matches *workspace*
    (when the index lists them), and returns the most-recently-modified wire log
    whose mtime is at/after ``launch_epoch_ms`` (minus skew). Returns ``None``
    until kimi has created the session.
    """
    sessions_root = kimi_home / "sessions"
    if not sessions_root.exists():
        return None
    workdirs = workdirs_for_kimi_sessions(kimi_home)
    floor_s = (launch_epoch_ms - _DISCOVER_SKEW_MS) / 1000.0
    best: tuple[float, Path] | None = None
    for wire in sessions_root.glob("*/session_*/agents/main/wire.jsonl"):
        # session_index keys on the session dir (…/<wd_…>/<session_…>).
        session_dir = str(wire.parent.parent.parent)
        work_dir = workdirs.get(session_dir)
        # When the index doesn't list it yet, fall back to recency alone — a
        # freshly created session may not be indexed until its first turn.
        if work_dir is not None and work_dir != workspace:
            continue
        try:
            mtime = wire.stat().st_mtime
        except OSError:
            continue
        if mtime < floor_s:
            continue
        if best is None or mtime > best[0]:
            best = (mtime, wire)
    return best[1] if best is not None else None


def _input_text(blocks: object) -> str:
    """Concatenate the ``text`` of an ``input`` / ``content`` block list."""
    if not isinstance(blocks, list):
        return ""
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def _row_to_item(line_no: int, row: dict[str, object]) -> KimiWireItem | None:
    """Map one wire-log row to a conversation item, or ``None`` to skip it."""
    row_type = row.get("type")
    if row_type == "turn.prompt":
        origin = row.get("origin")
        if isinstance(origin, dict) and origin.get("kind") != "user":
            return None
        text = _input_text(row.get("input"))
        if not text:
            return None
        return KimiWireItem(
            line_no=line_no,
            role="user",
            text=text,
            response_id=f"kimi:turn:{line_no}",
        )
    if row_type == "context.append_loop_event":
        event = row.get("event")
        if not isinstance(event, dict):
            return None
        event_type = event.get("type")
        if event_type == "step.end":
            # kimi's agent loop keeps stepping while a step stops for tool use;
            # every other finish reason ends the turn. Without a terminal edge a
            # native sub-agent never reports status and its parent waits forever.
            reason = event.get("finishReason")
            if reason in ("tool_use", "tool_calls"):
                return None
            # end_turn/length delivered their output (idle); anything else
            # (error, abort, …) is an abnormal end surfaced as a failed turn.
            kind = "turn_end" if reason in ("end_turn", "length", "max_tokens") else "turn_failed"
            return KimiWireItem(
                line_no=line_no,
                role="assistant",
                text="",
                response_id=f"kimi:{kind}:{line_no}",
                kind=kind,
            )
        if event_type != "content.part":
            return None
        part = event.get("part")
        if not isinstance(part, dict):
            return None
        uuid = event.get("uuid")
        response_id = f"kimi:{uuid}" if isinstance(uuid, str) and uuid else f"kimi:line:{line_no}"
        part_type = part.get("type")
        if part_type == "text":
            part_text = part.get("text")
            if not isinstance(part_text, str) or not part_text:
                return None
            return KimiWireItem(
                line_no=line_no,
                role="assistant",
                text=part_text,
                response_id=response_id,
            )
        if part_type == "think":
            # Reasoning lives in ``part["think"]`` (not ``part["text"]``). Mirror it
            # as a transient reasoning event so the web UI paints a thinking block —
            # the kimi analogue of codex-native's #1254 reasoning fix.
            think = part.get("think")
            if not isinstance(think, str) or not think:
                return None
            return KimiWireItem(
                line_no=line_no,
                role="assistant",
                text=think,
                response_id=response_id,
                kind="reasoning",
            )
        return None
    return None


def read_kimi_wire_items(wire_path: Path, last_line: int) -> list[KimiWireItem]:
    """Parse wire-log lines beyond *last_line* into the stable shared contract.

    The wire log is append-only JSONL, so a line count is a stable high-water
    mark. Non-JSON / unrecognized lines advance the cursor without emitting.
    """
    try:
        lines = wire_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    items: list[KimiWireItem] = []
    for idx in range(last_line, len(lines)):
        line = lines[idx].strip()
        if not line or not line.startswith("{"):
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        item = _row_to_item(idx, row)
        if item is not None:
            items.append(item)
    return items


_read_new_items = read_kimi_wire_items


def _offset_for_line(wire_path: Path, line: int) -> int:
    """Byte offset of the start of 0-based *line*, for migrating line-only state."""
    if line <= 0:
        return 0
    try:
        data = wire_path.read_bytes()
    except OSError:
        return 0
    offset = 0
    for _ in range(line):
        nl = data.find(b"\n", offset)
        if nl == -1:
            return len(data)
        offset = nl + 1
    return offset


def read_new_kimi_wire_items(
    wire_path: Path, offset: int, line_no: int
) -> tuple[list[KimiWireItem], int, int]:
    """Parse newline-terminated wire rows past byte *offset* into items.

    Incremental tail read: only the bytes past *offset* are read, up to the last
    complete line (a partially-written trailing line is left for the next poll).
    *line_no* is the 0-based number of the first unread line, carried so item
    response ids stay stable across polls. A file smaller than *offset* (a
    recreated log) restarts the tail from the top. Returns
    ``(items, new_offset, new_line_no)`` covering every consumed line; each
    item's ``offset_after``/``line_no`` lets the caller advance per posted item.
    """
    try:
        size = wire_path.stat().st_size
    except OSError:
        return [], offset, line_no
    if size < offset:
        offset, line_no = 0, 0
    if size == offset:
        return [], offset, line_no
    try:
        with open(wire_path, "rb") as fh:
            fh.seek(offset)
            data = fh.read(size - offset)
    except OSError:
        return [], offset, line_no
    last_nl = data.rfind(b"\n")
    if last_nl == -1:
        return [], offset, line_no
    consumed = data[: last_nl + 1]
    items: list[KimiWireItem] = []
    pos = offset
    for raw in consumed.split(b"\n")[:-1]:
        pos += len(raw) + 1
        this_line = line_no
        line_no += 1
        line = raw.decode("utf-8", errors="replace").strip()
        if not line or not line.startswith("{"):
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        item = _row_to_item(this_line, row)
        if item is not None:
            item.offset_after = pos
            items.append(item)
    return items, offset + len(consumed), line_no


async def _post_conversation_item(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    item: KimiWireItem,
    agent_name: str,
) -> None:
    """POST one mirrored turn as an external conversation item."""
    content_type = "input_text" if item.role == "user" else "output_text"
    item_data: dict[str, object] = {
        "role": item.role,
        "content": [{"type": content_type, "text": item.text}],
    }
    if item.role == "assistant":
        item_data["agent"] = agent_name
    body = {
        "type": "external_conversation_item",
        "data": {
            "item_type": "message",
            "item_data": item_data,
            "response_id": item.response_id,
        },
    }
    url = f"{base_url.rstrip('/')}/v1/sessions/{session_id}/events"
    resp = await client.post(url, headers=headers, json=body)
    resp.raise_for_status()


async def _post_external_session_status(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    status: str,
    output: str,
) -> None:
    """POST one ``external_session_status`` event to the Sessions API.

    For a sub-agent conversation the server maps an ``idle`` edge to a terminal
    completion that wakes the parent orchestrator's inbox — the SAME contract
    claude-/codex-/opencode-/cursor-native use. ``output`` carries the turn's
    final assistant text, since the runner delivers an empty result when an idle
    edge forwards none.

    :raises httpx.HTTPError: If the Omnigent request fails or is rejected.
    """
    url = f"{base_url.rstrip('/')}/v1/sessions/{session_id}/events"
    resp = await client.post(
        url,
        headers=headers,
        json={"type": "external_session_status", "data": {"status": status, "output": output}},
    )
    resp.raise_for_status()


async def _post_reasoning_item(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    item: KimiWireItem,
) -> None:
    """POST one mirrored think block as a transient reasoning event.

    Mirrors codex-native (#1254): a one-shot ``external_output_reasoning_delta``
    with ``started: true`` opens a reasoning block in the web UI. Kimi persists
    completed think parts (not streamed deltas), so one delta per part is correct.
    """
    body = {
        "type": "external_output_reasoning_delta",
        "data": {"delta": item.text, "started": True},
    }
    url = f"{base_url.rstrip('/')}/v1/sessions/{session_id}/events"
    resp = await client.post(url, headers=headers, json=body)
    resp.raise_for_status()


async def forward_kimi_wire_to_session(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    bridge_dir: Path,
    kimi_home: Path,
    workspace: str,
    launch_epoch_ms: int,
    agent_name: str = "kimi-native-ui",
    auth: httpx.Auth | None = None,
    pane_alive: Callable[[], bool] | None = None,
    quiescence_s: float = _TURN_QUIESCENCE_S,
    poll_interval_s: float = _POLL_INTERVAL_S,
) -> None:
    """Poll the kimi session wire log and mirror new turns into the chat.

    Runs until cancelled. Discovers the wire log lazily (kimi writes it after the
    first turn), then tails it incrementally past a persisted byte offset,
    POSTing each new user/assistant turn and advancing the cursor per post.

    Besides mirroring, this owns the session's turn-status edges: a ``step.end``
    wire row posts idle (``end_turn``/``length``) or failed (error/abort); a
    dead kimi pane mid-turn posts failed; and a turn whose wire has been quiet
    for *quiescence_s* is closed as idle (an interrupt writes no wire row at
    all), so a crashed or interrupted kimi never strands the session 'running'.

    :param auth: Optional refresh-capable httpx Auth so long sessions survive
        bearer-token expiry (mirrors the qwen forwarder).
    :param pane_alive: Optional probe for the kimi tmux pane's liveness; used
        only to fail a turn whose pane died. ``None`` disables the edge.
    """
    state = _read_state(bridge_dir)
    wire_path = Path(state.wire_path) if state is not None else None
    last_line = state.last_line if state is not None else 0
    offset = state.offset if state is not None else 0
    # Final assistant text of the turn in flight, forwarded on the ``end_turn``
    # edge so the parent's inbox gets the real result instead of an empty one.
    last_assistant_text = ""
    # True from an observed user prompt until a terminal edge posts; gates the
    # pane-death and quiescence fallbacks to turns that actually started.
    turn_open = False
    last_wire_activity = time.monotonic()
    # Consecutive 4xx rejections of the item at the head of the tail.
    poison_id: str | None = None
    poison_attempts = 0
    async with httpx.AsyncClient(timeout=15.0, auth=auth) as client:

        def _persist() -> None:
            _write_state(bridge_dir, _ForwardState(str(wire_path), last_line, offset))

        async def _close_turn(status: str, edge: str) -> None:
            """Post the terminal edge for a turn that will write no more wire rows."""
            nonlocal turn_open, last_assistant_text
            try:
                await _post_external_session_status(
                    client,
                    base_url=base_url,
                    headers=headers,
                    session_id=session_id,
                    status=status,
                    output=last_assistant_text,
                )
            except httpx.HTTPError as exc:
                _logger.warning("kimi forwarder: %s edge failed (will retry): %s", edge, exc)
            else:
                turn_open = False
                last_assistant_text = ""

        while True:
            if wire_path is None or not wire_path.exists():
                discovered = await asyncio.to_thread(
                    _discover_wire, kimi_home, workspace, launch_epoch_ms
                )
                if discovered is not None and discovered != wire_path:
                    wire_path = discovered
                    last_line = 0
                    offset = 0
                    _persist()
            if wire_path is not None and wire_path.exists():
                if offset < 0:
                    # State persisted by a line-only build: derive the byte
                    # offset once so the tail resumes without double-posting.
                    offset = await asyncio.to_thread(_offset_for_line, wire_path, last_line)
                items, new_offset, new_line = await asyncio.to_thread(
                    read_new_kimi_wire_items, wire_path, offset, last_line
                )
                if new_offset != offset:
                    last_wire_activity = time.monotonic()
                all_posted = True
                for item in items:
                    if item.kind == "message" and item.role == "user":
                        turn_open = True
                    try:
                        if item.kind in ("turn_end", "turn_failed"):
                            await _post_external_session_status(
                                client,
                                base_url=base_url,
                                headers=headers,
                                session_id=session_id,
                                status="idle" if item.kind == "turn_end" else "failed",
                                output=last_assistant_text,
                            )
                            last_assistant_text = ""
                            turn_open = False
                        elif item.kind == "reasoning":
                            await _post_reasoning_item(
                                client,
                                base_url=base_url,
                                headers=headers,
                                session_id=session_id,
                                item=item,
                            )
                        else:
                            await _post_conversation_item(
                                client,
                                base_url=base_url,
                                headers=headers,
                                session_id=session_id,
                                item=item,
                                agent_name=agent_name,
                            )
                            if item.role == "assistant":
                                last_assistant_text = item.text
                    except httpx.HTTPError as exc:
                        event_type = (
                            "external_session_status"
                            if item.kind in ("turn_end", "turn_failed")
                            else "external_conversation_item"
                        )
                        status_code = (
                            exc.response.status_code
                            if isinstance(exc, httpx.HTTPStatusError)
                            else None
                        )
                        if status_code is None:
                            # Transport failure: attribute it so the idle-turn
                            # watchdog can name connectivity as the cause.
                            record_native_post_failure(event_type, exc)
                        else:
                            # A status proves the server was reachable.
                            note_native_post_success()
                        if status_code is not None and 400 <= status_code < 500:
                            if poison_id == item.response_id:
                                poison_attempts += 1
                            else:
                                poison_id, poison_attempts = item.response_id, 1
                            if poison_attempts >= _POISON_MAX_ATTEMPTS:
                                _logger.error(
                                    "kimi forwarder: dropping item %s rejected %d times: %s",
                                    item.response_id,
                                    poison_attempts,
                                    exc,
                                )
                                poison_id, poison_attempts = None, 0
                                offset = item.offset_after
                                last_line = item.line_no + 1
                                _persist()
                                continue
                        _logger.warning("kimi forwarder: POST failed (will retry): %s", exc)
                        all_posted = False
                        break
                    else:
                        note_native_post_success()
                        if poison_id == item.response_id:
                            poison_id, poison_attempts = None, 0
                        offset = item.offset_after
                        last_line = item.line_no + 1
                        _persist()
                if all_posted and new_offset != offset:
                    # Consume trailing non-item lines so the next poll skips them.
                    offset = new_offset
                    last_line = new_line
                    _persist()
            if turn_open and pane_alive is not None and not pane_alive():
                # The pane died mid-turn: no further wire rows are coming, so
                # post the failed edge instead of stranding the session.
                await _close_turn("failed", "pane-death")
            elif turn_open and time.monotonic() - last_wire_activity > quiescence_s:
                # Turns that end without any wire edge (interrupt, wedged kimi):
                # a long-quiet wire means the turn is over; close it as idle.
                await _close_turn("idle", "quiescence")
            await asyncio.sleep(poll_interval_s)


async def supervise_kimi_forwarder(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    bridge_dir: Path,
    kimi_home: Path,
    workspace: str,
    launch_epoch_ms: int,
    agent_name: str = "kimi-native-ui",
    auth: httpx.Auth | None = None,
    pane_alive: Callable[[], bool] | None = None,
) -> None:
    """Run :func:`forward_kimi_wire_to_session` with restart-on-crash backoff.

    Propagates :class:`asyncio.CancelledError` cleanly (terminal teardown), but
    restarts on any other exception with exponential backoff — mirrors
    ``cursor_native_forwarder.supervise_cursor_forwarder``. ``auth`` /
    ``pane_alive`` pass through to the forward loop.
    """
    backoff = _BACKOFF_INITIAL_S
    while True:
        try:
            await forward_kimi_wire_to_session(
                base_url=base_url,
                headers=headers,
                session_id=session_id,
                bridge_dir=bridge_dir,
                kimi_home=kimi_home,
                workspace=workspace,
                launch_epoch_ms=launch_epoch_ms,
                agent_name=agent_name,
                auth=auth,
                pane_alive=pane_alive,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            _logger.exception("kimi forwarder crashed for session %s; restarting", session_id)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX_S)
        else:
            return
