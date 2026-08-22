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
  ``tool.result`` events are never posted — the embedded terminal shows them —
  but are tracked so a long-running tool doesn't look like an orphaned turn.)
- ``{"type": "turn.ended", "reason": "completed" | "failed" | "cancelled", …}``
  → the authoritative terminal record for a turn. Failed and cancelled turns
  emit no ``step.end`` at all (see ``tests/fixtures/kimi_wire``), so ``step.end``
  finish reasons are never treated as turn edges.

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
#: Supervisor backoff bounds (also reused for per-POST retry backoff).
_BACKOFF_INITIAL_S = 1.0
_BACKOFF_MAX_S = 30.0
#: Consecutive 4xx rejections of one item before it is dropped so a single
#: poison line cannot stall the tail (and any turn edge behind it) forever.
_POISON_MAX_ATTEMPTS = 3
#: 4xx statuses that are transient (auth lapse, rate limit, contention), never
#: poison: the item is retried with backoff instead of being discarded.
_TRANSIENT_POST_STATUSES = frozenset({401, 403, 408, 409, 425, 429})
#: Quiet-wire window before an in-flight turn is closed as idle. Deliberately
#: long: kimi appends wire rows only at part/step boundaries, so a slow model
#: step can be legitimately silent for minutes — closing a live turn early
#: would falsely complete a sub-agent, which is worse than closing late.
_TURN_QUIESCENCE_S = 300.0
#: Ceiling on wire silence while a tool call is in flight. A tool may
#: legitimately run for many minutes, but a wire with NO row of any kind for
#: this long means the tool (or kimi itself) hung — fail the turn instead of
#: suppressing quiescence forever against an alive-but-wedged pane. Accepted
#: tradeoff: a zero-row tool can strand a turn for up to this long; per-tool
#: heartbeats/limits are deliberately not implemented.
_TOOL_QUIESCENCE_S = 1800.0
#: Continuous edge-delivery failure past this long logs an error (rate-limited
#: to the same interval) so the outage is visible without extra plumbing.
_EDGE_FAILURE_ALERT_S = 300.0
#: Minimum silence window left after a restart re-seeds the activity clock
#: (a few poll intervals): a forward wall-clock jump (sleep/wake, NTP step)
#: must not read as elapsed silence and fire a watchdog before the resumed
#: kimi has had a moment to write a row.
_RESTART_GRACE_S = 1.0
#: Sentinel: the wire's first line is partial or unparseable (mid-write) —
#: defer adoption for this discovery cycle instead of trusting the coarse floor.
_HEADER_UNREADABLE = object()


@dataclass
class _ForwardState:
    """Durable cursor for the wire-log tail plus the mirrored turn lifecycle.

    ``turn_open``/``tools_in_flight`` keep the pane-death and quiescence
    fallbacks armed (or suppressed) across a forwarder restart mid-turn;
    ``last_edge_id`` dedupes a turn-edge POST replayed after a crash landed
    between the POST and the cursor persist.
    """

    wire_path: str
    last_line: int
    offset: int
    turn_open: bool = False
    tools_in_flight: int = 0
    last_edge_id: str | None = None
    dropped_edge_status: str | None = None
    # Wall-clock of the last genuine wire activity, so a restart resumes the
    # quiescence timers instead of re-arming their full windows.
    last_activity_ts: float | None = None
    # High-water of wire bytes observed (>= the delivery cursor), so a crash
    # loop replaying undelivered rows can't refresh the timers every restart.
    last_seen_offset: int | None = None


@dataclass
class KimiWireItem:
    """Stable parsed-wire contract shared by forwarding and offline import."""

    line_no: int
    role: str
    text: str
    response_id: str
    # "message" (a user/assistant turn → external_conversation_item),
    # "reasoning" (a think block → external_output_reasoning_delta),
    # "turn_end" (turn.ended completed/cancelled → external_session_status:
    # idle), "turn_failed" (turn.ended failed → status: failed), or
    # "tool_call"/"tool_result" (never posted; in-flight tool bookkeeping).
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
    last_edge_id = data.get("last_edge_id")
    tools_in_flight = data.get("tools_in_flight")
    dropped_edge_status = data.get("dropped_edge_status")
    last_activity_ts = data.get("last_activity_ts")
    last_seen_offset = data.get("last_seen_offset")
    if isinstance(wire_path, str) and isinstance(last_line, int) and isinstance(offset, int):
        return _ForwardState(
            wire_path=wire_path,
            last_line=last_line,
            offset=offset,
            turn_open=data.get("turn_open") is True,
            tools_in_flight=tools_in_flight if isinstance(tools_in_flight, int) else 0,
            last_edge_id=last_edge_id if isinstance(last_edge_id, str) else None,
            dropped_edge_status=(
                dropped_edge_status if isinstance(dropped_edge_status, str) else None
            ),
            last_activity_ts=(
                float(last_activity_ts) if isinstance(last_activity_ts, (int, float)) else None
            ),
            last_seen_offset=last_seen_offset if isinstance(last_seen_offset, int) else None,
        )
    return None


def _write_state(bridge_dir: Path, state: _ForwardState) -> None:
    payload = {
        "wire_path": state.wire_path,
        "last_line": state.last_line,
        "offset": state.offset,
        "turn_open": state.turn_open,
        "tools_in_flight": state.tools_in_flight,
        "last_edge_id": state.last_edge_id,
        "dropped_edge_status": state.dropped_edge_status,
        "last_activity_ts": state.last_activity_ts,
        "last_seen_offset": state.last_seen_offset,
    }
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


def _wire_created_at_ms(wire: Path) -> int | None | object:
    """``created_at`` (ms epoch) of the wire's first row — kimi's metadata header.

    Returns ``None`` only for a COMPLETE first line that carries no timestamp;
    a partial or unparseable first line (mid-write) returns
    :data:`_HEADER_UNREADABLE` so discovery retries next poll instead of
    falling back to the coarse mtime floor.
    """
    try:
        with open(wire, "rb") as fh:
            first = fh.readline(4096)
    except OSError:
        return _HEADER_UNREADABLE
    if not first.endswith(b"\n"):
        return _HEADER_UNREADABLE
    try:
        row = json.loads(first)
    except ValueError:
        return _HEADER_UNREADABLE
    if isinstance(row, dict):
        created_at = row.get("created_at")
        if isinstance(created_at, int):
            return created_at
    return None


def _discover_wire(kimi_home: Path, workspace: str, launch_epoch_ms: int) -> Path | None:
    """Locate the wire log for *workspace*'s newest session created at/after launch.

    Globs ``sessions/*/session_*/agents/main/wire.jsonl`` under *kimi_home*,
    keeps only sessions whose ``session_index`` ``workDir`` matches *workspace*
    (when the index lists them), and returns the most-recently-modified wire log
    whose mtime is at/after ``launch_epoch_ms``. Returns ``None`` until kimi has
    created the session.
    """
    sessions_root = kimi_home / "sessions"
    if not sessions_root.exists():
        return None
    workdirs = workdirs_for_kimi_sessions(kimi_home)
    # Pin adoption to the exact launch epoch: the private sessions store
    # survives terminal re-creation, so a prior launch's wire that ended just
    # before this launch must not be re-adopted. A whole-second mtime may be
    # filesystem truncation, so it only needs to reach the launch second.
    floor_ns = launch_epoch_ms * 1_000_000
    floor_coarse_ns = (launch_epoch_ms // 1000) * 1_000_000_000
    best: tuple[int, Path] | None = None
    for wire in sessions_root.glob("*/session_*/agents/main/wire.jsonl"):
        # session_index keys on the session dir (…/<wd_…>/<session_…>).
        session_dir = str(wire.parent.parent.parent)
        work_dir = workdirs.get(session_dir)
        # When the index doesn't list it yet, fall back to recency alone — a
        # freshly created session may not be indexed until its first turn.
        if work_dir is not None and work_dir != workspace:
            continue
        try:
            mtime_ns = wire.stat().st_mtime_ns
        except OSError:
            continue
        precise = mtime_ns % 1_000_000_000 != 0
        if mtime_ns < (floor_ns if precise else floor_coarse_ns):
            continue
        if not precise and mtime_ns < floor_ns:
            # A truncated mtime in the launch second can't tell before from
            # after; let the wire's own metadata header break the tie, keeping
            # the coarse floor only when a COMPLETE first line has no timestamp.
            created_at = _wire_created_at_ms(wire)
            if created_at is _HEADER_UNREADABLE:
                # Header mid-write: defer this candidate to the next poll.
                continue
            if isinstance(created_at, int) and created_at < launch_epoch_ms:
                continue
        if best is None or mtime_ns > best[0]:
            best = (mtime_ns, wire)
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
    if row_type == "turn.ended":
        # The authoritative terminal record: failed and cancelled turns write no
        # step.end at all, so step.end finish reasons never drive turn edges.
        reason = row.get("reason")
        if reason in ("completed", "cancelled"):
            return KimiWireItem(
                line_no=line_no,
                role="assistant",
                text="",
                response_id=f"kimi:turn_end:{line_no}",
                kind="turn_end",
            )
        if reason == "failed":
            error = row.get("error")
            message = error.get("message") if isinstance(error, dict) else None
            return KimiWireItem(
                line_no=line_no,
                role="assistant",
                text=message if isinstance(message, str) else "",
                response_id=f"kimi:turn_failed:{line_no}",
                kind="turn_failed",
            )
        # Unknown vocabulary: never guess "failed" — leave the turn open for
        # the pane-death/quiescence fallbacks to close.
        _logger.warning("kimi forwarder: unknown turn.ended reason %r (line %d)", reason, line_no)
        return None
    if row_type == "context.append_loop_event":
        event = row.get("event")
        if not isinstance(event, dict):
            return None
        event_type = event.get("type")
        if event_type in ("tool.call", "tool.result"):
            # Bookkeeping only (never posted): an open tool call keeps the wire
            # legitimately silent, which must suppress the quiescence fallback.
            kind = "tool_call" if event_type == "tool.call" else "tool_result"
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
    tool_quiescence_s: float = _TOOL_QUIESCENCE_S,
    poll_interval_s: float = _POLL_INTERVAL_S,
    post_backoff_initial_s: float = _BACKOFF_INITIAL_S,
) -> None:
    """Poll the kimi session wire log and mirror new turns into the chat.

    Runs until cancelled. Discovers the wire log lazily (kimi writes it after the
    first turn), then tails it incrementally past a persisted byte offset,
    POSTing each new user/assistant turn and advancing the cursor per post.

    Besides mirroring, this owns the session's turn-status edges: the top-level
    ``turn.ended`` wire record posts idle (completed/cancelled) or failed; a
    dead kimi pane mid-turn posts failed; a turn whose wire has been quiet
    for *quiescence_s* with no tool call in flight is closed as idle; and a
    turn silent past *tool_quiescence_s* WITH a tool in flight is failed (the
    tool hung), so a crashed or wedged kimi never strands the session
    'running'. The turn lifecycle is persisted alongside the tail cursor, so
    the fallbacks stay armed across a forwarder restart mid-turn.

    :param auth: Optional refresh-capable httpx Auth so long sessions survive
        bearer-token expiry (mirrors the qwen forwarder).
    :param pane_alive: Optional probe for the kimi tmux pane's liveness; used
        only to fail a turn whose pane died. ``None`` disables the edge.
    :param post_backoff_initial_s: Initial backoff after a failed POST, doubling
        up to a cap so an auth/rate-limit outage never becomes a request storm.
    """
    # Route the transcript mirror to the replica holding this session's runner
    # tunnel: the POST /events is published to that pod's in-process session
    # stream, so an off-replica POST persists the item (shows on reload) but the
    # live SSE tail never sees it ("no stream until refresh"). Unlike the other
    # native forwarders, this client carries no _RunnerDatabricksAuth (whose
    # auth_flow would stamp the key), so key the shared headers dict directly
    # from the runner-env host_id (databricks_request_headers with no explicit
    # host_id reads OMNIGENT_RUNNER_SLICE_KEY; emitted only on the workspace
    # mount). One point covers the client default + every helper POST below,
    # which all forward this same dict.
    from omnigent.cli_auth import databricks_request_headers

    headers = {**headers, **databricks_request_headers(base_url)}
    state = _read_state(bridge_dir)
    wire_path = Path(state.wire_path) if state is not None else None
    last_line = state.last_line if state is not None else 0
    offset = state.offset if state is not None else 0
    # Final assistant text of the turn in flight, forwarded on the terminal
    # edge so the parent's inbox gets the real result instead of an empty one.
    last_assistant_text = ""
    # True from an observed user prompt until a terminal edge posts; gates the
    # pane-death and quiescence fallbacks to turns that actually started.
    turn_open = state.turn_open if state is not None else False
    # Open tool calls in the current turn; a positive count means a silent wire
    # is a tool still running, not an orphaned turn.
    tools_in_flight = state.tools_in_flight if state is not None else 0
    # Id of the last terminal edge POSTed, persisted so a restart or wire
    # replay never double-fires the same edge at the server.
    last_edge_id = state.last_edge_id if state is not None else None
    # Terminal status of an edge the server permanently rejected (poison-drop):
    # the fallback close posts THIS status so a failed turn isn't closed idle.
    dropped_edge_status = state.dropped_edge_status if state is not None else None
    last_wire_activity = time.monotonic()
    last_wire_activity_wall = time.time()
    if state is not None and state.last_activity_ts is not None:
        # Resume the silence timers where the previous forwarder left them so
        # a crash-looping forwarder can't re-arm the full windows forever —
        # but floor the remaining window at a short grace so a wall-clock jump
        # (sleep/wake, NTP step) can't fire a watchdog on the spot.
        elapsed = max(0.0, time.time() - state.last_activity_ts)
        window = tool_quiescence_s if tools_in_flight > 0 else quiescence_s
        elapsed = min(elapsed, max(0.0, window - _RESTART_GRACE_S))
        last_wire_activity = time.monotonic() - elapsed
        last_wire_activity_wall = time.time() - elapsed
    # High-water of wire bytes actually observed: a redelivery retry (in this
    # run or a crash-loop replay) must not refresh the silence timers.
    last_seen_offset = (
        state.last_seen_offset
        if state is not None and state.last_seen_offset is not None
        else offset
    )
    # Consecutive permanent-4xx rejections of the item at the head of the tail.
    poison_id: str | None = None
    poison_attempts = 0
    post_backoff = post_backoff_initial_s
    # Start of the current unbroken edge-delivery failure streak, and the last
    # time it was alerted on (both monotonic).
    edge_failing_since: float | None = None
    last_edge_alert = 0.0
    async with httpx.AsyncClient(timeout=15.0, auth=auth) as client:

        def _persist() -> None:
            _write_state(
                bridge_dir,
                _ForwardState(
                    wire_path=str(wire_path),
                    last_line=last_line,
                    offset=offset,
                    turn_open=turn_open,
                    tools_in_flight=tools_in_flight,
                    last_edge_id=last_edge_id,
                    dropped_edge_status=dropped_edge_status,
                    last_activity_ts=last_wire_activity_wall,
                    last_seen_offset=last_seen_offset,
                ),
            )

        def _advance_past(item: KimiWireItem) -> None:
            """Move the delivery cursor past *item* and persist."""
            nonlocal offset, last_line
            offset = item.offset_after
            last_line = item.line_no + 1
            _persist()

        async def _backoff_after_post_failure() -> None:
            nonlocal post_backoff
            await asyncio.sleep(post_backoff)
            post_backoff = min(post_backoff * 2, _BACKOFF_MAX_S)

        async def _close_turn(status: str, edge: str) -> None:
            """Post the terminal edge for a turn that will write no more wire rows."""
            nonlocal turn_open, last_assistant_text, tools_in_flight, last_edge_id, post_backoff
            nonlocal dropped_edge_status, edge_failing_since, last_edge_alert
            # A wire edge the server permanently rejected already told us how
            # this turn ended; the fallback must not soften a failure to idle.
            status = dropped_edge_status or status
            # Line-anchored id: a restart retrying this fallback dedupes against
            # the already-posted edge, while a later turn gets a fresh id.
            edge_id = f"kimi:{edge}:{last_line}"
            if edge_id == last_edge_id:
                turn_open = False
                _persist()
                return
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
                # An undelivered edge is a delivery outage whatever the cause;
                # record it so the idle-turn watchdog can name it.
                record_native_post_failure("external_session_status", exc)
                _logger.warning("kimi forwarder: %s edge failed (will retry): %s", edge, exc)
                now = time.monotonic()
                if edge_failing_since is None:
                    edge_failing_since = now
                if (
                    now - edge_failing_since >= _EDGE_FAILURE_ALERT_S
                    and now - last_edge_alert >= _EDGE_FAILURE_ALERT_S
                ):
                    _logger.error(
                        "kimi forwarder: %s edge undelivered for %.0fs (still retrying): %s",
                        edge,
                        now - edge_failing_since,
                        exc,
                    )
                    last_edge_alert = now
                await _backoff_after_post_failure()
            else:
                note_native_post_success()
                post_backoff = post_backoff_initial_s
                turn_open = False
                tools_in_flight = 0
                last_assistant_text = ""
                last_edge_id = edge_id
                dropped_edge_status = None
                edge_failing_since = None
                _persist()

        while True:
            if wire_path is None or not wire_path.exists():
                discovered = await asyncio.to_thread(
                    _discover_wire, kimi_home, workspace, launch_epoch_ms
                )
                if discovered is not None and discovered != wire_path:
                    wire_path = discovered
                    last_line = 0
                    offset = 0
                    last_seen_offset = 0
                    # A new wire restarts line numbering, so a stale edge id
                    # could silently dedupe the new wire's edge at the same line.
                    last_edge_id = None
                    _persist()
            all_posted = True
            if wire_path is not None and wire_path.exists():
                items, new_offset, new_line = await asyncio.to_thread(
                    read_new_kimi_wire_items, wire_path, offset, last_line
                )
                if new_offset > last_seen_offset or new_offset < offset:
                    # Genuinely new rows (or a truncated/recreated wire) refresh
                    # the silence timers; a redelivery retry re-reading the same
                    # unposted tail must NOT defer the quiescence fallbacks.
                    # Persisted immediately (cursor untouched) so a crash loop
                    # can't refresh the timers with the same rows every restart.
                    last_seen_offset = new_offset
                    last_wire_activity = time.monotonic()
                    last_wire_activity_wall = time.time()
                    _persist()
                elif new_offset < last_seen_offset:
                    # The SAME wire path was recreated/compacted below the
                    # observed high-water (delivery cursor still under the new
                    # size, so the read didn't restart): the stale high-water
                    # would blind the refresh gate and quiescence could falsely
                    # close a live turn. Restart the tail like the truncation
                    # branch does and re-read next poll.
                    offset = 0
                    last_line = 0
                    last_seen_offset = 0
                    last_wire_activity = time.monotonic()
                    last_wire_activity_wall = time.time()
                    _persist()
                    await asyncio.sleep(poll_interval_s)
                    continue
                for item in items:
                    if item.kind in ("tool_call", "tool_result"):
                        tools_in_flight = (
                            tools_in_flight + 1
                            if item.kind == "tool_call"
                            else max(0, tools_in_flight - 1)
                        )
                        _advance_past(item)
                        continue
                    if item.kind == "message" and item.role == "user":
                        if dropped_edge_status is not None:
                            # Session status is single-valued: a new prompt
                            # legitimately supersedes the prior turn's pending
                            # status, but a swallowed FAILURE deserves a trace.
                            _logger.error(
                                "kimi forwarder: undeliverable %s edge superseded by a "
                                "new prompt; the parent never saw it",
                                dropped_edge_status,
                            )
                        turn_open = True
                        tools_in_flight = 0
                        dropped_edge_status = None
                    if item.kind in ("turn_end", "turn_failed") and (
                        item.response_id == last_edge_id
                    ):
                        # Edge already posted; a restart replayed the wire line
                        # before the cursor persisted. Just close out locally.
                        # (An ambiguous in-flight retry may still double-post,
                        # which is safe: the server treats a duplicate
                        # same-status terminal report as already delivered —
                        # no second parent wake, no status flip. Across a
                        # RUNNER restart the reconstructed work entry starts
                        # undelivered, so a replay could deliver again — but
                        # only the identical terminal status, and only when
                        # the crash landed between the POST and this persist;
                        # if the restart also lost the parent inbox, that
                        # re-delivery repairs it rather than duplicating.)
                        turn_open = False
                        _advance_past(item)
                        continue
                    try:
                        if item.kind in ("turn_end", "turn_failed"):
                            await _post_external_session_status(
                                client,
                                base_url=base_url,
                                headers=headers,
                                session_id=session_id,
                                status="idle" if item.kind == "turn_end" else "failed",
                                # A failed turn.ended carries the provider error
                                # message; surface it when no assistant text ran.
                                output=last_assistant_text or item.text,
                            )
                            last_assistant_text = ""
                            turn_open = False
                            tools_in_flight = 0
                            last_edge_id = item.response_id
                            dropped_edge_status = None
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
                        if status_code is None or status_code in _TRANSIENT_POST_STATUSES:
                            # Transport failures AND endlessly-retried transient
                            # rejections (e.g. a revoked token's 401s) are a
                            # delivery outage the idle-turn watchdog must see.
                            record_native_post_failure(event_type, exc)
                        else:
                            # A permanent status is a payload problem, resolved
                            # shortly by the poison drop — the server is fine.
                            note_native_post_success()
                        if (
                            status_code is not None
                            and 400 <= status_code < 500
                            and status_code not in _TRANSIENT_POST_STATUSES
                        ):
                            # Only a malformed payload poisons; auth/rate-limit
                            # rejections retry with backoff and are never dropped.
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
                                if item.kind in ("turn_end", "turn_failed"):
                                    # The turn's true terminal status is known;
                                    # hand it to the fallback close instead of
                                    # letting quiescence report idle.
                                    dropped_edge_status = (
                                        "idle" if item.kind == "turn_end" else "failed"
                                    )
                                    tools_in_flight = 0
                                poison_id, poison_attempts = None, 0
                                _advance_past(item)
                                continue
                        _logger.warning("kimi forwarder: POST failed (will retry): %s", exc)
                        all_posted = False
                        break
                    else:
                        note_native_post_success()
                        post_backoff = post_backoff_initial_s
                        if poison_id == item.response_id:
                            poison_id, poison_attempts = None, 0
                        _advance_past(item)
                if all_posted and new_offset != offset:
                    # Consume trailing non-item lines so the next poll skips them.
                    offset = new_offset
                    last_line = new_line
                    _persist()
            if turn_open and pane_alive is not None and not pane_alive():
                # The pane died mid-turn: no further wire rows are coming, so
                # post the failed edge instead of stranding the session.
                await _close_turn("failed", "pane-death")
            elif (
                turn_open
                and tools_in_flight == 0
                and time.monotonic() - last_wire_activity > quiescence_s
            ):
                # Turns that end without any wire edge (wedged kimi, lost edge
                # row): a long-quiet wire means the turn is over; close it as
                # idle. A silent wire mid-tool is a long tool call, not an
                # orphan — then only pane death (above) closes the turn.
                await _close_turn("idle", "quiescence")
            elif (
                turn_open
                and tools_in_flight > 0
                and time.monotonic() - last_wire_activity > tool_quiescence_s
            ):
                # Ceiling on the mid-tool suppression: no wire row of any kind
                # for this long means the tool (or kimi) hung in an alive pane —
                # fail the turn rather than suppress quiescence forever.
                _logger.error(
                    "kimi forwarder: %d tool call(s) in flight but wire silent > %.0fs; "
                    "failing the turn as hung",
                    tools_in_flight,
                    tool_quiescence_s,
                )
                await _close_turn("failed", "tool-quiescence")
            if not all_posted:
                await _backoff_after_post_failure()
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
