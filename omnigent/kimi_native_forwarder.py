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
``sessions/`` is symlinked to the user's global store, so several workspaces'
sessions share the tree; we disambiguate by ``workDir`` (via ``session_index.jsonl``)
and recency. Relevant wire events:

- ``turn.prompt`` (``origin.kind == "user"``) → a user message.
- ``context.append_loop_event`` wraps the streamed turn. Its ``event.type`` is
  one of: ``step.begin`` / ``step.end`` (step boundaries, carrying ``turnId`` and
  a terminal ``finishReason``); ``content.part`` where ``part.type == "text"`` is
  an assistant message and ``part.type == "think"`` is reasoning (mirrored as a
  transient ``external_output_reasoning_delta`` from ``part["think"]``);
  ``tool.call`` and ``tool.result`` (a built-in tool invocation and its output).
- ``turn.cancel`` → the turn was interrupted.

Each turn is mirrored so the web chat matches the TUI: user/assistant text and
tool calls are :class:`_MirrorItem`s POSTed as ``external_conversation_item`` /
``external_output_reasoning_delta``, and the turn is bracketed by ``running`` /
``idle`` ``external_session_status`` edges (posted via :func:`_post_status_edge`,
mirroring claude-native's item/status split — status is not a conversation item).
All of a turn's assistant-side items and its status edges share one ``response_id``
(``kimi:turn:<turnId>``) so the web renders in-flight tools as *live* cards
(spinner + ticking timer) rather than static ones.

kimi's wire has no ``turn.end`` event, and ``tool.result`` carries no ``turnId``,
so the forwarder is a *stateful* tailer: it remembers the active turn across
lines (:class:`_TurnState`) and treats a ``step.end`` whose ``finishReason`` is
not ``tool_use`` (or a ``turn.cancel``) as the turn's end. A per-session line
offset is persisted in ``<bridge_dir>/kimi_forwarder.json`` so restarts resume
without double-posting.

The ``idle`` edge is also the terminal-status report a parent orchestrator waits
on, so it carries the turn's final assistant text as ``output`` — the runner
delivers an empty result when an idle edge forwards none (#3166).

Row translation is shared with the offline importer: :func:`read_kimi_wire_items`
replays a whole wire log into :class:`KimiWireItem`s (see
:mod:`omnigent.session_import.local`), so live mirroring and import agree on
parsing and ids.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import NamedTuple

import httpx

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

#: Omnigent session-event types (must match the server ingestion route; shared
#: with the codex-/opencode-native forwarders).
_EXTERNAL_ITEM = "external_conversation_item"
_EXTERNAL_STATUS = "external_session_status"
_EXTERNAL_REASONING_DELTA = "external_output_reasoning_delta"
_STATUS_RUNNING = "running"
_STATUS_IDLE = "idle"

#: kimi ``step.end.finishReason`` meaning "paused this step to run a tool, more
#: steps follow" — the one non-terminal reason. Anything else (``end_turn`` …)
#: ends the turn.
_FINISH_TOOL_USE = "tool_use"


class _StatusEdge(NamedTuple):
    """A ``running`` / ``idle`` status edge to POST.

    ``response_id`` names the turn so the server records it as
    ``active_response_id`` — that is what keeps a mid-turn reconnect rendering
    the forwarded tool cards LIVE (the turn-start edge is not replayed on the SSE
    stream). Not modeled as a conversation item, mirroring claude-native.

    ``output`` carries the turn's final assistant text on the ``idle`` edge: for
    a sub-agent conversation the server maps that edge to a terminal completion
    that wakes the parent orchestrator's inbox, and the runner delivers an empty
    result when an idle edge forwards none (#3166).
    """

    status: str
    response_id: str | None = None
    output: str = ""


@dataclass
class _ForwardState:
    """Durable cursor for the wire-log tail."""

    wire_path: str
    last_line: int


@dataclass
class _TurnState:
    """In-memory turn tracking for the stateful tail.

    kimi emits no ``turn.end`` event and its ``tool.result`` carries no
    ``turnId``, so the active turn must be remembered across lines. ``turn_id``
    is kimi's ``turnId`` for the live turn (e.g. ``"3"``); ``running`` records
    whether a ``running`` status edge has already been posted for it, so it
    fires once per turn rather than once per step; ``last_text`` is the newest
    assistant text seen in the turn, forwarded as the ``idle`` edge's ``output``
    so a parent orchestrator's inbox gets the real result instead of an empty
    one (#3166).
    """

    turn_id: str | None = None
    running: bool = False
    last_text: str = ""


@dataclass
class KimiWireItem:
    """Stable parsed-wire contract shared by forwarding and offline import.

    One conversation item to POST, plus the line index it came from.

    ``kind`` selects the wire shape (all but ``reasoning`` post as
    ``external_conversation_item``):

    - ``message`` → user/assistant text (``role`` + ``text``)
    - ``reasoning`` → a think block, posted as ``external_output_reasoning_delta``
      from ``text``
    - ``function_call`` → a tool invocation (``call_id`` + ``name`` + ``arguments``)
    - ``function_call_output`` → its result (``call_id`` + ``output``)

    Fields not relevant to a given ``kind`` stay ``None``.
    """

    line_no: int
    kind: str = "message"
    response_id: str = ""
    role: str | None = None
    text: str | None = None
    call_id: str | None = None
    name: str | None = None
    arguments: str | None = None
    output: str | None = None


#: Historical in-module name, kept so the forwarder reads as before while
#: :class:`KimiWireItem` is the name the offline importer depends on (#3032).
_MirrorItem = KimiWireItem


class _WireLine(NamedTuple):
    """One tailed wire-log line: its 0-based index and the parsed JSON object."""

    line_no: int
    row: dict[str, object]


#: What ``_translate_row`` produces for one wire line: the conversation items to
#: POST, an optional ``running`` / ``idle`` status edge, and the next turn state.
#: A row yields items *or* a status, never both.
_RowResult = tuple[list[_MirrorItem], "_StatusEdge | None", _TurnState]


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
    if isinstance(wire_path, str) and isinstance(last_line, int):
        return _ForwardState(wire_path=wire_path, last_line=last_line)
    return None


def _write_state(bridge_dir: Path, state: _ForwardState) -> None:
    payload = {"wire_path": state.wire_path, "last_line": state.last_line}
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


def _event_turn_id(event: dict[str, object]) -> str | None:
    """Return the event's ``turnId`` as a string, or ``None`` when absent."""
    turn_id = event.get("turnId")
    if isinstance(turn_id, str) and turn_id:
        return turn_id
    if isinstance(turn_id, int):
        return str(turn_id)
    return None


def _turn_response_id(turn_id: str) -> str:
    """The shared per-turn response id (groups a turn's items + status edges)."""
    return f"kimi:turn:{turn_id}"


def _response_id(state: _TurnState, event: dict[str, object], line_no: int) -> str:
    """Per-turn response id, or a per-event fallback when no turn is known.

    Assistant text and tool events group under ``kimi:turn:<turnId>`` so they
    share the turn's ``running`` edge and render as one live response. A
    ``tool.result`` has no ``turnId`` and leans on the remembered turn; if even
    that is missing (e.g. a mid-turn restart resumed past ``step.begin``), fall
    back to a per-event id so the item still posts, just ungrouped.
    """
    if state.turn_id is not None:
        return _turn_response_id(state.turn_id)
    uuid = event.get("uuid")
    if isinstance(uuid, str) and uuid:
        return f"kimi:{uuid}"
    return f"kimi:line:{line_no}"


def _tool_output_text(result: object) -> str:
    """Extract the mirrored text from a ``tool.result`` ``result`` payload."""
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        output = result.get("output")
        if isinstance(output, str):
            return output
        return json.dumps(result, ensure_ascii=True)
    return ""


def _idle_edge(state: _TurnState) -> _StatusEdge:
    """The ``idle`` edge closing the active turn (id names the released turn).

    Carries the turn's final assistant text so a sub-agent's parent orchestrator
    is woken with the real result (#3166), not an empty one.
    """
    rid = _turn_response_id(state.turn_id) if state.turn_id is not None else None
    return _StatusEdge(_STATUS_IDLE, rid, state.last_text)


def _translate_row(line_no: int, row: dict[str, object], state: _TurnState) -> _RowResult:
    """Translate one wire row into ``(items, status_edge, next_state)``.

    Pure: any given row yields *either* conversation items *or* a status edge
    (never both). The caller commits the returned state only after the posts
    land, so a POST failure that retries the row cannot lose a ``running`` edge
    or double-advance the turn.
    """
    row_type = row.get("type")

    if row_type == "turn.prompt":
        origin = row.get("origin")
        if isinstance(origin, dict) and origin.get("kind") != "user":
            return [], None, state
        text = _input_text(row.get("input"))
        if not text:
            return [], None, state
        # The user bubble keeps its own per-line id: turn.prompt carries no
        # turnId, and the message need not join the turn's response group — it
        # only has to precede the assistant items, which wire order guarantees.
        item = _MirrorItem(line_no, "message", f"kimi:turn:{line_no}", role="user", text=text)
        return [item], None, state

    if row_type == "turn.cancel":
        if state.running:
            return [], _idle_edge(state), _TurnState()
        return [], None, state

    if row_type != "context.append_loop_event":
        return [], None, state

    event = row.get("event")
    if not isinstance(event, dict):
        return [], None, state
    etype = event.get("type")

    # Any event carrying turnId (re-)establishes the active turn — this also
    # recovers the turn after a mid-turn restart resumed past ``step.begin``.
    turn_id = _event_turn_id(event)
    if turn_id is not None:
        state = replace(state, turn_id=turn_id)

    if etype == "step.begin":
        if state.turn_id is not None and not state.running:
            edge = _StatusEdge(_STATUS_RUNNING, _turn_response_id(state.turn_id))
            return [], edge, replace(state, running=True)
        return [], None, state

    if etype == "step.end":
        if event.get("finishReason") == _FINISH_TOOL_USE:
            return [], None, state  # paused for a tool; the turn continues
        # Unconditional, even when no ``running`` edge was posted for this turn
        # (a restart can resume past ``step.begin``): the terminal edge is what
        # wakes a parent orchestrator, so skipping it strands the parent (#3166).
        return [], _idle_edge(state), _TurnState()

    if etype == "content.part":
        part = event.get("part")
        if not isinstance(part, dict):
            return [], None, state
        part_type = part.get("type")
        if part_type == "text":
            text = part.get("text")
            if not isinstance(text, str) or not text:
                return [], None, state
            rid = _response_id(state, event, line_no)
            item = _MirrorItem(line_no, "message", rid, role="assistant", text=text)
            # Remember it for the turn's ``idle`` edge output (#3166).
            return [item], None, replace(state, last_text=text)
        if part_type == "think":
            # Reasoning lives in ``part["think"]`` (not ``part["text"]``); mirror
            # it as a transient reasoning delta so the web UI paints a thinking
            # block — the kimi analogue of codex-native's #1254 reasoning fix.
            think = part.get("think")
            if not isinstance(think, str) or not think:
                return [], None, state
            rid = _response_id(state, event, line_no)
            item = _MirrorItem(line_no, "reasoning", rid, role="assistant", text=think)
            return [item], None, state
        return [], None, state

    if etype == "tool.call":
        call_id = event.get("toolCallId") or event.get("uuid")
        name = event.get("name")
        if not isinstance(call_id, str) or not isinstance(name, str):
            return [], None, state
        args = event.get("args")
        arguments = json.dumps(args if isinstance(args, dict) else {}, ensure_ascii=True)
        rid = _response_id(state, event, line_no)
        item = _MirrorItem(
            line_no, "function_call", rid, call_id=call_id, name=name, arguments=arguments
        )
        return [item], None, state

    if etype == "tool.result":
        call_id = event.get("toolCallId") or event.get("parentUuid")
        if not isinstance(call_id, str):
            return [], None, state
        output = _tool_output_text(event.get("result"))
        rid = _response_id(state, event, line_no)
        item = _MirrorItem(line_no, "function_call_output", rid, call_id=call_id, output=output)
        return [item], None, state

    return [], None, state


def _read_new_rows(wire_path: Path, last_line: int) -> list[_WireLine]:
    """Parse wire-log lines beyond *last_line* into :class:`_WireLine`s.

    The wire log is append-only JSONL, so a line count is a stable high-water
    mark. Non-JSON / non-object lines are dropped (they carry no line number
    forward on their own — the cursor advances as later rows are processed).
    """
    try:
        lines = wire_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows: list[_WireLine] = []
    for idx in range(last_line, len(lines)):
        line = lines[idx].strip()
        if not line or not line.startswith("{"):
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(_WireLine(idx, row))
    return rows


def read_kimi_wire_items(wire_path: Path, last_line: int) -> list[KimiWireItem]:
    """Parse wire-log lines beyond *last_line* into the stable shared contract.

    The offline importer (:mod:`omnigent.session_import.local`) replays a whole
    wire log through this and keeps the ``message`` items, so it goes through the
    same translation the live tail uses — one parser, one set of ids. Status
    edges are a forwarding-only concern and are dropped here.
    """
    items: list[KimiWireItem] = []
    turn = _TurnState()
    for line_no, row in _read_new_rows(wire_path, last_line):
        row_items, _status, turn = _translate_row(line_no, row, turn)
        items.extend(row_items)
    return items


def _conversation_item_data(item: _MirrorItem, agent_name: str) -> dict[str, object]:
    """Build the ``external_conversation_item`` ``data`` for one item."""
    if item.kind == "function_call":
        return {
            "item_type": "function_call",
            "item_data": {
                "agent": agent_name,
                "name": item.name,
                "arguments": item.arguments,
                "call_id": item.call_id,
            },
            "response_id": item.response_id,
        }
    if item.kind == "function_call_output":
        return {
            "item_type": "function_call_output",
            "item_data": {"call_id": item.call_id, "output": item.output},
            "response_id": item.response_id,
        }
    content_type = "input_text" if item.role == "user" else "output_text"
    item_data: dict[str, object] = {
        "role": item.role,
        "content": [{"type": content_type, "text": item.text}],
    }
    if item.role == "assistant":
        item_data["agent"] = agent_name
    return {"item_type": "message", "item_data": item_data, "response_id": item.response_id}


def _status_edge_data(edge: _StatusEdge) -> dict[str, object]:
    """Build the ``external_session_status`` ``data`` for one edge."""
    data: dict[str, object] = {"status": edge.status}
    if edge.response_id is not None:
        data["response_id"] = edge.response_id
    if edge.output:
        data["output"] = edge.output
    return data


async def _post_conversation_item(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    item: _MirrorItem,
    agent_name: str,
) -> None:
    """POST one mirrored message / function-call item."""
    body = {"type": _EXTERNAL_ITEM, "data": _conversation_item_data(item, agent_name)}
    url = f"{base_url.rstrip('/')}/v1/sessions/{session_id}/events"
    resp = await client.post(url, headers=headers, json=body)
    resp.raise_for_status()


async def _post_reasoning_item(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    item: _MirrorItem,
) -> None:
    """POST one mirrored think block as a transient reasoning event.

    A one-shot ``external_output_reasoning_delta`` with ``started: true`` opens a
    reasoning block in the web UI. Kimi persists completed think parts (not
    streamed deltas), so one delta per part is correct.
    """
    body = {"type": _EXTERNAL_REASONING_DELTA, "data": {"delta": item.text, "started": True}}
    url = f"{base_url.rstrip('/')}/v1/sessions/{session_id}/events"
    resp = await client.post(url, headers=headers, json=body)
    resp.raise_for_status()


async def _post_status_edge(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    edge: _StatusEdge,
) -> None:
    """POST one ``running`` / ``idle`` session-status edge."""
    body = {"type": _EXTERNAL_STATUS, "data": _status_edge_data(edge)}
    url = f"{base_url.rstrip('/')}/v1/sessions/{session_id}/events"
    resp = await client.post(url, headers=headers, json=body)
    resp.raise_for_status()


async def _deliver_row(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    items: list[_MirrorItem],
    status: _StatusEdge | None,
    agent_name: str,
) -> None:
    """POST one row's items (then its status edge, if any) to ``/events``."""
    for item in items:
        if item.kind == "reasoning":
            await _post_reasoning_item(
                client, base_url=base_url, headers=headers, session_id=session_id, item=item
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
    if status is not None:
        await _post_status_edge(
            client,
            base_url=base_url,
            headers=headers,
            session_id=session_id,
            edge=status,
        )


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
) -> None:
    """Poll the kimi session wire log and mirror new turns into the chat.

    Runs until cancelled. Discovers the wire log lazily (kimi writes it after the
    first turn), then tails it, translating each new row into conversation items
    and ``running`` / ``idle`` status edges and persisting the line offset after
    every processed row.
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
    turn = _TurnState()
    async with httpx.AsyncClient(timeout=15.0) as client:
        while True:
            if wire_path is None or not wire_path.exists():
                discovered = await asyncio.to_thread(
                    _discover_wire, kimi_home, workspace, launch_epoch_ms
                )
                if discovered is not None and discovered != wire_path:
                    wire_path = discovered
                    last_line = 0
                    turn = _TurnState()
                    _write_state(bridge_dir, _ForwardState(str(wire_path), last_line))
            if wire_path is not None and wire_path.exists():
                rows = await asyncio.to_thread(_read_new_rows, wire_path, last_line)
                for line_no, row in rows:
                    items, status, next_turn = _translate_row(line_no, row, turn)
                    try:
                        await _deliver_row(
                            client,
                            base_url=base_url,
                            headers=headers,
                            session_id=session_id,
                            items=items,
                            status=status,
                            agent_name=agent_name,
                        )
                    except httpx.HTTPError as exc:
                        _logger.warning("kimi forwarder: POST failed (will retry): %s", exc)
                        break
                    # Commit turn state only after the row's posts land, so a
                    # retried row re-translates against unchanged state.
                    turn = next_turn
                    last_line = line_no + 1
                    _write_state(bridge_dir, _ForwardState(str(wire_path), last_line))
            await asyncio.sleep(_POLL_INTERVAL_S)


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
) -> None:
    """Run :func:`forward_kimi_wire_to_session` with restart-on-crash backoff.

    Propagates :class:`asyncio.CancelledError` cleanly (terminal teardown), but
    restarts on any other exception with exponential backoff — mirrors
    ``cursor_native_forwarder.supervise_cursor_forwarder``.
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
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            _logger.exception("kimi forwarder crashed for session %s; restarting", session_id)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX_S)
        else:
            return
