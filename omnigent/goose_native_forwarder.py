"""TUI→web forwarder for the goose-native harness.

The ``omnigent goose`` wrapper launches the real ``goose session`` TUI in a
runner-owned tmux pane, and :mod:`omnigent.goose_native_bridge` injects web-UI
messages into it. That covers the web→TUI direction, but the *embedded terminal*
is then the only surface that reflects the agent's work — the Omnigent
conversation view (chat bubbles, title) stays empty because nothing mirrors the
TUI's transcript back into the session.

This module is that missing mirror — the goose analog of
:mod:`omnigent.cursor_native_forwarder`. Goose stores sessions in a SQLite
database at ``~/.local/share/goose/sessions/sessions.db`` (verified against Goose
1.38.0): a ``sessions`` row per session and a ``messages`` row per turn
(``id`` autoincrement, ``session_id`` FK, ``role``, ``content_json``). Because the
runner launches ``goose session --name <omnigent-session-id>``, discovery is a
direct ``sessions.name`` lookup — no content-addressed path hashing like cursor.
We poll ``messages`` past a high-water ``id`` and POST new user/assistant rows as
``external_conversation_item`` events (which also seeds the session title).

Status (``running``/``idle``) is intentionally NOT posted here: the runner's
PTY-activity watcher owns those edges for goose-native (see
:mod:`omnigent.runner.app`), exactly as for cursor-/claude-native.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from omnigent.cursor_native_bridge import FORK_HISTORY_CLOSE_TAG, FORK_HISTORY_OPEN_TAG

_logger = logging.getLogger(__name__)

#: Seconds between store polls. Goose flushes a ``messages`` row per agentic
#: *step* (each assistant-text / tool-call cycle) as a turn progresses — not just
#: once at turn end — so a snappier sub-second cadence makes the mirrored chat
#: track the terminal step-by-step on coding turns (many short tool-call steps)
#: rather than lagging a beat behind each one. 0.4s balances liveness vs. load.
_DEFAULT_POLL_INTERVAL_S = 0.4
_POST_TIMEOUT_S = 30.0

# Supervisor backoff (mirrors cursor_native_forwarder.supervise_cursor_forwarder).
_SUPERVISOR_INITIAL_BACKOFF_S = 1.0
_SUPERVISOR_MAX_BACKOFF_S = 30.0
_SUPERVISOR_HEALTHY_UPTIME_S = 60.0

_STATE_FILE = "goose_forwarder.json"

# Sqlite read errors are swallowed in the helpers below (a live DB is briefly
# unreadable mid-checkpoint, so returning empty and retrying is correct). But a
# *persistent* error (schema drift, wrong path) would otherwise leave the chat
# view silently empty forever — so surface each distinct error string once.
_warned_sqlite_errors: set[str] = set()


def _warn_sqlite_once(context: str, exc: sqlite3.Error) -> None:
    """Log a distinct sqlite error at warning level once (dedup by message)."""
    key = f"{context}:{exc}"
    if key in _warned_sqlite_errors:
        return
    _warned_sqlite_errors.add(key)
    _logger.warning("goose forwarder sqlite error during %s: %s", context, exc)


# The executor injects ``[Attached: <path>]`` markers for web-UI attachments
# before pasting into the TUI; strip them from the mirrored bubble (the path is
# an internal bridge detail).
_ATTACHMENT_MARKER_RE = re.compile(r"\[Attached:[^\]]*\]")

# On a fork into goose, the executor prepends the prior conversation to the first
# user message, fenced in <omnigent_fork_history>…</omnigent_fork_history>
# (cursor_native_bridge.wrap_fork_preamble — shared across native harnesses).
# goose stores it in the user message; strip the whole block so the mirrored
# bubble shows only the user's real text (the copied history already lives in the
# Omnigent timeline from the fork). Non-greedy → stops at the first (real) close
# tag; the trailing alternative degrades an unterminated block to end-of-text.
# Mirrors :data:`omnigent.cursor_native_forwarder._FORK_HISTORY_RE`.
_FORK_HISTORY_RE = re.compile(
    rf"{re.escape(FORK_HISTORY_OPEN_TAG)}.*?{re.escape(FORK_HISTORY_CLOSE_TAG)}"
    rf"|{re.escape(FORK_HISTORY_OPEN_TAG)}.*",
    re.DOTALL,
)


def default_sessions_db() -> Path:
    """Return Goose's SQLite session store path for this process's HOME.

    Overridable via ``GOOSE_SESSIONS_DB`` (tests, non-standard installs).
    """
    override = os.environ.get("GOOSE_SESSIONS_DB", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".local" / "share" / "goose" / "sessions" / "sessions.db"


@dataclass
class _ForwardState:
    """Durable forwarder cursor, persisted to ``bridge_dir/goose_forwarder.json``.

    :param goose_session_id: The resolved Goose ``sessions.id`` being tailed, or
        ``None`` before the session row exists.
    :param last_id: Highest ``messages.id`` already processed (forwarded or
        skipped). ``messages.id`` is autoincrement, so the high-water mark is
        sufficient dedup with O(1) state.
    """

    goose_session_id: str | None = None
    last_id: int = 0


def _read_state(bridge_dir: Path) -> _ForwardState:
    """Load the persisted forward cursor, or a cold default."""
    try:
        raw = (bridge_dir / _STATE_FILE).read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return _ForwardState()
    gsid = data.get("goose_session_id")
    last_id = data.get("last_id")
    return _ForwardState(
        goose_session_id=gsid if isinstance(gsid, str) else None,
        last_id=last_id if isinstance(last_id, int) else 0,
    )


def _write_state(bridge_dir: Path, state: _ForwardState) -> bool:
    """Atomically persist the forward cursor (tmp write + rename)."""
    try:
        bridge_dir.mkdir(parents=True, exist_ok=True)
        tmp = bridge_dir / (_STATE_FILE + ".tmp")
        tmp.write_text(
            json.dumps({"goose_session_id": state.goose_session_id, "last_id": state.last_id}),
            encoding="utf-8",
        )
        os.replace(tmp, bridge_dir / _STATE_FILE)
        return True
    except OSError:
        _logger.warning("goose forwarder could not persist state to %s", bridge_dir, exc_info=True)
        return False


def clear_goose_bridge_state(bridge_dir: Path) -> None:
    """Remove the persisted forward cursor so a re-created terminal starts clean."""
    with contextlib.suppress(OSError):
        (bridge_dir / _STATE_FILE).unlink()


def _connect_ro(db_path: Path) -> sqlite3.Connection | None:
    """Open *db_path* read-only in a way that reads the live WAL, or ``None``.

    ``mode=ro`` (not ``immutable=1``) so a live session's ``-wal`` sidecar is
    read via the ``-shm``; a plain connection is the fallback for the rare window
    where ``-shm`` is momentarily absent. Only SELECTs are issued.
    """
    for uri, kw in ((f"file:{db_path}?mode=ro", {"uri": True}), (str(db_path), {})):
        try:
            return sqlite3.connect(uri, timeout=5.0, **kw)
        except sqlite3.Error:
            continue
    return None


def _resolve_goose_session_id(db_path: Path, session_name: str) -> str | None:
    """Return the Goose ``sessions.id`` whose ``name`` matches *session_name*.

    The runner launches ``goose session --name <omnigent-session-id>``; the row
    appears once Goose initializes the session. Newest match wins if a name was
    somehow reused.
    """
    con = _connect_ro(db_path)
    if con is None:
        return None
    try:
        row = con.execute(
            "SELECT id FROM sessions WHERE name = ? ORDER BY created_at DESC LIMIT 1",
            (session_name,),
        ).fetchone()
    except sqlite3.Error as exc:
        _warn_sqlite_once("session resolution", exc)
        return None
    finally:
        con.close()
    return row[0] if row and isinstance(row[0], str) else None


@dataclass(frozen=True)
class PendingToolCall:
    """A goose tool call awaiting in-terminal approval, read from the store.

    Goose persists the assistant message (carrying its ``toolRequest`` parts)
    *before* it blocks on the cliclack confirmation (verified against goose's
    reply loop: the message is added to the store, then the ``ToolConfirmation``
    is emitted), so the structured call is readable at prompt time — no scraping
    the tool name/args off the pane.

    :param request_id: The goose tool-request id (correlates to a later
        ``toolResponse`` once executed or denied).
    :param name: The bare tool name (e.g. ``shell``), as goose stores it.
    :param arguments: The tool arguments object.
    :param extension: The owning goose extension (``_meta.goose_extension``),
        used to recognise Omnigent-MCP tools that the relay already gates.
    """

    request_id: str
    name: str
    arguments: dict[str, object]
    extension: str | None


def _tool_request_from_part(part: object) -> PendingToolCall | None:
    """Parse one ``{"type":"toolRequest",...}`` content part, or ``None``.

    Shape (verified against a live ``sessions.db``)::

        {"type":"toolRequest","id":"toolu_…",
         "toolCall":{"status":"success","value":{"name":"shell",
                     "arguments":{"command":"…"}}},
         "_meta":{"goose_extension":"developer"}}
    """
    if not isinstance(part, dict) or part.get("type") != "toolRequest":
        return None
    request_id = part.get("id")
    if not isinstance(request_id, str) or not request_id:
        return None
    call = part.get("toolCall")
    # ``toolCall`` is a serialized ``Result``; only a success carries name/args.
    if not isinstance(call, dict) or call.get("status") != "success":
        return None
    value = call.get("value")
    if not isinstance(value, dict):
        return None
    name = value.get("name")
    if not isinstance(name, str) or not name:
        return None
    arguments = value.get("arguments")
    if not isinstance(arguments, dict):
        arguments = {}
    extension = None
    meta = part.get("_meta")
    if isinstance(meta, dict):
        ext = meta.get("goose_extension")
        if isinstance(ext, str) and ext:
            extension = ext
    return PendingToolCall(
        request_id=request_id, name=name, arguments=arguments, extension=extension
    )


@dataclass
class _MirrorItem:
    """One conversation item ready to POST, plus the message id that produced it.

    :param reasoning_text: Thinking (chain-of-thought) text from the same row, if
        any. Posted as a transient ``external_output_reasoning_delta`` BEFORE the
        conversation item, so the web UI paints a "thinking" block that the
        following assistant message finalizes. ``item_type`` may be empty (a
        reasoning-only row) while ``reasoning_text`` is set.
    """

    msg_id: int
    item_type: str
    item_data: dict[str, object]
    response_id: str
    reasoning_text: str | None = None


def _content_reasoning(content_json: str) -> str:
    """Extract concatenated thinking text from a Goose ``content_json`` value.

    Goose persists model reasoning as ``{"type":"thinking","thinking":"…"}`` parts
    (verified against a live ``sessions.db``); redacted thinking
    (``{"type":"redactedThinking"}``) carries no readable text and is skipped.
    """
    try:
        obj = json.loads(content_json)
    except ValueError:
        return ""
    parts = obj if isinstance(obj, list) else [obj]
    chunks: list[str] = []
    for part in parts:
        if isinstance(part, dict) and part.get("type") == "thinking":
            thinking = part.get("thinking")
            if isinstance(thinking, str) and thinking:
                chunks.append(thinking)
    return "".join(chunks).strip()


def _content_text(content_json: str) -> str:
    """Extract human-readable text from a Goose ``messages.content_json`` value.

    Goose serializes message content as JSON; the exact shape can vary by version
    and message kind (a bare string, a list of typed parts, or a dict wrapping
    either). This decoder is deliberately tolerant — it pulls ``text`` from any
    part shaped like ``{"type": "text", "text": ...}`` (or a bare ``{"text": ...}``)
    and falls back to a top-level string — so a schema tweak degrades to "best
    available text" rather than dropping the message. See plan R1: pin the exact
    part shape against a live row and tighten if needed.
    """
    try:
        obj = json.loads(content_json)
    except ValueError:
        return content_json.strip()

    def _from_part(part: object) -> str:
        if isinstance(part, str):
            return part
        if isinstance(part, dict):
            text = part.get("text")
            if isinstance(text, str):
                return text
            # Some shapes nest the text under "content".
            nested = part.get("content")
            if isinstance(nested, str):
                return nested
        return ""

    if isinstance(obj, str):
        return obj.strip()
    if isinstance(obj, list):
        return "".join(_from_part(p) for p in obj).strip()
    if isinstance(obj, dict):
        # {"text": ...} | {"content": <str|list>} | a single part dict
        direct = _from_part(obj)
        if direct:
            return direct.strip()
        inner = obj.get("content")
        if isinstance(inner, str):
            return inner.strip()
        if isinstance(inner, list):
            return "".join(_from_part(p) for p in inner).strip()
    return ""


def _message_to_item(
    msg_id: int, role: object, content_json: object, agent_name: str
) -> _MirrorItem | None:
    """Convert one ``messages`` row to a mirror item, or ``None`` to skip it."""
    if not isinstance(role, str) or not isinstance(content_json, str):
        return None
    text = _ATTACHMENT_MARKER_RE.sub("", _content_text(content_json)).strip()
    response_id = f"goose:{msg_id}"
    if role == "user":
        # A fork's first user message carries the prior conversation as a fenced
        # preamble (see the executor); strip it so the bubble shows only the
        # user's real text — the copied history is already in the timeline.
        text = _FORK_HISTORY_RE.sub("", text).strip()
        if not text:
            return None
        return _MirrorItem(
            msg_id=msg_id,
            item_type="message",
            item_data={"role": "user", "content": [{"type": "input_text", "text": text}]},
            response_id=response_id,
        )
    if role == "assistant":
        reasoning = _content_reasoning(content_json) or None
        if not text:
            # A reasoning-only step (thinking, then tools) still surfaces its
            # thinking; the conversation item is empty (item_type="") so only the
            # reasoning delta posts, and the cursor advances.
            if reasoning:
                return _MirrorItem(
                    msg_id=msg_id,
                    item_type="",
                    item_data={},
                    response_id="",
                    reasoning_text=reasoning,
                )
            return None  # tool-only turn with no prose or thinking
        return _MirrorItem(
            msg_id=msg_id,
            item_type="message",
            item_data={
                "role": "assistant",
                "agent": agent_name,
                "content": [{"type": "output_text", "text": text}],
            },
            response_id=response_id,
            reasoning_text=reasoning,
        )
    return None  # tool / system / other scaffolding


def _read_new_items(
    db_path: Path, goose_session_id: str, last_id: int, agent_name: str
) -> list[_MirrorItem]:
    """Read ``messages`` rows with ``id > last_id`` for this session as items.

    A skipped row (tool/system/empty) still advances the cursor via a sentinel
    item so it is never reconsidered.
    """
    con = _connect_ro(db_path)
    if con is None:
        return []
    try:
        rows = con.execute(
            "SELECT id, role, content_json FROM messages "
            "WHERE session_id = ? AND id > ? ORDER BY id",
            (goose_session_id, last_id),
        ).fetchall()
    except sqlite3.Error as exc:
        _warn_sqlite_once("message read", exc)
        return []
    finally:
        con.close()
    items: list[_MirrorItem] = []
    for msg_id, role, content_json in rows:
        item = _message_to_item(msg_id, role, content_json, agent_name)
        if item is not None:
            items.append(item)
        else:
            items.append(_MirrorItem(msg_id=msg_id, item_type="", item_data={}, response_id=""))
    return items


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


async def _post_reasoning_delta(client: httpx.AsyncClient, *, session_id: str, text: str) -> None:
    """POST a row's thinking as one transient ``external_output_reasoning_delta``.

    Goose persists the complete thinking block (not token deltas), so the whole
    text goes as a single ``started=True`` delta; the assistant message posted
    next finalizes the painted block. Reasoning is never a persisted conversation
    item (the same contract as every other harness), so this is fire-and-forget.
    """
    resp = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "external_output_reasoning_delta",
            "data": {"delta": text, "started": True},
        },
    )
    resp.raise_for_status()


async def forward_goose_store_to_session(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    bridge_dir: Path,
    agent_name: str,
    goose_session_name: str,
    db_path: Path | None = None,
    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
    auth: httpx.Auth | None = None,
) -> None:
    """Tail Goose's session store and mirror new messages into the AP session.

    Resolves this session's Goose ``sessions.id`` by ``name`` (the
    ``--name <omnigent-session-id>`` the runner launched with), then polls its
    ``messages`` rows, posting each new user/assistant row as an
    ``external_conversation_item``. The high-water ``id`` is persisted to
    ``bridge_dir`` so a supervisor restart resumes without re-posting.

    :param base_url: Omnigent server base URL.
    :param headers: Static HTTP headers (auth normally via ``auth``).
    :param session_id: Omnigent session/conversation id.
    :param bridge_dir: The goose-native bridge dir (holds the persisted cursor).
    :param agent_name: Agent label stamped on mirrored assistant items.
    :param goose_session_name: The ``--name`` passed to ``goose session``.
    :param db_path: Goose sessions DB; defaults to :func:`default_sessions_db`.
    :param poll_interval_s: Seconds between store polls.
    :param auth: Optional refresh-capable httpx Auth for remote deployments.
    :returns: Never normally returns; cancel the task to stop it.
    """
    db = db_path or default_sessions_db()
    persisted = _read_state(bridge_dir)
    goose_session_id: str | None = persisted.goose_session_id
    last_id = persisted.last_id if goose_session_id is not None else 0
    timeout = httpx.Timeout(_POST_TIMEOUT_S)
    async with httpx.AsyncClient(
        base_url=base_url, headers=headers, auth=auth, timeout=timeout
    ) as client:
        while True:
            try:
                if goose_session_id is None:
                    resolved = await asyncio.to_thread(
                        _resolve_goose_session_id, db, goose_session_name
                    )
                    if resolved is not None:
                        goose_session_id = resolved
                        last_id = 0
                        _write_state(
                            bridge_dir,
                            _ForwardState(goose_session_id=resolved, last_id=0),
                        )
                if goose_session_id is not None:
                    items = await asyncio.to_thread(
                        _read_new_items, db, goose_session_id, last_id, agent_name
                    )
                    for item in items:
                        # Paint thinking first so the message that follows finalizes
                        # the reasoning block.
                        if item.reasoning_text:
                            await _post_reasoning_delta(
                                client, session_id=session_id, text=item.reasoning_text
                            )
                        if item.item_type:
                            await _post_conversation_item(client, session_id=session_id, item=item)
                        last_id = item.msg_id
                        _write_state(
                            bridge_dir,
                            _ForwardState(goose_session_id=goose_session_id, last_id=last_id),
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception(
                    "goose forwarder poll failed; session=%s goose_session=%s",
                    session_id,
                    goose_session_id,
                )
            await asyncio.sleep(poll_interval_s)


def _supervisor_monotonic() -> float:
    """Indirection so tests can stub the supervisor's clock."""
    return time.monotonic()


async def _supervisor_sleep(seconds: float) -> None:
    """Indirection so tests can stub the supervisor's backoff sleep."""
    await asyncio.sleep(seconds)


async def supervise_goose_forwarder(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    bridge_dir: Path,
    agent_name: str,
    goose_session_name: str,
    db_path: Path | None = None,
    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
    auth: httpx.Auth | None = None,
) -> None:
    """Run :func:`forward_goose_store_to_session` under a restart supervisor.

    Mirrors :func:`omnigent.cursor_native_forwarder.supervise_cursor_forwarder`:
    bounded exponential backoff, :class:`asyncio.CancelledError` propagates for
    clean teardown, and the persisted ``id`` cursor means restarts resume exactly
    where they left off.

    :returns: Never normally returns; cancel the task to stop it.
    """
    backoff_s = _SUPERVISOR_INITIAL_BACKOFF_S
    while True:
        run_started_at = _supervisor_monotonic()
        crash_exc: Exception | None = None
        try:
            await forward_goose_store_to_session(
                base_url=base_url,
                headers=headers,
                session_id=session_id,
                bridge_dir=bridge_dir,
                agent_name=agent_name,
                goose_session_name=goose_session_name,
                db_path=db_path,
                poll_interval_s=poll_interval_s,
                auth=auth,
            )
            _logger.warning(
                "goose forwarder returned unexpectedly; restarting; session=%s bridge_dir=%s",
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
                "goose forwarder crashed; restarting in %.1fs; session=%s bridge_dir=%s",
                backoff_s,
                session_id,
                bridge_dir,
                exc_info=crash_exc,
            )
        await _supervisor_sleep(backoff_s)
        backoff_s = min(backoff_s * 2.0, _SUPERVISOR_MAX_BACKOFF_S)
