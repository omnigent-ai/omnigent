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

- ``{"type": "turn.prompt", "input": [{"type":"text","text":…}], "origin": {"kind":"user"}}``
  → a user message.
- ``{"type": "context.append_loop_event", "event": {"type": "content.part",
  "part": {"type": "text", "text": …}, "uuid": …}}`` → an assistant message.
  (``part.type == "think"`` is reasoning, mirrored as a transient
  ``external_output_reasoning_delta`` from ``part["think"]``; ``tool.call`` /
  ``tool.result`` events are still skipped — the embedded terminal shows them.)

Each mirrored turn is POSTed as an ``external_conversation_item`` to
``/v1/sessions/{id}/events`` (the same shape :mod:`omnigent.kimi_native_hook`
uses for its read-only approval surface). A per-session line offset is persisted
in ``<bridge_dir>/kimi_forwarder.json`` so restarts resume without double-posting.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path

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


@dataclass
class _ForwardState:
    """Durable cursor for the wire-log tail."""

    wire_path: str
    last_line: int


@dataclass
class _MirrorItem:
    """One conversation item to POST, plus the line index it came from."""

    line_no: int
    role: str
    text: str
    response_id: str
    # "message" (a user/assistant turn → external_conversation_item) or
    # "reasoning" (a think block → external_output_reasoning_delta).
    kind: str = "message"


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


def _workdirs_for_sessions(kimi_home: Path) -> dict[str, str]:
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
    workdirs = _workdirs_for_sessions(kimi_home)
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


def _row_to_item(line_no: int, row: dict[str, object]) -> _MirrorItem | None:
    """Map one wire-log row to a conversation item, or ``None`` to skip it."""
    row_type = row.get("type")
    if row_type == "turn.prompt":
        origin = row.get("origin")
        if isinstance(origin, dict) and origin.get("kind") != "user":
            return None
        text = _input_text(row.get("input"))
        if not text:
            return None
        return _MirrorItem(
            line_no=line_no,
            role="user",
            text=text,
            response_id=f"kimi:turn:{line_no}",
        )
    if row_type == "context.append_loop_event":
        event = row.get("event")
        if not isinstance(event, dict) or event.get("type") != "content.part":
            return None
        part = event.get("part")
        if not isinstance(part, dict):
            return None
        uuid = event.get("uuid")
        response_id = f"kimi:{uuid}" if isinstance(uuid, str) and uuid else f"kimi:line:{line_no}"
        part_type = part.get("type")
        if part_type == "text":
            text = part.get("text")
            if not isinstance(text, str) or not text:
                return None
            return _MirrorItem(
                line_no=line_no, role="assistant", text=text, response_id=response_id
            )
        if part_type == "think":
            # Reasoning lives in ``part["think"]`` (not ``part["text"]``). Mirror it
            # as a transient reasoning event so the web UI paints a thinking block —
            # the kimi analogue of codex-native's #1254 reasoning fix.
            think = part.get("think")
            if not isinstance(think, str) or not think:
                return None
            return _MirrorItem(
                line_no=line_no,
                role="assistant",
                text=think,
                response_id=response_id,
                kind="reasoning",
            )
        return None
    return None


def _read_new_items(wire_path: Path, last_line: int) -> list[_MirrorItem]:
    """Parse wire-log lines beyond *last_line* into conversation items.

    The wire log is append-only JSONL, so a line count is a stable high-water
    mark. Non-JSON / unrecognized lines advance the cursor without emitting.
    """
    try:
        lines = wire_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    items: list[_MirrorItem] = []
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


async def _post_conversation_item(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    item: _MirrorItem,
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


async def _post_reasoning_item(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    item: _MirrorItem,
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
) -> None:
    """Poll the kimi session wire log and mirror new turns into the chat.

    Runs until cancelled. Discovers the wire log lazily (kimi writes it after the
    first turn), then tails it, POSTing each new user/assistant turn and
    persisting the line offset after every post.
    """
    state = _read_state(bridge_dir)
    wire_path = Path(state.wire_path) if state is not None else None
    last_line = state.last_line if state is not None else 0
    async with httpx.AsyncClient(timeout=15.0) as client:
        while True:
            if wire_path is None or not wire_path.exists():
                discovered = await asyncio.to_thread(
                    _discover_wire, kimi_home, workspace, launch_epoch_ms
                )
                if discovered is not None and discovered != wire_path:
                    wire_path = discovered
                    last_line = 0
                    _write_state(bridge_dir, _ForwardState(str(wire_path), last_line))
            if wire_path is not None and wire_path.exists():
                items = await asyncio.to_thread(_read_new_items, wire_path, last_line)
                for item in items:
                    try:
                        if item.kind == "reasoning":
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
                    except httpx.HTTPError as exc:
                        _logger.warning("kimi forwarder: POST failed (will retry): %s", exc)
                        break
                    last_line = item.line_no + 1
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
