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

**Live tool-call cards**: when the forwarder sees ``toolreq`` parts in an
assistant message it emits ``function_call`` / ``function_call_output`` items
stamped with a per-turn ``response_id`` (``goose:turn:{first_assistant_msg_id}``).
A ``running`` status edge carrying the same id is POSTed on the first assistant
item of each turn. The closing ``idle`` edge is posted when the turn's final
prose message lands (Goose's agent loop ends on an assistant reply with no tool
calls), when the next user message arrives, or — as a backstop for turns that
died without either (TUI interrupt, Goose crash) — after
:data:`_STALLED_TURN_IDLE_S` of store inactivity. On restart the open turn is
replayed from the store (:func:`_replay_open_turn`) so resumed rows keep the
same turn id and a card left running by a crash still gets closed.
The PTY-activity watcher continues to drive the generic session-level
running/idle badge (id-less); the id-bearing edges here drive only the
streaming lifecycle of the individual tool-call bubbles.
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
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from omnigent._native_post_delivery import post_external_session_status

_logger = logging.getLogger(__name__)

#: Seconds between store polls. Goose flushes a ``messages`` row per agentic
#: *step* (each assistant-text / tool-call cycle) as a turn progresses — not just
#: once at turn end — so a snappier sub-second cadence makes the mirrored chat
#: track the terminal step-by-step on coding turns (many short tool-call steps)
#: rather than lagging a beat behind each one. 0.4s balances liveness vs. load.
_DEFAULT_POLL_INTERVAL_S = 0.4
_POST_TIMEOUT_S = 30.0

#: Seconds of store inactivity after which an open turn's live card is closed.
#: This is only a backstop for turns that died without their normal close (the
#: final prose row or the next user message) — e.g. a TUI interrupt or a Goose
#: crash. Minutes, not seconds: a legitimately long tool call writes no store
#: rows while it runs, and closing early makes the spinner flicker on exactly
#: the calls the live card is most useful for.
_STALLED_TURN_IDLE_S = 300.0

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


@dataclass
class _MirrorItem:
    """One conversation item ready to POST, plus the message id that produced it."""

    msg_id: int
    item_type: str
    item_data: dict[str, object]
    response_id: str


def _extract_tool_calls(content_json: str) -> list[tuple[str, str, str]]:
    """Extract tool calls from a Goose assistant ``content_json`` value.

    Goose records tool calls as ``{"type": "toolreq", "id": ..., "name": ...,
    "parameters": ...}`` parts inside the assistant message content list.

    :returns: List of ``(tool_id, tool_name, arguments_json)`` triples — one
        entry per ``toolreq`` part. Empty when there are no tool calls or the
        content cannot be parsed.
    """
    try:
        parts = json.loads(content_json)
    except ValueError:
        return []
    if not isinstance(parts, list):
        return []
    calls: list[tuple[str, str, str]] = []
    for part in parts:
        if not isinstance(part, dict) or part.get("type") != "toolreq":
            continue
        tool_id = part.get("id")
        if not isinstance(tool_id, str) or not tool_id:
            continue
        name = part.get("name") or part.get("tool_name") or ""
        if not isinstance(name, str):
            name = str(name)
        # Goose stores arguments as "parameters"; tolerate "input" / "arguments".
        raw_params = part.get("parameters") or part.get("input") or part.get("arguments") or {}
        try:
            args_json = json.dumps(raw_params) if not isinstance(raw_params, str) else raw_params
        except (TypeError, ValueError):
            args_json = "{}"
        calls.append((tool_id, name, args_json))
    return calls


def _extract_tool_result(content_json: str) -> tuple[str, str] | None:
    """Extract a tool result from a Goose ``tool`` role ``content_json`` value.

    Goose records tool results as ``{"type": "toolresp", "id": ..., "output": ...}``
    parts inside a ``role="tool"`` message.

    :returns: ``(tool_id, output_text)`` if a ``toolresp`` part is found, else
        ``None``.
    """
    try:
        parts = json.loads(content_json)
    except ValueError:
        return None
    if not isinstance(parts, list):
        # Bare dict: wrap for uniform handling.
        parts = [parts] if isinstance(parts, dict) else []
    for part in parts:
        if not isinstance(part, dict) or part.get("type") != "toolresp":
            continue
        # Goose uses "id" on toolresp to match the toolreq; tolerate "tool_use_id".
        tool_id = part.get("id") or part.get("tool_use_id")
        if not isinstance(tool_id, str) or not tool_id:
            continue
        raw_output = part.get("output") or part.get("content") or ""
        if isinstance(raw_output, dict):
            # Some providers wrap the output in {"text": ...}.
            raw_output = raw_output.get("text") or json.dumps(raw_output)
        output_text = str(raw_output) if not isinstance(raw_output, str) else raw_output
        return tool_id, output_text
    return None


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


def _message_to_items(
    msg_id: int,
    role: object,
    content_json: object,
    agent_name: str,
    turn_response_id: str | None,
) -> list[_MirrorItem]:
    """Convert one ``messages`` row to zero or more mirror items.

    Returns an empty list for rows that produce no postable content (system,
    empty assistant, etc.).  A non-empty ``turn_response_id`` is stamped on
    every item so the web UI can drive live streaming for that turn.

    :param turn_response_id: The active turn's response id, or ``None`` when no
        turn is open yet (e.g. the very first message before any assistant row).
    """
    if not isinstance(role, str) or not isinstance(content_json, str):
        return []

    # Per-message fallback id; overridden by the per-turn id for assistant/tool items.
    per_msg_id = f"goose:{msg_id}"

    if role == "user":
        text = _ATTACHMENT_MARKER_RE.sub("", _content_text(content_json)).strip()
        if not text:
            return []
        return [
            _MirrorItem(
                msg_id=msg_id,
                item_type="message",
                item_data={"role": "user", "content": [{"type": "input_text", "text": text}]},
                response_id=per_msg_id,
            )
        ]

    if role == "assistant":
        rid = turn_response_id or per_msg_id
        items: list[_MirrorItem] = []
        # Prose bubble (may be empty for tool-only steps; skip if so).
        text = _ATTACHMENT_MARKER_RE.sub("", _content_text(content_json)).strip()
        if text:
            items.append(
                _MirrorItem(
                    msg_id=msg_id,
                    item_type="message",
                    item_data={
                        "role": "assistant",
                        "agent": agent_name,
                        "content": [{"type": "output_text", "text": text}],
                    },
                    response_id=rid,
                )
            )
        # Tool-call cards: one function_call item per toolreq part.
        for tool_id, tool_name, args_json in _extract_tool_calls(content_json):
            items.append(
                _MirrorItem(
                    msg_id=msg_id,
                    item_type="function_call",
                    item_data={
                        "agent": agent_name,
                        "name": tool_name,
                        "arguments": args_json,
                        "call_id": tool_id,
                    },
                    response_id=rid,
                )
            )
        return items

    if role == "tool":
        rid = turn_response_id or per_msg_id
        result = _extract_tool_result(content_json)
        if result is None:
            return []
        tool_id, output_text = result
        return [
            _MirrorItem(
                msg_id=msg_id,
                item_type="function_call_output",
                item_data={"call_id": tool_id, "output": output_text},
                response_id=rid,
            )
        ]

    return []  # system / other scaffolding


def _read_new_rows(
    db_path: Path, goose_session_id: str, last_id: int
) -> list[tuple[int, str, str]]:
    """Read raw ``(id, role, content_json)`` rows with ``id > last_id``.

    Returns an empty list on any SQLite error (live DB briefly unreadable
    mid-checkpoint is normal; the caller retries on the next poll).
    """
    con = _connect_ro(db_path)
    if con is None:
        return []
    try:
        return con.execute(
            "SELECT id, role, content_json FROM messages "
            "WHERE session_id = ? AND id > ? ORDER BY id",
            (goose_session_id, last_id),
        ).fetchall()
    except sqlite3.Error as exc:
        _warn_sqlite_once("message read", exc)
        return []
    finally:
        con.close()


def _read_new_items(
    db_path: Path, goose_session_id: str, last_id: int, agent_name: str
) -> list[_MirrorItem]:
    """Read ``messages`` rows with ``id > last_id`` for this session as items.

    A skipped row (tool/system/empty) still advances the cursor via a sentinel
    so it is never reconsidered.

    .. note::
        This function is retained for backward compatibility with existing tests.
        The main poll loop uses :func:`_read_new_rows` directly so it can track
        per-turn state while iterating.
    """
    rows = _read_new_rows(db_path, goose_session_id, last_id)
    result: list[_MirrorItem] = []
    for msg_id, role, content_json in rows:
        items = _message_to_items(msg_id, role, content_json, agent_name, turn_response_id=None)
        if items:
            result.extend(items)
        else:
            result.append(_MirrorItem(msg_id=msg_id, item_type="", item_data={}, response_id=""))
    return result


@dataclass
class _TurnState:
    """Live-card lifecycle state for the assistant turn currently being mirrored.

    In-memory only; rebuilt from the store on restart by :func:`_replay_open_turn`.

    :param response_id: Shared response id stamped on the open turn's items
        (``goose:turn:{first_msg_id}``), or ``None`` before any turn opened.
        Retained after a close so a turn that unexpectedly resumes rejoins its
        original streaming group instead of minting a new one.
    :param live: A ``running`` edge for ``response_id`` was posted and not yet
        closed by an ``idle``.
    :param pending_tool_call_ids: ``toolreq`` ids still awaiting a ``toolresp``;
        while non-empty the turn is provably mid-tool-call, so a prose row
        cannot be its final message.
    :param last_activity_ts: Monotonic time of the last store row seen while a
        turn was open, for the stalled-turn backstop close.
    """

    response_id: str | None = None
    live: bool = False
    pending_tool_call_ids: set[str] = field(default_factory=set)
    last_activity_ts: float | None = None

    def reset(self) -> None:
        """Forget the turn (a user row closed it, or replay found it finished)."""
        self.response_id = None
        self.live = False
        self.pending_tool_call_ids.clear()
        self.last_activity_ts = None


def _row_completes_turn(state: _TurnState, role: str, items: list[_MirrorItem]) -> bool:
    """Advance *state*'s tool-call ledger by one mirrored row; ``True`` if the
    row is the turn's final message.

    Goose's agent loop keeps stepping while the model returns tool calls and
    stops on a plain reply, so an assistant prose row with no tool calls (and
    none outstanding) is the authoritative end of the turn.
    """
    if role not in ("assistant", "tool") or not items:
        return False
    saw_call = False
    for item in items:
        call_id = str(item.item_data.get("call_id", ""))
        if item.item_type == "function_call":
            state.pending_tool_call_ids.add(call_id)
            saw_call = True
        elif item.item_type == "function_call_output":
            state.pending_tool_call_ids.discard(call_id)
    return role == "assistant" and not saw_call and not state.pending_tool_call_ids


def _read_open_turn_rows(
    db_path: Path, goose_session_id: str, last_id: int
) -> list[tuple[int, str, str]]:
    """Read the already-processed rows of the possibly-open turn: everything
    after the last user row, up to and including the ``last_id`` cursor.

    Empty when the cursor sits on a user row (no turn open) or on error.
    """
    con = _connect_ro(db_path)
    if con is None:
        return []
    try:
        return con.execute(
            "SELECT id, role, content_json FROM messages "
            "WHERE session_id = ? AND id <= ? "
            "AND id > COALESCE((SELECT MAX(id) FROM messages "
            "WHERE session_id = ? AND id <= ? AND role = 'user'), 0) "
            "ORDER BY id",
            (goose_session_id, last_id, goose_session_id, last_id),
        ).fetchall()
    except sqlite3.Error as exc:
        _warn_sqlite_once("open-turn replay read", exc)
        return []
    finally:
        con.close()


async def _replay_open_turn(
    client: httpx.AsyncClient,
    *,
    db: Path,
    goose_session_id: str,
    last_id: int,
    agent_name: str,
    session_id: str,
    state: _TurnState,
) -> None:
    """Rebuild *state* for a turn that was mid-flight when the previous run stopped.

    Turn state is in-memory, so without this a restart would mint a fresh turn id
    for the remaining rows (splitting the streaming group of items already posted
    under the original id) and could never close a ``running`` edge the previous
    run posted (a spinner that never settles). Replaying the open turn's rows
    through the same transitions restores the original id and ledger; if the
    replayed turn already ended, the closing ``idle`` is (re-)posted — redundant
    when the previous run got there first, but idempotent for the UI.
    """
    rows = await asyncio.to_thread(_read_open_turn_rows, db, goose_session_id, last_id)
    any_items = False
    completed = False
    for msg_id, role, content_json in rows:
        if role in ("assistant", "tool") and state.response_id is None:
            state.response_id = f"goose:turn:{msg_id}"
        items = _message_to_items(msg_id, role, content_json, agent_name, state.response_id)
        any_items = any_items or bool(items)
        # Item-less scaffolding rows (system, empty) don't reopen a turn whose
        # final prose already landed.
        completed = _row_completes_turn(state, role, items) or (completed and not items)
    if state.response_id is None or not any_items:
        # No turn open, or one that never produced a postable item (so the
        # previous run never posted `running` for it either).
        state.reset()
        return
    if completed:
        await post_external_session_status(
            client, session_id=session_id, status="idle", response_id=state.response_id
        )
        state.reset()
    else:
        state.live = True
        state.last_activity_ts = time.monotonic()


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

    state = _TurnState()
    # A previous run may have died mid-turn; rebuild the turn state from the
    # store before mirroring anything new. Retried in-band (not via the
    # supervisor) if the replay's idle post hits a transient server error.
    needs_replay = goose_session_id is not None and last_id > 0

    async with httpx.AsyncClient(
        base_url=base_url, headers=headers, auth=auth, timeout=timeout
    ) as client:
        while True:
            try:
                if needs_replay and goose_session_id is not None:
                    await _replay_open_turn(
                        client,
                        db=db,
                        goose_session_id=goose_session_id,
                        last_id=last_id,
                        agent_name=agent_name,
                        session_id=session_id,
                        state=state,
                    )
                    needs_replay = False

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
                    rows = await asyncio.to_thread(_read_new_rows, db, goose_session_id, last_id)
                    for msg_id, role, content_json in rows:
                        if role == "user":
                            # A new user turn authoritatively closes the previous one.
                            if state.live:
                                await post_external_session_status(
                                    client,
                                    session_id=session_id,
                                    status="idle",
                                    response_id=state.response_id,
                                )
                            state.reset()

                        elif role in ("assistant", "tool") and state.response_id is None:
                            # First assistant/tool row of a new turn: mint the turn id.
                            state.response_id = f"goose:turn:{msg_id}"

                        items = _message_to_items(
                            msg_id, role, content_json, agent_name, state.response_id
                        )

                        # Post running once per turn, before the turn's first item.
                        if items and role in ("assistant", "tool") and not state.live:
                            await post_external_session_status(
                                client,
                                session_id=session_id,
                                status="running",
                                response_id=state.response_id,
                            )
                            state.live = True

                        for item in items:
                            await _post_conversation_item(client, session_id=session_id, item=item)

                        # The turn's final prose row closes its live card at once.
                        if _row_completes_turn(state, role, items) and state.live:
                            await post_external_session_status(
                                client,
                                session_id=session_id,
                                status="idle",
                                response_id=state.response_id,
                            )
                            state.live = False

                        if state.response_id is not None:
                            # Any store write while a turn is open proves Goose is
                            # alive; only true silence should trip the backstop.
                            state.last_activity_ts = time.monotonic()

                        last_id = msg_id
                        _write_state(
                            bridge_dir,
                            _ForwardState(goose_session_id=goose_session_id, last_id=last_id),
                        )

                    # Backstop: a turn that died without its normal close (TUI
                    # interrupt, Goose crash) must not leave a spinner forever.
                    if (
                        state.live
                        and state.last_activity_ts is not None
                        and time.monotonic() - state.last_activity_ts > _STALLED_TURN_IDLE_S
                    ):
                        await post_external_session_status(
                            client,
                            session_id=session_id,
                            status="idle",
                            response_id=state.response_id,
                        )
                        # Keep response_id: a late resume rejoins the same turn.
                        state.live = False

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
