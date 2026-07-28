"""TUI→web forwarder for the copilot-native harness.

The ``omnigent copilot`` wrapper launches the real ``copilot`` TUI in a
runner-owned tmux pane, and :mod:`omnigent.copilot_native_bridge` injects web-UI
messages into it. That covers the web→TUI direction, but the *embedded terminal*
is then the only surface reflecting the agent's work — the Omnigent conversation
view shows the user's bubble and an assistant turn that never fills in, because
nothing mirrors the transcript back into the session.

This module is that missing mirror, and it is the qwen analog: like
:mod:`omnigent.qwen_native_forwarder` it tails an **append-only NDJSON event
stream** by byte offset, deduplicates on each event's uuid, and persists the
cursor to the bridge dir so a supervisor restart resumes without re-posting.
Copilot needs no transcript scraping (as goose and cursor do for their SQLite
stores): the CLI records one JSON event per line to
``<copilot-home>/session-state/<uuid>/events.jsonl``, and ``--session-id`` pins
that uuid at launch, so discovery is exact rather than recency-based.

Event shapes consumed (others are ignored defensively; verified against Copilot
CLI 1.0.63):

- ``{"type":"user.message","id":…,"data":{"content":…}}`` → a user bubble.
  ``data.transformedContent`` is deliberately NOT used: it carries the CLI's
  injected ``<current_datetime>`` and ``<system_reminder>`` scaffolding.
- ``{"type":"assistant.message","id":…,"data":{"content":…,"turnId":…}}`` → an
  assistant bubble. Tool-only steps carry empty ``content`` and are skipped —
  the embedded terminal already shows them.
- ``{"type":"assistant.turn_end","data":{"turnId":…}}`` → the turn-end edge,
  posted as ``external_session_status: idle``. This is the contract
  claude-/cursor-/goose-native use to mark a sub-agent turn terminal and wake
  its parent's inbox; Copilot is unusual in emitting an explicit, reliable
  turn-end marker rather than needing an idle heuristic.

The web-facing ``running``/``idle`` *spinner* edges are not posted here: the
runner's PTY-activity watcher owns those ``session.status`` edges for the
tmux-hosted native harnesses.
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
from omnigent.copilot_native_bridge import session_events_path
from omnigent.inner.native_attachments import ATTACHMENT_MARKER_STRIP_PATTERN

_logger = logging.getLogger(__name__)

#: Seconds between event-file polls. Copilot flushes events per step, so a
#: sub-second cadence keeps the mirrored chat tracking the terminal step by step.
_DEFAULT_POLL_INTERVAL_S = 0.4
_POST_TIMEOUT_S = 30.0

# Supervisor backoff (mirrors qwen_native_forwarder.supervise_qwen_forwarder).
_SUPERVISOR_INITIAL_BACKOFF_S = 1.0
_SUPERVISOR_MAX_BACKOFF_S = 30.0
_SUPERVISOR_HEALTHY_UPTIME_S = 60.0

_STATE_FILE = "copilot_forwarder.json"

#: Max length of a mirrored item's ``response_id``; the server column is a
#: ``VARCHAR(64)``. ``copilot:`` + a 36-char uuid fits comfortably, but the cap
#: keeps a future id-shape change from wedging the loop on a rejected POST.
_RESPONSE_ID_MAX_LEN = 64

#: Recently posted event uuids kept so a truncation/relaunch re-read (offset
#: rewinds to 0) doesn't re-post them. Insertion-ordered so the cap keeps the
#: most recent ids.
_DEDUP_WINDOW = 512

# The executor injects ``[Attached: <path>]`` (or the could-not-load marker from
# native_attachments) for web-UI attachments before submitting; strip them from
# the mirrored bubble (internal bridge details).
_ATTACHMENT_MARKER_RE = re.compile(ATTACHMENT_MARKER_STRIP_PATTERN)


def _new_seen(uuids: Iterable[str] | None = None) -> dict[str, None]:
    """Build the insertion-ordered dedup set (``dict`` used as an ordered set)."""
    return dict.fromkeys(uuids or [])


@dataclass
class _ForwardState:
    """Durable forwarder cursor, persisted to ``bridge_dir/copilot_forwarder.json``.

    :param offset: Byte offset into ``events.jsonl`` already consumed. The file
        is append-only within a session; a relaunch pins a fresh Copilot session
        uuid and therefore a fresh file, but a truncation is still detected as
        ``size < offset`` and rewinds to 0.
    :param seen_uuids: Recently posted event ids, for idempotent dedup across a
        truncation/restart. Bounded to the most recent entries.
    """

    offset: int = 0
    seen_uuids: list[str] | None = None


def _read_state(bridge_dir: Path) -> _ForwardState:
    """Load the persisted forward cursor, or a cold default."""
    try:
        raw = (bridge_dir / _STATE_FILE).read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return _ForwardState(offset=0, seen_uuids=[])
    offset = data.get("offset")
    seen = data.get("seen_uuids")
    return _ForwardState(
        offset=offset if isinstance(offset, int) and offset >= 0 else 0,
        seen_uuids=[u for u in seen if isinstance(u, str)] if isinstance(seen, list) else [],
    )


def _write_state(bridge_dir: Path, state: _ForwardState) -> bool:
    """Atomically persist the forward cursor (tmp write + rename)."""
    try:
        bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = bridge_dir / (_STATE_FILE + ".tmp")
        seen = (state.seen_uuids or [])[-_DEDUP_WINDOW:]
        tmp.write_text(
            json.dumps({"offset": state.offset, "seen_uuids": seen}),
            encoding="utf-8",
        )
        os.replace(tmp, bridge_dir / _STATE_FILE)
        return True
    except OSError:
        _logger.warning(
            "copilot forwarder could not persist state to %s", bridge_dir, exc_info=True
        )
        return False


def clear_copilot_bridge_state(bridge_dir: Path) -> None:
    """Remove the persisted forward cursor so a re-created terminal starts clean."""
    with contextlib.suppress(OSError):
        (bridge_dir / _STATE_FILE).unlink()


@dataclass
class _MirrorItem:
    """One conversation item ready to POST, plus the event uuid that produced it."""

    uuid: str
    item_type: str
    item_data: dict[str, object]
    response_id: str


@dataclass
class _TurnEnd:
    """A completed assistant turn, carrying the response id to close out."""

    uuid: str
    response_id: str | None


def _event_text(data: object) -> str:
    """Return the prose of a ``*.message`` event's ``data``, or ``""``."""
    if not isinstance(data, dict):
        return ""
    content = data.get("content")
    if isinstance(content, str):
        return content.strip()
    # Tolerate a future block-list shape rather than dropping the message.
    if isinstance(content, list):
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        ]
        return "".join(parts).strip()
    return ""


def _response_id_for(event_id: str) -> str:
    """Build the server-side grouping id for a mirrored event."""
    return f"copilot:{event_id}"[:_RESPONSE_ID_MAX_LEN]


def _event_to_mirror(event: dict[str, object], agent_name: str) -> _MirrorItem | _TurnEnd | None:
    """Convert one Copilot event to a mirror item or turn-end, or ``None`` to skip."""
    etype = event.get("type")
    uuid = event.get("id")
    if not isinstance(uuid, str) or not uuid:
        return None
    data = event.get("data")
    if etype == "assistant.turn_end":
        return _TurnEnd(uuid=uuid, response_id=None)
    if etype not in ("user.message", "assistant.message"):
        # session.start / system.message / assistant.turn_start / shutdown carry
        # no transcript prose to mirror.
        return None
    text = _ATTACHMENT_MARKER_RE.sub("", _event_text(data)).strip()
    if not text:
        return None  # tool-only step with no prose
    response_id = _response_id_for(uuid)
    if etype == "user.message":
        return _MirrorItem(
            uuid=uuid,
            item_type="message",
            item_data={"role": "user", "content": [{"type": "input_text", "text": text}]},
            response_id=response_id,
        )
    return _MirrorItem(
        uuid=uuid,
        item_type="message",
        item_data={
            "role": "assistant",
            "agent": agent_name,
            "content": [{"type": "output_text", "text": text}],
        },
        response_id=response_id,
    )


def _read_new_events(
    events_file: Path, offset: int, seen: Container[str], agent_name: str
) -> tuple[list[_MirrorItem | _TurnEnd], int]:
    """Read NDJSON lines past *offset*, returning new mirrors + the new offset.

    Detects a truncated/recreated event file (``size < offset``) and rewinds to
    0. Only fully terminated lines (ending in ``\\n``) are consumed; a trailing
    partial line is left for the next poll by not advancing past it.
    """
    try:
        size = events_file.stat().st_size
    except OSError:
        return [], offset  # the CLI has not created the session dir yet
    if size < offset:
        offset = 0
    if size == offset:
        return [], offset
    try:
        with open(events_file, "rb") as fh:
            fh.seek(offset)
            data = fh.read(size - offset)
    except OSError:
        return [], offset
    last_nl = data.rfind(b"\n")
    if last_nl == -1:
        return [], offset  # no complete line yet
    consumed = data[: last_nl + 1]
    new_offset = offset + len(consumed)
    mirrors: list[_MirrorItem | _TurnEnd] = []
    for raw in consumed.split(b"\n"):
        raw = raw.strip()
        if not raw:
            continue
        try:
            event = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue  # tolerate a malformed line rather than stalling the tail
        if not isinstance(event, dict):
            continue
        mirror = _event_to_mirror(event, agent_name)
        if mirror is not None and mirror.uuid not in seen:
            mirrors.append(mirror)
    return mirrors, new_offset


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


async def forward_copilot_events_to_session(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    bridge_dir: Path,
    agent_name: str,
    copilot_session_id: str,
    events_file: Path | None = None,
    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
    auth: httpx.Auth | None = None,
) -> None:
    """Tail Copilot's ``events.jsonl`` and mirror new messages into the session.

    Polls the event file past a persisted byte offset, posting each new
    user/assistant message as an ``external_conversation_item`` and each
    completed assistant turn as an ``external_session_status: idle``. The offset
    and dedup set are persisted to *bridge_dir* so a supervisor restart resumes
    without re-posting.

    :param base_url: Omnigent server base URL.
    :param headers: Static HTTP headers (auth normally via ``auth``).
    :param session_id: Omnigent session/conversation id.
    :param bridge_dir: Copilot-native bridge dir (holds the persisted cursor).
    :param agent_name: Agent label stamped on mirrored assistant items.
    :param copilot_session_id: The uuid pinned via ``--session-id`` at launch.
    :param events_file: Override for the tailed file; defaults to the path
        derived from *copilot_session_id*.
    :param poll_interval_s: Seconds between event-file polls.
    :param auth: Optional refresh-capable httpx Auth for remote deployments.
    :returns: Never normally returns; cancel the task to stop it.
    """
    target = events_file or session_events_path(copilot_session_id)
    persisted = _read_state(bridge_dir)
    offset = persisted.offset
    seen = _new_seen(persisted.seen_uuids)
    # The response id of the assistant message most recently mirrored, so the
    # turn-end edge can close out the bubble it belongs to.
    open_response_id: str | None = None
    timeout = httpx.Timeout(_POST_TIMEOUT_S)
    async with httpx.AsyncClient(
        base_url=base_url, headers=headers, auth=auth, timeout=timeout
    ) as client:
        while True:
            try:
                mirrors, new_offset = await asyncio.to_thread(
                    _read_new_events, target, offset, seen, agent_name
                )
                for mirror in mirrors:
                    if isinstance(mirror, _TurnEnd):
                        await post_external_session_status(
                            client,
                            session_id=session_id,
                            status="idle",
                            response_id=open_response_id,
                        )
                        open_response_id = None
                    else:
                        await _post_conversation_item(client, session_id=session_id, item=mirror)
                        if mirror.item_data.get("role") == "assistant":
                            open_response_id = mirror.response_id
                    seen[mirror.uuid] = None
                if new_offset != offset or mirrors:
                    offset = new_offset
                    _write_state(
                        bridge_dir,
                        _ForwardState(offset=offset, seen_uuids=list(seen)),
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception(
                    "copilot forwarder poll failed; session=%s bridge_dir=%s",
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


async def supervise_copilot_forwarder(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    bridge_dir: Path,
    agent_name: str,
    copilot_session_id: str,
    events_file: Path | None = None,
    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
    auth: httpx.Auth | None = None,
) -> None:
    """Run :func:`forward_copilot_events_to_session` under a restart supervisor.

    Mirrors :func:`omnigent.qwen_native_forwarder.supervise_qwen_forwarder`:
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
            await forward_copilot_events_to_session(
                base_url=base_url,
                headers=headers,
                session_id=session_id,
                bridge_dir=bridge_dir,
                agent_name=agent_name,
                copilot_session_id=copilot_session_id,
                events_file=events_file,
                poll_interval_s=poll_interval_s,
                auth=auth,
            )
            _logger.warning(
                "copilot forwarder returned unexpectedly; restarting; session=%s bridge_dir=%s",
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
                "copilot forwarder crashed; restarting in %.1fs; session=%s bridge_dir=%s",
                backoff_s,
                session_id,
                bridge_dir,
                exc_info=crash_exc,
            )
        await _supervisor_sleep(backoff_s)
        backoff_s = min(backoff_s * 2.0, _SUPERVISOR_MAX_BACKOFF_S)
