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

- ``{"type":"user","message":{"role":"user","content":[{"type":"text","text":...}]}}``
- ``{"type":"assistant","message":{"role":"assistant","content":[{"type":"text"|
  "thinking"|"tool_use",...}]}}`` — only ``text`` blocks are mirrored.
- ``{"type":"control_request","request":{"subtype":"can_use_tool",...},
  "request_id":...}`` — recognized and logged; Omnigent-side policy gating
  (answer via ``confirmation_response``) is the documented follow-up. With the
  default in-terminal approval mode qwen does not emit these.

Status (``running``/``idle``) is intentionally NOT posted here: the runner's
PTY-activity watcher owns those edges for qwen-native (see
:mod:`omnigent.runner.app`), exactly as for goose-/cursor-native.
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

import httpx

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
        bridge_dir.mkdir(parents=True, exist_ok=True)
        tmp = bridge_dir / (_STATE_FILE + ".tmp")
        # Cap the dedup window so the state file can't grow unbounded.
        seen = (state.seen_uuids or [])[-512:]
        tmp.write_text(
            json.dumps({"offset": state.offset, "seen_uuids": seen}),
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
    response_id: str


def _text_from_content(content: object) -> str:
    """Join the ``text`` blocks of a stream-json message ``content`` array.

    ``thinking`` and ``tool_use`` blocks are skipped — only user-facing prose is
    mirrored into the chat bubble. Tolerant of a bare string or odd shapes so a
    schema tweak degrades to "best available text" rather than dropping the row.
    """
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "text":
            continue
        text = block.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts).strip()


def _event_to_item(event: dict[str, object], agent_name: str) -> _MirrorItem | None:
    """Convert one qwen stream-json event to a mirror item, or ``None`` to skip it."""
    etype = event.get("type")
    if etype not in ("user", "assistant"):
        if etype == "control_request":
            request = event.get("request")
            subtype = request.get("subtype") if isinstance(request, dict) else None
            if subtype == "can_use_tool":
                # Recognized but not yet gated through Omnigent's policy engine —
                # qwen's in-terminal approval is the gate for now. Wiring
                # confirmation_response back through TOOL_CALL policy is the
                # documented follow-up (see module docstring / design doc).
                _logger.debug("qwen forwarder saw can_use_tool control_request (in-terminal gate)")
        return None
    uuid = event.get("uuid")
    if not isinstance(uuid, str) or not uuid:
        return None
    message = event.get("message")
    if not isinstance(message, dict):
        return None
    text = _ATTACHMENT_MARKER_RE.sub("", _text_from_content(message.get("content"))).strip()
    if not text:
        return None  # tool-only / thinking-only turn with no prose
    response_id = f"qwen:{uuid}"
    if etype == "user":
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
    events_file: Path, offset: int, seen: set[str], agent_name: str
) -> tuple[list[_MirrorItem], int]:
    """Read NDJSON lines past *offset*, returning new mirror items + the new offset.

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
    items: list[_MirrorItem] = []
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
        item = _event_to_item(event, agent_name)
        if item is not None and item.uuid not in seen:
            items.append(item)
    return items, new_offset


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
    """Tail qwen's ``--json-file`` and mirror new messages into the AP session.

    Polls the event file past a persisted byte offset, posting each new
    user/assistant message as an ``external_conversation_item``. The offset +
    dedup set are persisted to ``bridge_dir`` so a supervisor restart resumes
    without re-posting.

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
    seen: set[str] = set(persisted.seen_uuids or [])
    timeout = httpx.Timeout(_POST_TIMEOUT_S)
    async with httpx.AsyncClient(
        base_url=base_url, headers=headers, auth=auth, timeout=timeout
    ) as client:
        while True:
            try:
                items, new_offset = await asyncio.to_thread(
                    _read_new_events, target, offset, seen, agent_name
                )
                for item in items:
                    await _post_conversation_item(client, session_id=session_id, item=item)
                    seen.add(item.uuid)
                if new_offset != offset or items:
                    offset = new_offset
                    _write_state(
                        bridge_dir,
                        _ForwardState(offset=offset, seen_uuids=list(seen)),
                    )
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
