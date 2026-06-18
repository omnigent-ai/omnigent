"""TUI→web forwarder for the cursor-native harness.

The ``omnigent cursor`` wrapper launches the real ``cursor-agent`` TUI in a
runner-owned tmux pane, and :mod:`omnigent.cursor_native_bridge` injects web-UI
messages into it. That covers the web→TUI direction, but the *embedded terminal*
is then the only surface that reflects the agent's work — the Omnigent
conversation view (chat bubbles, title, working spinner) stays empty because
nothing mirrors the TUI's transcript back into the session.

This module is that missing mirror — the cursor analog of
:mod:`omnigent.claude_native_forwarder` (which tails Claude Code's JSONL
transcript) and :mod:`omnigent.codex_native_forwarder` (which subscribes to the
Codex app-server). cursor-agent has neither a JSONL transcript nor an event
socket; its conversation lives in a **content-addressed SQLite store** at
``~/.cursor/chats/<md5(cwd)>/<chat-id>/store.db``. Each message is a plain-JSON
``blobs`` row (``role`` + ``content``); SQLite ``rowid`` order is conversation
order (the binary Merkle manifest that also lives there is *not* needed). We poll
that store, extract new user/assistant messages, and POST them as
``external_conversation_item`` events — which also seeds the session title from
the first user message (the same hook claude/codex rely on).

Status (``running``/``idle``) is intentionally NOT posted here: the runner's
PTY-activity watcher owns those edges for cursor-native (see
``_publish_turn_status`` in :mod:`omnigent.runner.app`), exactly as for
claude-native and pi-native.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

_logger = logging.getLogger(__name__)

#: Seconds between store polls. Cursor turns run for many seconds/minutes in the
#: TUI, so a sub-second cadence would add load without improving perceived
#: latency; ~0.7s keeps the chat view feeling live.
_DEFAULT_POLL_INTERVAL_S = 0.7
_POST_TIMEOUT_S = 30.0

# Supervisor backoff (mirrors claude_native_forwarder.supervise_forwarder).
_SUPERVISOR_INITIAL_BACKOFF_S = 1.0
_SUPERVISOR_MAX_BACKOFF_S = 30.0
_SUPERVISOR_HEALTHY_UPTIME_S = 60.0

#: Discovery tolerance: a chat dir whose ``createdAtMs`` is within this many ms
#: *before* the recorded launch time still counts as this session's chat. Covers
#: the small skew between the runner stamping ``launch_epoch_ms`` and cursor
#: writing the chat's ``meta.json`` once the first message lands.
_DISCOVERY_SKEW_MS = 10_000

_STATE_FILE = "cursor_forwarder.json"

# cursor wraps the real prompt the user typed in ``<user_query>…</user_query>``
# and prepends a large ``<user_info>…`` context dump as a separate user blob.
# We forward only the former (unwrapped) and skip the latter.
_USER_QUERY_RE = re.compile(r"<user_query>(.*?)</user_query>", re.DOTALL)


@dataclass
class _ForwardState:
    """Durable forwarder cursor, persisted to ``bridge_dir/cursor_forwarder.json``.

    :param store_path: Absolute path of the cursor chat store being tailed, or
        ``None`` before one is discovered.
    :param last_rowid: Highest ``blobs.rowid`` already processed for
        ``store_path`` (forwarded or deliberately skipped). The store is
        append-only and content-addressed, so rowids only grow — tracking the
        high-water mark is sufficient dedup with O(1) state.
    """

    store_path: str | None = None
    last_rowid: int = 0


def _read_state(bridge_dir: Path) -> _ForwardState:
    """Load the persisted forward cursor, or a cold default."""
    try:
        raw = (bridge_dir / _STATE_FILE).read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return _ForwardState()
    store_path = data.get("store_path")
    last_rowid = data.get("last_rowid")
    return _ForwardState(
        store_path=store_path if isinstance(store_path, str) else None,
        last_rowid=last_rowid if isinstance(last_rowid, int) else 0,
    )


def _write_state(bridge_dir: Path, state: _ForwardState) -> None:
    """Atomically persist the forward cursor (tmp write + rename)."""
    with contextlib.suppress(OSError):
        bridge_dir.mkdir(parents=True, exist_ok=True)
        tmp = bridge_dir / (_STATE_FILE + ".tmp")
        tmp.write_text(
            json.dumps({"store_path": state.store_path, "last_rowid": state.last_rowid}),
            encoding="utf-8",
        )
        os.replace(tmp, bridge_dir / _STATE_FILE)


def _cursor_chats_root() -> Path:
    """Return ``~/.cursor/chats`` for the process's HOME (shared with the TUI)."""
    return Path.home() / ".cursor" / "chats"


def _workspace_hash(workspace: str) -> str:
    """Return cursor's chat-dir key for *workspace* (``md5`` of the path)."""
    return hashlib.md5(workspace.encode("utf-8")).hexdigest()


def _chat_created_ms(chat_dir: Path) -> int:
    """Return ``meta.json``'s ``createdAtMs`` for *chat_dir* (0 if unreadable)."""
    try:
        meta = json.loads((chat_dir / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    created = meta.get("createdAtMs")
    return created if isinstance(created, int) else 0


def _discover_store(workspace: str, launch_epoch_ms: int) -> Path | None:
    """Locate this session's cursor chat store under ``~/.cursor/chats``.

    cursor names each workspace's chat dir ``md5(<cwd>)`` and each chat
    ``<chat-id>`` with a ``meta.json`` carrying ``createdAtMs``. The TUI creates
    the dir lazily on the first message, so we pick the newest chat created at or
    after this session's launch — first under the exact ``md5(workspace)`` dir,
    then (in case cursor normalizes the path differently than we hash it) across
    every workspace dir as a fallback.

    :param workspace: The session's working directory, exactly as passed to the
        cursor TUI.
    :param launch_epoch_ms: Wall-clock ms when this terminal launched; only
        chats created at/after this (minus a small skew) are candidates.
    :returns: The newest matching ``store.db`` path, or ``None`` if none yet.
    """
    root = _cursor_chats_root()
    floor_ms = launch_epoch_ms - _DISCOVERY_SKEW_MS
    exact_dir = root / _workspace_hash(workspace)
    best, best_created = _scan_hash_dir(exact_dir, floor_ms, None, -1)
    if best is not None:
        return best
    # Fallback (only when the exact hash dir yielded nothing, so it can't shadow
    # a precise match): cursor may normalize the path differently than we hash
    # it — scan every workspace dir for the newest chat created since launch.
    if not root.is_dir():
        return None
    for hash_dir in sorted(root.iterdir()):
        if hash_dir == exact_dir or not hash_dir.is_dir():
            continue
        best, best_created = _scan_hash_dir(hash_dir, floor_ms, best, best_created)
    return best


def _scan_hash_dir(
    hash_dir: Path, floor_ms: int, best: Path | None, best_created: int
) -> tuple[Path | None, int]:
    """Update ``(best, best_created)`` with the newest qualifying chat in *hash_dir*."""
    if not hash_dir.is_dir():
        return best, best_created
    for chat_dir in hash_dir.iterdir():
        store = chat_dir / "store.db"
        if not store.is_file():
            continue
        created = _chat_created_ms(chat_dir)
        if created >= floor_ms and created > best_created:
            best, best_created = store, created
    return best, best_created


def _content_text(content: object) -> str:
    """Join the ``text`` of a cursor message's content (str or part list)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            p.get("text", "")
            for p in content
            if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str)
        ]
        return "".join(parts)
    return ""


def _strip_control_chars(text: str) -> str:
    """Drop C0 control bytes cursor embeds in stored prompts (keep \\n and \\t)."""
    return "".join(ch for ch in text if ch >= " " or ch in "\n\t")


def _unwrap_user_query(text: str) -> str | None:
    """Return the human prompt from a stored user blob, or ``None`` to skip it.

    A real user turn is wrapped in ``<user_query>…</user_query>``; the large
    ``<user_info>…`` context dump cursor prepends has no such wrapper and is not
    a conversation message, so it is skipped.
    """
    match = _USER_QUERY_RE.search(text)
    if match is None:
        return None
    return _strip_control_chars(match.group(1)).strip() or None


@dataclass
class _MirrorItem:
    """One conversation item ready to POST, plus the rowid that produced it."""

    rowid: int
    item_type: str
    item_data: dict[str, object]
    response_id: str


def _read_blob_rows(store_path: Path, last_rowid: int) -> list[tuple[int, str, object]]:
    """Return ``(rowid, id, data)`` for blobs with ``rowid > last_rowid``.

    A *live* cursor chat keeps almost all of its data in the ``-wal`` sidecar
    (the main ``store.db`` is nearly empty until cursor checkpoints), so the
    store must be opened in a way that reads the WAL. ``?mode=ro&immutable=1``
    is wrong — it tells SQLite the file never changes and to ignore the WAL,
    yielding an empty database (``no such table: blobs``). ``mode=ro`` reads the
    WAL via the live ``-shm``; a plain connection is the fallback for the rare
    window where ``-shm`` is momentarily absent. Both are read-only in practice
    (only SELECTs are issued).
    """
    sql = "SELECT rowid, id, data FROM blobs WHERE rowid > ? ORDER BY rowid"
    for uri, kw in ((f"file:{store_path}?mode=ro", {"uri": True}), (str(store_path), {})):
        try:
            con = sqlite3.connect(uri, timeout=5.0, **kw)
        except sqlite3.Error:
            continue
        try:
            return con.execute(sql, (last_rowid,)).fetchall()
        except sqlite3.Error:
            continue
        finally:
            con.close()
    return []


def _read_new_items(store_path: Path, last_rowid: int, agent_name: str) -> list[_MirrorItem]:
    """Read role-bearing blobs with ``rowid > last_rowid`` as conversation items.

    Reads the latest WAL-committed state each call so new messages surface while
    the TUI keeps writing.

    :param store_path: The cursor chat store to read.
    :param last_rowid: High-water rowid already processed.
    :param agent_name: Agent label stamped on assistant items.
    :returns: New items in conversation (rowid) order; the caller advances its
        cursor to the max rowid returned even for skipped (system/context) rows.
    """
    items: list[_MirrorItem] = []
    rows = _read_blob_rows(store_path, last_rowid)
    for rowid, blob_id, data in rows:
        item = _blob_to_item(rowid, blob_id, data, agent_name)
        if item is not None:
            items.append(item)
        else:
            # A skipped row (system prompt, context dump, non-JSON Merkle node)
            # still advances the cursor so it is never reconsidered: emit a
            # sentinel carrying just the rowid.
            items.append(_MirrorItem(rowid=rowid, item_type="", item_data={}, response_id=""))
    return items


def _blob_to_item(rowid: int, blob_id: str, data: object, agent_name: str) -> _MirrorItem | None:
    """Convert one ``blobs`` row to a mirror item, or ``None`` to skip it."""
    if isinstance(data, (bytes, bytearray)):
        try:
            data = data.decode("utf-8")
        except UnicodeDecodeError:
            return None  # binary Merkle-tree node, not a message
    if not isinstance(data, str):
        return None
    try:
        obj = json.loads(data)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    role = obj.get("role")
    response_id = f"cursor:{blob_id}"
    if role == "user":
        prompt = _unwrap_user_query(_content_text(obj.get("content")))
        if not prompt:
            return None
        return _MirrorItem(
            rowid=rowid,
            item_type="message",
            item_data={"role": "user", "content": [{"type": "input_text", "text": prompt}]},
            response_id=response_id,
        )
    if role == "assistant":
        text = _content_text(obj.get("content")).strip()
        if not text:
            return None  # reasoning-only / tool-only turn with no prose
        return _MirrorItem(
            rowid=rowid,
            item_type="message",
            item_data={
                "role": "assistant",
                "agent": agent_name,
                "content": [{"type": "output_text", "text": text}],
            },
            response_id=response_id,
        )
    return None  # system or other scaffolding


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


async def forward_cursor_store_to_session(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    bridge_dir: Path,
    agent_name: str,
    workspace: str,
    launch_epoch_ms: int,
    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
    auth: httpx.Auth | None = None,
) -> None:
    """Tail the cursor chat store and mirror new messages into the AP session.

    Discovers this session's store (newest chat under ``md5(workspace)`` created
    at/after ``launch_epoch_ms``), then polls it, posting each new user/assistant
    message as an ``external_conversation_item``. The high-water rowid is
    persisted to ``bridge_dir`` so a supervisor restart resumes without
    re-posting; if discovery resolves a *different* store than the persisted one
    (a cold resume relaunched a fresh chat), the cursor resets to that store.

    :param base_url: Omnigent server base URL.
    :param headers: Static HTTP headers (auth normally via ``auth``).
    :param session_id: Omnigent session/conversation id.
    :param bridge_dir: The cursor-native bridge dir (holds the persisted cursor).
    :param agent_name: Agent label stamped on mirrored assistant items.
    :param workspace: The session's working directory (cursor's chat-dir key).
    :param launch_epoch_ms: Wall-clock ms when this terminal launched.
    :param poll_interval_s: Seconds between store polls.
    :param auth: Optional refresh-capable httpx Auth for remote deployments.
    :returns: Never normally returns; cancel the task to stop it.
    """
    persisted = _read_state(bridge_dir)
    store_path: Path | None = None
    last_rowid = 0
    timeout = httpx.Timeout(_POST_TIMEOUT_S)
    async with httpx.AsyncClient(
        base_url=base_url, headers=headers, auth=auth, timeout=timeout
    ) as client:
        while True:
            try:
                if store_path is None or not store_path.exists():
                    resolved = await asyncio.to_thread(_discover_store, workspace, launch_epoch_ms)
                    if resolved is not None:
                        store_path = resolved
                        if persisted.store_path == str(resolved):
                            last_rowid = persisted.last_rowid
                        else:
                            last_rowid = 0
                            _write_state(
                                bridge_dir,
                                _ForwardState(store_path=str(resolved), last_rowid=0),
                            )
                        persisted = _ForwardState()  # consumed
                if store_path is not None and store_path.exists():
                    items = await asyncio.to_thread(
                        _read_new_items, store_path, last_rowid, agent_name
                    )
                    for item in items:
                        if item.item_type:
                            await _post_conversation_item(client, session_id=session_id, item=item)
                        last_rowid = item.rowid
                        _write_state(
                            bridge_dir,
                            _ForwardState(store_path=str(store_path), last_rowid=last_rowid),
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception(
                    "cursor forwarder poll failed; session=%s store=%s",
                    session_id,
                    store_path,
                )
            await asyncio.sleep(poll_interval_s)


def _supervisor_monotonic() -> float:
    """Indirection so tests can stub the supervisor's clock."""
    return time.monotonic()


async def _supervisor_sleep(seconds: float) -> None:
    """Indirection so tests can stub the supervisor's backoff sleep."""
    await asyncio.sleep(seconds)


async def supervise_cursor_forwarder(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    bridge_dir: Path,
    agent_name: str,
    workspace: str,
    launch_epoch_ms: int,
    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
    auth: httpx.Auth | None = None,
) -> None:
    """Run :func:`forward_cursor_store_to_session` under a restart supervisor.

    Mirrors :func:`omnigent.claude_native_forwarder.supervise_forwarder`: the
    forwarder's own loop already swallows per-poll errors, but a crash in client
    setup or an unexpected return would otherwise desync the chat view forever.
    This restarts with bounded exponential backoff; :class:`asyncio.CancelledError`
    propagates so teardown is clean. The persisted rowid cursor means restarts
    resume exactly where they left off.

    :param base_url: Omnigent server base URL.
    :param headers: Static HTTP headers for Omnigent requests.
    :param session_id: Omnigent session/conversation id.
    :param bridge_dir: The cursor-native bridge dir.
    :param agent_name: Agent label stamped on mirrored assistant items.
    :param workspace: The session's working directory.
    :param launch_epoch_ms: Wall-clock ms when this terminal launched.
    :param poll_interval_s: Seconds between store polls.
    :param auth: Optional refresh-capable httpx Auth.
    :returns: Never normally returns; cancel the task to stop it.
    """
    backoff_s = _SUPERVISOR_INITIAL_BACKOFF_S
    while True:
        run_started_at = _supervisor_monotonic()
        crash_exc: Exception | None = None
        try:
            await forward_cursor_store_to_session(
                base_url=base_url,
                headers=headers,
                session_id=session_id,
                bridge_dir=bridge_dir,
                agent_name=agent_name,
                workspace=workspace,
                launch_epoch_ms=launch_epoch_ms,
                poll_interval_s=poll_interval_s,
                auth=auth,
            )
            _logger.warning(
                "cursor forwarder returned unexpectedly; restarting; session=%s bridge_dir=%s",
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
                "cursor forwarder crashed; restarting in %.1fs; session=%s bridge_dir=%s",
                backoff_s,
                session_id,
                bridge_dir,
                exc_info=crash_exc,
            )
        await _supervisor_sleep(backoff_s)
        backoff_s = min(backoff_s * 2.0, _SUPERVISOR_MAX_BACKOFF_S)
