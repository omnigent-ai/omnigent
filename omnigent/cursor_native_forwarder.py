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

The generic, turn-agnostic "Working…" *spinner* badge is still owned by the
runner's PTY-activity watcher for cursor-native (see ``_publish_turn_status`` in
:mod:`omnigent.runner.app`), exactly as for claude-native and pi-native — that
id-less edge never wakes a parent orchestrator, so this forwarder additionally
POSTs an ``external_session_status: idle`` event once per completed turn (the
cursor ``stop`` hook's turn-end markers, tailed via
:mod:`omnigent.cursor_native_status`) — the SAME server contract
claude-/codex-/opencode-native use to mark a sub-agent turn terminal and wake its
parent's inbox.

**Live tool-call cards**: the forwarder also mirrors cursor's committed
``tool-call`` / ``tool-result`` content parts as ``function_call`` /
``function_call_output`` items, stamps every item of one assistant turn with a
shared per-turn ``response_id`` (``cursor:turn:<blob_id>``), and POSTs a
``running`` status edge carrying that id before the turn's first item. That
id-bearing edge is what makes the web UI render cursor's in-flight tool calls
LIVE (spinner + ticking timer) instead of as static completed cards; the closing
``idle`` edge (driven by the ``stop`` hook) carries the same id so the cards
settle. The open turn is rebuilt from persisted state on a supervisor restart so
a resumed turn rejoins its streaming group without re-posting ``running``.
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
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from omnigent import cursor_native_status
from omnigent._native_post_delivery import (
    post_external_session_status,
    post_may_have_been_delivered,
)
from omnigent.cursor_native_bridge import FORK_HISTORY_CLOSE_TAG, FORK_HISTORY_OPEN_TAG

_logger = logging.getLogger(__name__)

#: Seconds between store polls. Cursor turns run for many seconds/minutes in the
#: TUI, so a sub-second cadence would add load without improving perceived
#: latency; ~0.7s keeps the chat view feeling live.
_DEFAULT_POLL_INTERVAL_S = 0.7
_POST_TIMEOUT_S = 30.0

#: Max length of a mirrored item's ``response_id``. The server stores it in
#: ``conversation_items.response_id``, a ``VARCHAR(64)`` (see
#: ``omnigent.db.db_models.SqlConversationItem``). cursor's content-address blob
#: id is itself a 64-char hash, so an un-capped ``cursor:<blob_id>`` is 71 chars
#: and overflows the column — every mirror POST then 500s and, because the poll
#: loop only advances its high-water rowid after a successful POST, the forwarder
#: wedges on that one message and re-posts it forever. Cap at the column width.
#: ``response_id`` is a non-unique, non-dedup grouping label, so truncation can in
#: theory alias two blobs onto one id — that only groups two messages under one UI
#: response, never data loss.
_RESPONSE_ID_MAX_LEN = 64

#: Prefix for the per-turn ``response_id`` shared by every item of one assistant
#: turn (the user bubble keeps its own per-blob id). Stamping a turn's mirrored
#: ``function_call`` items with this id — and posting a ``running`` status edge
#: carrying the same id before the turn's first item — is what makes the web UI
#: render cursor's in-flight tool calls LIVE (spinner + ticking timer) instead of
#: as static, already-completed cards. ``cursor:turn:`` + a 64-char blob hash
#: overflows the ``VARCHAR(64)`` column, so the id is capped exactly like the
#: per-blob one (see :data:`_RESPONSE_ID_MAX_LEN`).
_TURN_ID_PREFIX = "cursor:turn:"

#: Consecutive server rejections (a 4xx, or a 5xx such as a failed DB insert) of
#: a single mirror item the poll loop tolerates before it logs and skips past
#: that item. Without this bound a rejected POST never advances ``last_rowid``,
#: so the loop re-POSTs the same item every ``_DEFAULT_POLL_INTERVAL_S`` forever
#: — mirroring nothing after it and flooding the app. A connection-level failure
#: (server unreachable) is deliberately NOT counted here: that is not the item's
#: fault, so it retries indefinitely rather than drop the conversation. At the
#: ~0.7s poll cadence this is a few seconds of retrying — enough to ride out a
#: brief transient rejection while staying firmly bounded.
_MAX_ITEM_POST_ATTEMPTS = 5

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

# A sibling session's persisted claim (naming the same ``store_path``) counts as
# a LIVE owner only if its heartbeat was refreshed within this window; an older
# claim is treated as a dead session and may be taken over. Generous relative to
# the ~0.7s poll so a brief supervisor backoff/restart never drops a live claim.
_CLAIM_FRESH_MS = 30_000

# cursor wraps the real prompt the user typed in ``<user_query>…</user_query>``
# and prepends a large ``<user_info>…`` context dump as a separate user blob.
# We forward only the former (unwrapped) and skip the latter.
_USER_QUERY_RE = re.compile(r"<user_query>(.*?)</user_query>", re.DOTALL)
# The executor injects ``[Attached: <path>]`` markers for web-UI attachments
# before pasting into the TUI; cursor stores them inside the user_query, so
# strip them from the mirrored bubble (the path is an internal bridge detail).
_ATTACHMENT_MARKER_RE = re.compile(r"\[Attached:[^\]]*\]")
# On a fork into cursor, the executor prepends the prior conversation to the
# first user message, fenced in <omnigent_fork_history>…</omnigent_fork_history>
# (cursor_native_bridge.wrap_fork_preamble). cursor stores it inside the
# user_query; strip the whole block so the mirrored bubble shows only the user's
# real text — the copied history already lives in the Omnigent timeline, so
# echoing it here would duplicate it.
#
# The match is non-greedy so it stops at the FIRST close tag: that is always the
# real one, because wrap_fork_preamble defangs any literal sentinels inside the
# replayed transcript (so the block holds exactly one real open/close pair), and
# stopping at the first close preserves a tag in the user's own message that
# sits after it. The trailing alternative strips an UNTERMINATED open block (a
# truncated paste with no close tag) to end-of-text, so it degrades gracefully
# instead of mirroring the whole raw block.
_FORK_HISTORY_RE = re.compile(
    rf"{re.escape(FORK_HISTORY_OPEN_TAG)}.*?{re.escape(FORK_HISTORY_CLOSE_TAG)}"
    rf"|{re.escape(FORK_HISTORY_OPEN_TAG)}.*",
    re.DOTALL,
)

# When an in-pane ``/summarize`` finishes, cursor-agent collapses the prior
# history into a single user blob whose plain-string ``content`` starts with
# this marker (verified against ``~/.cursor/chats/.../store.db``). Real user
# turns are a ``[{type:text}]`` list, never a bare string, so this prefix
# unambiguously identifies the post-compaction rollup. It is the only durable
# signal that the compaction the web UI requested has actually completed —
# cursor-agent has no compaction hook the way Claude Code does — so the
# forwarder maps it to an ``external_compaction_status`` "completed" edge.
_COMPACTION_SUMMARY_PREFIX = "[Previous conversation summary]:"


@dataclass
class _ForwardState:
    """Durable forwarder cursor, persisted to ``bridge_dir/cursor_forwarder.json``.

    :param store_path: Absolute path of the cursor chat store being tailed, or
        ``None`` before one is discovered.
    :param last_rowid: Highest ``blobs.rowid`` already processed for
        ``store_path`` (forwarded or deliberately skipped). The store is
        append-only and content-addressed, so rowids only grow — tracking the
        high-water mark is sufficient dedup with O(1) state.
    :param launch_epoch_ms: This session's launch time, used to break ties when
        two sessions discover the same chat: the earlier-launched (established)
        session keeps it. ``0`` for a cold default.
    :param heartbeat_ms: Wall-clock ms of the last persist. A sibling reads this
        to tell a live owner from a dead session's leftover claim (see
        :func:`_chat_claimed_by_other`). Stamped by :func:`_write_state`.
    :param turn_response_id: The open assistant turn's shared ``response_id``
        (``cursor:turn:<blob_id>``), or ``None`` when no turn is open. Persisted
        so a supervisor restart rejoins the in-flight turn's streaming group and
        keeps stamping the same id on its remaining items.
    :param turn_live: Whether a ``running`` status edge for ``turn_response_id``
        was posted and not yet closed. Persisted so a restart does not re-post a
        ``running`` edge the previous run already opened.
    """

    store_path: str | None = None
    last_rowid: int = 0
    launch_epoch_ms: int = 0
    heartbeat_ms: int = 0
    turn_response_id: str | None = None
    turn_live: bool = False


@dataclass
class _ModelMirrorState:
    """In-memory dedupe for terminal→web model-change mirroring.

    Not persisted: a supervisor restart re-posts the current ``lastUsedModel``
    on the first poll (the server no-ops if it already matches), so the web pill
    re-syncs after a restart without manual action.

    :param observed: Most recent ``lastUsedModel`` seen in the meta row.
    :param posted: Last model id already posted; the dedupe baseline (``None``
        until the first observation is posted).
    """

    observed: str | None = None
    posted: str | None = None


def _read_state(bridge_dir: Path) -> _ForwardState:
    """Load the persisted forward cursor, or a cold default."""
    try:
        raw = (bridge_dir / _STATE_FILE).read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return _ForwardState()
    store_path = data.get("store_path")
    last_rowid = data.get("last_rowid")
    launch_epoch_ms = data.get("launch_epoch_ms")
    heartbeat_ms = data.get("heartbeat_ms")
    turn_response_id = data.get("turn_response_id")
    turn_live = data.get("turn_live")
    return _ForwardState(
        store_path=store_path if isinstance(store_path, str) else None,
        last_rowid=last_rowid if isinstance(last_rowid, int) else 0,
        launch_epoch_ms=launch_epoch_ms if isinstance(launch_epoch_ms, int) else 0,
        heartbeat_ms=heartbeat_ms if isinstance(heartbeat_ms, int) else 0,
        turn_response_id=turn_response_id if isinstance(turn_response_id, str) else None,
        turn_live=bool(turn_live),
    )


def _write_state(bridge_dir: Path, state: _ForwardState) -> bool:
    """Atomically persist the forward cursor (tmp write + rename).

    :returns: ``True`` on success. A failure is logged (not silently swallowed)
        and returns ``False`` — the in-memory cursor still guards against
        within-process re-posting; only a crash before a successful persist
        could re-post, so a persistent write failure is worth surfacing.
    """
    try:
        bridge_dir.mkdir(parents=True, exist_ok=True)
        tmp = bridge_dir / (_STATE_FILE + ".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "store_path": state.store_path,
                    "last_rowid": state.last_rowid,
                    "launch_epoch_ms": state.launch_epoch_ms,
                    "turn_response_id": state.turn_response_id,
                    "turn_live": state.turn_live,
                    # Stamp the heartbeat at persist time so every poll refreshes
                    # the chat claim; a peer treats a claim older than
                    # ``_CLAIM_FRESH_MS`` as a dead session it may take over.
                    "heartbeat_ms": int(time.time() * 1000),
                }
            ),
            encoding="utf-8",
        )
        os.replace(tmp, bridge_dir / _STATE_FILE)
        return True
    except OSError:
        _logger.warning(
            "cursor forwarder could not persist state to %s", bridge_dir, exc_info=True
        )
        return False


def clear_cursor_bridge_state(bridge_dir: Path) -> None:
    """Remove the persisted forward cursor so a re-created terminal starts clean.

    Mirrors codex's ``clear_bridge_state``: the runner calls this when it
    re-creates a cursor terminal, so a stale ``store_path``/``last_rowid`` from a
    prior terminal can't make the new forwarder resume the wrong chat or carry a
    stale high-water rowid.
    """
    with contextlib.suppress(OSError):
        (bridge_dir / _STATE_FILE).unlink()


def _chat_claimed_by_other(bridge_dir: Path, store_path: Path, my_launch_ms: int) -> bool:
    """Whether another LIVE session is already mirroring *store_path*.

    cursor keeps one chat per working directory, so two cursor-native sessions
    launched in the same cwd discover the SAME store — without this guard both
    would mirror it into two separate conversations (the duplicate-session bug).
    A sibling bridge dir under the same root claims the chat when its persisted
    state names the same store with a heartbeat fresher than ``_CLAIM_FRESH_MS``.
    Ties resolve toward the EARLIER-launched session (then the lexicographically
    smaller bridge-dir name, for a deterministic, symmetric verdict), so the
    established session keeps the chat and a duplicate later launch yields.

    :param bridge_dir: This session's bridge dir (its parent is the shared root).
    :param store_path: The cursor chat store this session would mirror.
    :param my_launch_ms: This session's ``launch_epoch_ms``.
    :returns: ``True`` if a different live session owns the chat (so this session
        should not mirror it); ``False`` otherwise.
    """
    root = bridge_dir.parent
    if not root.is_dir():
        return False
    target = str(store_path)
    now_ms = int(time.time() * 1000)
    me = bridge_dir.name
    for sibling in root.iterdir():
        if sibling.name == me or not sibling.is_dir():
            continue
        other = _read_state(sibling)
        if other.store_path != target:
            continue
        if now_ms - other.heartbeat_ms > _CLAIM_FRESH_MS:
            continue  # stale claim — the owning session is gone; ignore it
        if other.launch_epoch_ms < my_launch_ms:
            return True
        if other.launch_epoch_ms == my_launch_ms and sibling.name < me:
            return True
    return False


def _get_current_rowid(store_path: Path) -> int:
    """Return the highest rowid currently in *store_path*, or 0 on any error."""
    sql = "SELECT MAX(rowid) FROM blobs"
    for uri, kw in ((f"file:{store_path}?mode=ro", {"uri": True}), (str(store_path), {})):
        try:
            con = sqlite3.connect(uri, timeout=5.0, **kw)
        except sqlite3.Error:
            continue
        try:
            row = con.execute(sql).fetchone()
            return int(row[0]) if row and row[0] is not None else 0
        except sqlite3.Error:
            continue
        finally:
            con.close()
    return 0


def preseed_resume_state(
    bridge_dir: Path,
    workspace: str,
    chat_id: str,
    launch_epoch_ms: int,
) -> bool:
    """Pre-seed the bridge state for a cold resume of a cursor-native session.

    On cold resume ``cursor-agent --resume <chatId>`` loads an existing chat
    store whose ``meta.json`` creation timestamp predates this launch.
    ``_discover_store``'s recency filter would therefore miss it, leaving the
    forwarder stuck in an empty-discovery loop and new messages unmirrored.

    Writing the known store path + current rowid here lets the forwarder skip
    discovery entirely and start tailing only messages posted after the resume.

    External contract (verified empirically against the current cursor build):
    ``cursor-agent --resume`` reuses the SAME chat store and *appends* new turns
    at higher rowids — it does not re-write prior turns as new blobs. Seeding
    ``last_rowid`` to the current max therefore mirrors only post-resume turns,
    with no duplicate-history re-post. If a future cursor build re-appended the
    prior conversation at rowids past the seed, those would be re-mirrored as
    duplicates — the e2e gate (cursor-sdk-e2e-dev) is the guard for that drift.

    :param bridge_dir: Per-session bridge directory (``bridge_dir_for_session_id``).
    :param workspace: Realpath-normalised workspace (must match the cursor TUI cwd
        so the store hash aligns).
    :param chat_id: The cursor chat id (``external_session_id``).
    :param launch_epoch_ms: Wall-clock ms of this terminal launch (used for the
        claim-ownership heartbeat).
    :returns: ``True`` when the store was found and state was written; ``False``
        when the store doesn't exist yet (unlikely on resume, but safe to handle).
    """
    store_path = _cursor_chats_root() / _workspace_hash(workspace) / chat_id / "store.db"
    if not store_path.exists():
        return False
    last_rowid = _get_current_rowid(store_path)
    _write_state(
        bridge_dir,
        _ForwardState(
            store_path=str(store_path),
            last_rowid=last_rowid,
            launch_epoch_ms=launch_epoch_ms,
        ),
    )
    return True


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
    after this session's launch under the exact ``md5(workspace)`` dir. If that
    dir has nothing (a path-hash mismatch), we fall back to other workspace dirs
    but bind ONLY when exactly one chat across them qualifies — never guessing
    among multiple, so a concurrent session or unrelated workspace can't be
    mirrored by mistake.

    :param workspace: The session's working directory, exactly as passed to the
        cursor TUI.
    :param launch_epoch_ms: Wall-clock ms when this terminal launched; only
        chats created at/after this (minus a small skew) are candidates.
    :returns: The newest matching ``store.db`` path, or ``None`` if none yet.
    """
    root = _cursor_chats_root()
    floor_ms = launch_epoch_ms - _DISCOVERY_SKEW_MS
    exact_dir = root / _workspace_hash(workspace)
    # The reliable case: the workspace is realpath-normalized on both the launch
    # and forwarder sides, so cursor's own ``md5(cwd)`` dir == ``exact_dir``.
    best, _best_created = _scan_hash_dir(exact_dir, floor_ms, None, -1)
    if best is not None:
        return best
    # Fallback ONLY for a path-hash mismatch: scan the other workspace dirs, but
    # bind only when EXACTLY ONE chat across them qualifies. With two candidates
    # we can't tell which session owns which (concurrent sessions, or an
    # unrelated workspace), so we return None and retry rather than risk
    # mirroring the wrong conversation — silent cross-talk is worse than a brief
    # delay.
    if not root.is_dir():
        return None
    matches: list[Path] = []
    for hash_dir in sorted(root.iterdir()):
        if hash_dir == exact_dir or not hash_dir.is_dir():
            continue
        for chat_dir in hash_dir.iterdir():
            store = chat_dir / "store.db"
            if store.is_file() and _chat_created_ms(chat_dir) >= floor_ms:
                matches.append(store)
    return matches[0] if len(matches) == 1 else None


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


def _extract_tool_calls(content: object) -> list[tuple[str, str, str]]:
    """Extract committed tool calls from a cursor assistant blob's ``content``.

    cursor records a tool call as a ``{"type": "tool-call", "toolCallId": …,
    "toolName": …, "args": …}`` content part (verified against cursor-agent's
    ``store.db``). An auto-run / approved call lands as clean JSON in a
    ``blobs`` row, so the forwarder's existing ``json.loads`` already sees it.

    :param content: The parsed ``content`` value (a part list, or anything else
        which yields no calls).
    :returns: ``(tool_call_id, tool_name, arguments_json)`` triples in order.
    """
    if not isinstance(content, list):
        return []
    calls: list[tuple[str, str, str]] = []
    for part in content:
        if not isinstance(part, dict) or part.get("type") != "tool-call":
            continue
        call_id = part.get("toolCallId")
        if not isinstance(call_id, str) or not call_id:
            continue
        name = part.get("toolName")
        if not isinstance(name, str) or not name:
            name = "tool"
        args = part.get("args")
        if args is None:
            args = {}
        try:
            args_json = args if isinstance(args, str) else json.dumps(args, ensure_ascii=True)
        except (TypeError, ValueError):
            args_json = "{}"
        calls.append((call_id, name, args_json))
    return calls


def _extract_tool_results(content: object) -> list[tuple[str, str]]:
    """Extract tool results from a cursor ``role="tool"`` blob's ``content``.

    cursor records a result as a ``{"type": "tool-result", "toolCallId": …,
    "result": …}`` content part; the ``toolCallId`` matches the originating
    ``tool-call`` so the web UI can settle the right card.

    :param content: The parsed ``content`` value.
    :returns: ``(tool_call_id, output_text)`` pairs in order; a structured
        ``result`` is JSON-encoded so the card always shows text.
    """
    if not isinstance(content, list):
        return []
    results: list[tuple[str, str]] = []
    for part in content:
        if not isinstance(part, dict) or part.get("type") != "tool-result":
            continue
        call_id = part.get("toolCallId")
        if not isinstance(call_id, str) or not call_id:
            continue
        raw = part.get("result")
        if raw is None:
            output = ""
        elif isinstance(raw, str):
            output = raw
        else:
            try:
                output = json.dumps(raw, ensure_ascii=True)
            except (TypeError, ValueError):
                output = str(raw)
        results.append((call_id, output))
    return results


def _turn_response_id(blob_id: str) -> str:
    """Return the per-turn ``response_id`` minted from a turn's first blob id.

    Capped at the ``conversation_items.response_id`` column width, exactly as the
    per-blob id is, so a 64-char content-hash blob id can't overflow it and wedge
    the mirror POST (see :data:`_RESPONSE_ID_MAX_LEN`).
    """
    return f"{_TURN_ID_PREFIX}{blob_id}"[:_RESPONSE_ID_MAX_LEN]


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
    inner = _FORK_HISTORY_RE.sub("", _strip_control_chars(match.group(1)))
    inner = _ATTACHMENT_MARKER_RE.sub("", inner)
    return inner.strip() or None


@dataclass
class _MirrorItem:
    """One conversation item ready to POST, plus the rowid that produced it."""

    rowid: int
    item_type: str
    item_data: dict[str, object]
    response_id: str


@dataclass
class _TurnState:
    """Per-turn grouping state, threaded through :func:`_read_new_items`.

    A turn opens on the first assistant/tool blob after a user blob and closes on
    the next user blob; every assistant/tool item in between is stamped with one
    shared :data:`response_id`, so the web UI groups them (and drives their live
    streaming lifecycle) as a single response.

    :param response_id: The open turn's shared id (``cursor:turn:<blob_id>``), or
        ``None`` when no turn is open.
    :param seen_call_ids: ``toolCallId``s already mirrored this turn — cursor can
        write a gated call twice (a framed pending frame, then a committed row),
        so dedup on the id keeps one card per call.
    """

    response_id: str | None = None
    seen_call_ids: set[str] = field(default_factory=set)

    def reset(self) -> None:
        """Forget the turn (a new user blob closed it)."""
        self.response_id = None
        self.seen_call_ids.clear()


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


def _read_last_used_model(store_path: Path) -> str | None:
    """Return the chat's currently-selected model id, or ``None`` if unavailable.

    cursor records the active model in the ``meta`` table under key ``"0"`` as a
    hex-encoded JSON blob carrying ``lastUsedModel`` — the *base* model id (e.g.
    ``"gpt-5.2"``, ``"claude-opus-4-6"``), the same namespace the curated picker
    catalog (:func:`omnigent.cursor_native.cursor_base_model_options`) and the
    ``/model`` picker use, so a mirrored value matches a picker option. It
    updates in place whenever the user switches model in the TUI, so polling it
    is how the web picker learns of a terminal-side switch (the reverse of
    :func:`omnigent.cursor_native_bridge.inject_model_command`).

    Opened ``mode=ro`` (with a plain-connection fallback) for the same
    WAL-reading reason as :func:`_read_blob_rows`; only a SELECT is issued.

    :param store_path: The cursor chat store to read.
    :returns: The ``lastUsedModel`` id, or ``None`` when the meta row is absent,
        not yet written, or malformed.
    """
    sql = "SELECT value FROM meta"
    for uri, kw in ((f"file:{store_path}?mode=ro", {"uri": True}), (str(store_path), {})):
        try:
            con = sqlite3.connect(uri, timeout=5.0, **kw)
        except sqlite3.Error:
            continue
        try:
            rows = con.execute(sql).fetchall()
        except sqlite3.Error:
            continue
        finally:
            con.close()
        for (value,) in rows:
            model = _last_used_model_from_meta_value(value)
            if model is not None:
                return model
        return None
    return None


def _last_used_model_from_meta_value(value: object) -> str | None:
    """Decode one ``meta.value`` cell and return its ``lastUsedModel``, if any.

    The cell is hex-encoded JSON text (cursor stores it that way); decode the
    hex, parse the JSON, and pull a non-empty ``lastUsedModel`` string.
    """
    if isinstance(value, str):
        try:
            raw: bytes = bytes.fromhex(value)
        except ValueError:
            return None
    elif isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
    else:
        return None
    try:
        obj = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    model = obj.get("lastUsedModel")
    if isinstance(model, str) and model.strip():
        return model.strip()
    return None


async def _post_model_change_if_new(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    state: _ModelMirrorState,
    model: str | None,
) -> None:
    """Mirror the terminal-observed model to ``model_override``, deduped.

    Posts ``external_model_change`` whenever the observed base model id differs
    from what we last posted — *including the very first observation*. Unlike
    claude-native (which seeds the first value without posting, because its pill
    already falls back to a Claude-ish ``llmModel``), cursor must post the first
    observation: a cursor-native session has no per-session llm model, so an
    un-pinned session falls back to omnigent's default (e.g. "fable") in the Web
    UI pill — meaningless for cursor. Surfacing the real model immediately fixes
    that and makes the picker highlight correct from the first poll. Safe to
    post the first value because cursor has no bind-time sticky-model handoff to
    clobber (see ``nativeModelFamilyForSession`` in the web store — cursor is
    not a native model family), and the server no-ops when the value already
    matches ``model_override``.

    Best-effort: a failed POST leaves ``posted`` behind ``observed`` so the next
    poll retries.

    :param client: Omnigent HTTP client.
    :param session_id: Omnigent session/conversation id.
    :param state: Per-session dedupe state, mutated in place.
    :param model: Model id observed this poll, or ``None`` when the meta row
        carried no usable id (does not clear a previously-observed value).
    """
    if model is not None:
        state.observed = model
    if state.observed is None or state.observed == state.posted:
        return
    try:
        resp = await client.post(
            f"/v1/sessions/{session_id}/events",
            json={"type": "external_model_change", "data": {"model": state.observed}},
        )
        resp.raise_for_status()
        state.posted = state.observed
    except httpx.HTTPError:
        # Leave posted behind observed so the next poll retries.
        _logger.warning(
            "Failed to mirror cursor model change to session=%s; web picker may lag",
            session_id,
        )


def _blob_role(data: object) -> str | None:
    """Peek a blob's ``role`` (``"user"`` / ``"assistant"`` / ``"tool"``) or ``None``.

    Used to open/close a turn before mapping the blob to items; a binary Merkle
    node or non-message row yields ``None``.
    """
    if isinstance(data, (bytes, bytearray)):
        try:
            data = data.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(data, str):
        return None
    try:
        obj = json.loads(data)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    role = obj.get("role")
    return role if isinstance(role, str) else None


def _read_new_items(
    store_path: Path,
    last_rowid: int,
    agent_name: str,
    turn: _TurnState | None = None,
) -> list[_MirrorItem]:
    """Read role-bearing blobs with ``rowid > last_rowid`` as conversation items.

    Reads the latest WAL-committed state each call so new messages surface while
    the TUI keeps writing. When *turn* is provided its per-turn grouping id is
    minted on the first assistant/tool blob of a turn and stamped on every
    assistant/tool item until the next user blob closes it, so a turn's prose,
    tool-call and tool-result items share one ``response_id`` (the id-bearing
    edges that drive live tool-call cards). Without *turn* every item keeps its
    per-blob id (the pre-live behaviour).

    :param store_path: The cursor chat store to read.
    :param last_rowid: High-water rowid already processed.
    :param agent_name: Agent label stamped on assistant items.
    :param turn: Mutable per-turn grouping state, or ``None`` for per-blob ids.
    :returns: New items in conversation (rowid) order; the caller advances its
        cursor to the max rowid returned even for skipped (system/context) rows.
    """
    items: list[_MirrorItem] = []
    rows = _read_blob_rows(store_path, last_rowid)
    for rowid, blob_id, data in rows:
        turn_rid: str | None = None
        seen: set[str] | None = None
        if turn is not None:
            role = _blob_role(data)
            if role == "user":
                turn.reset()
            elif role in ("assistant", "tool") and turn.response_id is None:
                turn.response_id = _turn_response_id(blob_id)
            turn_rid = turn.response_id
            seen = turn.seen_call_ids
        blob_items = _blob_to_items(rowid, blob_id, data, agent_name, turn_rid, seen)
        if blob_items:
            items.extend(blob_items)
        else:
            # A skipped row (system prompt, context dump, non-JSON Merkle node)
            # still advances the cursor so it is never reconsidered: emit a
            # sentinel carrying just the rowid.
            items.append(_MirrorItem(rowid=rowid, item_type="", item_data={}, response_id=""))
    return items


def _blob_to_items(
    rowid: int,
    blob_id: str,
    data: object,
    agent_name: str,
    turn_response_id: str | None = None,
    seen_call_ids: set[str] | None = None,
) -> list[_MirrorItem]:
    """Convert one ``blobs`` row to zero or more mirror items.

    A user blob yields at most one bubble (or a compaction-completed signal); an
    assistant blob yields its prose bubble (if any) followed by one
    ``function_call`` per committed ``tool-call`` part; a ``tool`` blob yields a
    ``function_call_output`` per ``tool-result`` part. ``turn_response_id`` (when
    set) is stamped on the assistant/tool items so a turn's cards stream live;
    the user bubble always keeps its own per-blob id.
    """
    if isinstance(data, (bytes, bytearray)):
        try:
            data = data.decode("utf-8")
        except UnicodeDecodeError:
            return []  # binary Merkle-tree node, not a message
    if not isinstance(data, str):
        return []
    try:
        obj = json.loads(data)
    except ValueError:
        return []
    if not isinstance(obj, dict):
        return []
    role = obj.get("role")
    per_blob_id = f"cursor:{blob_id}"[:_RESPONSE_ID_MAX_LEN]
    rid = turn_response_id or per_blob_id
    if role == "user":
        content = obj.get("content")
        # cursor writes the post-/summarize history rollup as a user blob with a
        # plain-string content (real user turns are a ``[{type:text}]`` list).
        # Surface it as a compaction-completed signal, not a chat bubble, so the
        # forwarder can tell the web UI the compaction actually finished.
        if isinstance(content, str) and content.startswith(_COMPACTION_SUMMARY_PREFIX):
            return [
                _MirrorItem(
                    rowid=rowid,
                    item_type="compaction_completed",
                    item_data={},
                    response_id=per_blob_id,
                )
            ]
        prompt = _unwrap_user_query(_content_text(content))
        if not prompt:
            return []
        return [
            _MirrorItem(
                rowid=rowid,
                item_type="message",
                item_data={"role": "user", "content": [{"type": "input_text", "text": prompt}]},
                response_id=per_blob_id,
            )
        ]
    if role == "assistant":
        content = obj.get("content")
        items: list[_MirrorItem] = []
        text = _content_text(content).strip()
        if text:
            items.append(
                _MirrorItem(
                    rowid=rowid,
                    item_type="message",
                    item_data={
                        "role": "assistant",
                        "agent": agent_name,
                        "content": [{"type": "output_text", "text": text}],
                    },
                    response_id=rid,
                )
            )
        for call_id, name, args_json in _extract_tool_calls(content):
            if seen_call_ids is not None:
                if call_id in seen_call_ids:
                    continue
                seen_call_ids.add(call_id)
            items.append(
                _MirrorItem(
                    rowid=rowid,
                    item_type="function_call",
                    item_data={
                        "agent": agent_name,
                        "name": name,
                        "arguments": args_json,
                        "call_id": call_id,
                    },
                    response_id=rid,
                )
            )
        return items
    if role == "tool":
        return [
            _MirrorItem(
                rowid=rowid,
                item_type="function_call_output",
                item_data={"call_id": call_id, "output": output},
                response_id=rid,
            )
            for call_id, output in _extract_tool_results(obj.get("content"))
        ]
    return []  # system or other scaffolding


async def _patch_external_session_id(
    client: httpx.AsyncClient, *, session_id: str, chat_id: str
) -> None:
    """PATCH the Omnigent session with the cursor chat id for cold-resume.

    Best-effort: logs on failure but does not raise so the forwarder loop
    continues mirroring even if the PATCH can't be delivered.
    """
    try:
        resp = await client.patch(
            f"/v1/sessions/{session_id}",
            json={"external_session_id": chat_id},
        )
        if resp.status_code >= 400:
            _logger.warning(
                "AP rejected external_session_id PATCH (%s); session=%s chat_id=%s",
                resp.status_code,
                session_id,
                chat_id,
            )
    except httpx.HTTPError:
        _logger.warning(
            "Transient error PATCHing external_session_id; session=%s — will not retry",
            session_id,
        )


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


async def _post_external_compaction_status(
    client: httpx.AsyncClient, *, session_id: str, status: str
) -> None:
    """POST one ``external_compaction_status`` event to the Sessions API.

    The server republishes this as the ``response.compaction.completed`` SSE the
    web UI already renders, upgrading the "Compacting conversation…" spinner
    (raised by the runner when it submitted ``/summarize``) to the permanent
    "Conversation compacted" marker. Posting it only when the summary blob
    actually appears is what makes the marker track cursor-agent's real
    progress instead of firing the instant the command was submitted. Mirrors
    :func:`omnigent.claude_native_forwarder._post_external_compaction_status`.

    :param client: Omnigent HTTP client.
    :param session_id: Omnigent session/conversation id.
    :param status: Compaction status value, e.g. ``"completed"``.
    :raises httpx.HTTPError: If the Omnigent request fails or is rejected.
    """
    resp = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "external_compaction_status",
            "data": {"status": status},
        },
    )
    resp.raise_for_status()


async def _persist_native_compaction_item(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    store_path: Path,
) -> None:
    """Persist a compaction boundary item to the conversation store."""
    resp = await client.get(
        f"/v1/sessions/{session_id}/items",
        params={"limit": 1, "order": "desc"},
    )
    resp.raise_for_status()
    items = resp.json().get("data", [])
    last_item_id = items[0]["id"] if items else f"compact_boundary_{session_id}"

    compacted_messages = None
    try:
        rows = _read_blob_rows(store_path, 0)
        msgs = []
        for _rowid, _blob_id, raw_data in rows:
            if isinstance(raw_data, (bytes, bytearray)):
                try:
                    raw_data = raw_data.decode("utf-8")
                except UnicodeDecodeError:
                    continue
            if not isinstance(raw_data, str):
                continue
            try:
                obj = json.loads(raw_data)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(obj, dict):
                continue
            role = obj.get("role")
            content = obj.get("content")
            if role in ("user", "assistant") and content:
                text = content if isinstance(content, str) else _content_text(content).strip()
                if text:
                    block_type = "input_text" if role == "user" else "output_text"
                    msgs.append(
                        {
                            "type": "message",
                            "role": role,
                            "content": [{"type": block_type, "text": text}],
                        }
                    )
        if msgs:
            compacted_messages = msgs
    except Exception:  # noqa: BLE001
        _logger.debug("Failed to read cursor store for compaction persist", exc_info=True)

    compaction_data = {
        "summary": "[Cursor compaction — context was compacted via /summarize]",
        "last_item_id": last_item_id,
        "model": "unknown",
        "token_count": 0,
    }
    if compacted_messages:
        compaction_data["compacted_messages"] = compacted_messages

    resp = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={"type": "compaction", "data": compaction_data},
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

    A failed item POST never silently re-posts forever: a server *rejection* (a
    4xx, or a 5xx such as a failed DB insert) is retried for up to
    ``_MAX_ITEM_POST_ATTEMPTS`` polls and then skipped so one poison item can't
    wedge the mirror — or flood the app — indefinitely; an *ambiguous* failure
    (request sent, response lost) is skipped at once since external items aren't
    deduped and a retry could duplicate the bubble; a *connection* failure
    (server unreachable) is retried indefinitely so an outage never drops the
    conversation.

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
    # Bounded-retry-then-skip guard (see the post loop below): the rowid whose
    # POST is currently being rejected and how many consecutive rejections it
    # has seen. Reset whenever the cursor advances past an item.
    failed_rowid = 0
    failed_attempts = 0
    # Track whether the cursor chat id has been persisted as external_session_id
    # so the cold-resume path can pass ``--resume <chatId>`` to cursor-agent.
    chat_id_patched = False
    model_state = _ModelMirrorState()
    # Per-turn grouping + live-card lifecycle. ``turn`` mints/stamps the shared
    # ``response_id``; ``running_posted_for`` is the turn id whose ``running``
    # status edge is already open, so the edge posts once per turn and a restart
    # mid-turn rejoins it without re-posting. Both are rebuilt from persisted
    # state so a supervisor restart resumes the open turn.
    turn = _TurnState()
    turn.response_id = persisted.turn_response_id
    running_posted_for: str | None = persisted.turn_response_id if persisted.turn_live else None

    def _turn_state(rowid: int) -> _ForwardState:
        """Build the persistable state for *rowid*, carrying the open-turn fields."""
        return _ForwardState(
            store_path=str(store_path),
            last_rowid=rowid,
            launch_epoch_ms=launch_epoch_ms,
            turn_response_id=turn.response_id,
            turn_live=running_posted_for is not None and running_posted_for == turn.response_id,
        )

    timeout = httpx.Timeout(_POST_TIMEOUT_S)
    async with httpx.AsyncClient(
        base_url=base_url, headers=headers, auth=auth, timeout=timeout
    ) as client:
        while True:
            try:
                # Whether this poll drained every owed item (running edge +
                # each chat item). A ``break`` out of the mirror loop leaves the
                # turn half-posted; the idle block below must not then consume
                # the turn-end marker, or the still-open running card never gets
                # its matching idle and spins forever.
                drained_cleanly = True
                if store_path is None or not store_path.exists():
                    # On cold resume the runner pre-seeds the bridge state with
                    # the known store path (see ``preseed_resume_state``), so we
                    # use it directly rather than running ``_discover_store`` whose
                    # launch-recency filter would miss a store created before this
                    # launch. For a normal fresh start the persisted path is absent
                    # (bridge state was cleared) and we fall through to discovery.
                    if persisted.store_path and Path(persisted.store_path).exists():
                        store_path = Path(persisted.store_path)
                        last_rowid = persisted.last_rowid
                        _write_state(bridge_dir, _turn_state(last_rowid))
                        persisted = _ForwardState()  # consumed
                        if not chat_id_patched:
                            chat_id_val = store_path.parent.name
                            await _patch_external_session_id(
                                client, session_id=session_id, chat_id=chat_id_val
                            )
                            chat_id_patched = True
                    else:
                        resolved = await asyncio.to_thread(
                            _discover_store, workspace, launch_epoch_ms
                        )
                        if resolved is not None and not await asyncio.to_thread(
                            _chat_claimed_by_other, bridge_dir, resolved, launch_epoch_ms
                        ):
                            store_path = resolved
                            if persisted.store_path == str(resolved):
                                last_rowid = persisted.last_rowid
                            else:
                                last_rowid = 0
                                # A fresh store (cold resume) is a new chat:
                                # reset the model dedupe so the new chat's
                                # current model is re-posted (server no-ops if
                                # unchanged), and drop any open-turn state left
                                # over from the prior store.
                                model_state = _ModelMirrorState()
                                turn.reset()
                                running_posted_for = None
                            _write_state(bridge_dir, _turn_state(last_rowid))
                            persisted = _ForwardState()  # consumed
                            # Persist the cursor chat id as external_session_id so
                            # a later cold resume can pass ``--resume <chatId>``
                            # to the cursor-agent TUI.
                            if not chat_id_patched:
                                chat_id_val = store_path.parent.name
                                await _patch_external_session_id(
                                    client, session_id=session_id, chat_id=chat_id_val
                                )
                                chat_id_patched = True
                if store_path is not None and store_path.exists():
                    # cursor keeps ONE chat per working dir, so two cursor-native
                    # sessions launched in the same cwd discover the same store.
                    # Yield to an earlier-launched live session rather than mirror
                    # the same chat into a second conversation (the duplicate-
                    # session bug); the released store is re-evaluated next poll.
                    if await asyncio.to_thread(
                        _chat_claimed_by_other, bridge_dir, store_path, launch_epoch_ms
                    ):
                        _logger.warning(
                            "cursor chat %s already mirrored by another session; "
                            "pausing mirror for session=%s",
                            store_path,
                            session_id,
                        )
                        store_path = None
                    else:
                        items = await asyncio.to_thread(
                            _read_new_items, store_path, last_rowid, agent_name, turn
                        )
                        for item in items:
                            if item.item_type == "compaction_completed":
                                # cursor finished /summarize: tell the web UI so
                                # its "Compacting…" spinner upgrades to the
                                # permanent marker. Best-effort — a failed post
                                # only leaves the spinner lingering; unlike a
                                # chat item it carries no content to lose, so we
                                # never retry-wedge the mirror on it. Advance the
                                # cursor either way. NOTE: this also swallows
                                # connection-level errors (a server blip at
                                # exactly the rollup blob loses the completion
                                # for good, since the cursor persists past it) —
                                # strictly less resilient than the chat path,
                                # which retries connection loss forever. Matches
                                # the claude forwarder's best-effort posture and
                                # never desyncs the mirror, so it is acceptable.
                                try:
                                    await _post_external_compaction_status(
                                        client, session_id=session_id, status="completed"
                                    )
                                except httpx.HTTPError:
                                    _logger.warning(
                                        "cursor forwarder could not post "
                                        "compaction-completed; the web UI spinner "
                                        "may linger; session=%s rowid=%s",
                                        session_id,
                                        item.rowid,
                                        exc_info=True,
                                    )
                                try:
                                    await _persist_native_compaction_item(
                                        client,
                                        session_id=session_id,
                                        store_path=store_path,
                                    )
                                except Exception:  # noqa: BLE001
                                    _logger.warning(
                                        "cursor forwarder could not persist "
                                        "compaction item; session=%s",
                                        session_id,
                                        exc_info=True,
                                    )
                                failed_rowid = failed_attempts = 0
                                last_rowid = item.rowid
                                _write_state(bridge_dir, _turn_state(last_rowid))
                                continue
                            if item.item_type:
                                # Open the turn's live card once, before its first
                                # mirrored item: a ``running`` status edge carrying
                                # the turn id makes the web UI stream that turn's
                                # tool cards live (spinner + timer) instead of
                                # rendering them already-completed. Only turn items
                                # (assistant/tool) carry the ``cursor:turn:`` id; the
                                # user bubble and compaction signal do not.
                                if (
                                    item.response_id.startswith(_TURN_ID_PREFIX)
                                    and item.response_id != running_posted_for
                                ):
                                    try:
                                        await post_external_session_status(
                                            client,
                                            session_id=session_id,
                                            status="running",
                                            response_id=item.response_id,
                                        )
                                        running_posted_for = item.response_id
                                    except httpx.HTTPError:
                                        # Best-effort: retry the whole item (and its
                                        # running edge) next poll rather than mirror
                                        # the card without the edge that makes it live.
                                        _logger.warning(
                                            "cursor forwarder could not post running "
                                            "edge; retrying; session=%s rowid=%s",
                                            session_id,
                                            item.rowid,
                                            exc_info=True,
                                        )
                                        drained_cleanly = False
                                        break
                                try:
                                    await _post_conversation_item(
                                        client, session_id=session_id, item=item
                                    )
                                except httpx.HTTPError as exc:
                                    if post_may_have_been_delivered(exc):
                                        # Ambiguous: the request was sent but its
                                        # response was lost, so the server may have
                                        # already committed this item. External
                                        # items aren't deduped, so re-posting would
                                        # duplicate the web bubble — skip past it.
                                        _logger.warning(
                                            "cursor forwarder skipping item after an "
                                            "ambiguous POST failure (may already be "
                                            "committed); session=%s rowid=%s",
                                            session_id,
                                            item.rowid,
                                            exc_info=True,
                                        )
                                    elif isinstance(exc, httpx.HTTPStatusError):
                                        # The server received and rejected the item
                                        # (a 4xx, or a 5xx like the response_id
                                        # truncation that wedged the mirror). Retry
                                        # a bounded number of polls, then skip so one
                                        # poison item can't wedge the mirror — and
                                        # flood the app — forever.
                                        if item.rowid != failed_rowid:
                                            failed_rowid, failed_attempts = item.rowid, 0
                                        failed_attempts += 1
                                        if failed_attempts < _MAX_ITEM_POST_ATTEMPTS:
                                            _logger.warning(
                                                "cursor forwarder POST rejected (HTTP "
                                                "%s); retrying; session=%s rowid=%s "
                                                "attempt=%s",
                                                exc.response.status_code,
                                                session_id,
                                                item.rowid,
                                                failed_attempts,
                                            )
                                            drained_cleanly = False
                                            break  # retry this item before any after it
                                        _logger.error(
                                            "cursor forwarder dropping item after %s "
                                            "rejected POSTs (HTTP %s); mirror would "
                                            "otherwise wedge; session=%s rowid=%s",
                                            failed_attempts,
                                            exc.response.status_code,
                                            session_id,
                                            item.rowid,
                                        )
                                    else:
                                        # Connection-level failure: the server is
                                        # unreachable, which is not this item's
                                        # fault. Retry indefinitely so an outage
                                        # never drops the conversation; the poll
                                        # cadence and supervisor ride it out.
                                        _logger.warning(
                                            "cursor forwarder POST could not reach the "
                                            "server; retrying; session=%s rowid=%s",
                                            session_id,
                                            item.rowid,
                                            exc_info=True,
                                        )
                                        drained_cleanly = False
                                        break
                            # Reached on a successful post, an ambiguous-delivery
                            # skip, a quarantine, or a non-posted sentinel row:
                            # advance past this item and reset the failure counter.
                            failed_rowid = failed_attempts = 0
                            last_rowid = item.rowid
                            _write_state(bridge_dir, _turn_state(last_rowid))
                        # Refresh the claim heartbeat every poll (even with no new
                        # items) so an idle owner keeps its claim and a peer can
                        # detect a dead session.
                        _write_state(bridge_dir, _turn_state(last_rowid))
                        # Mirror a terminal-side model switch (TUI ``/model``)
                        # back to the web picker. Polled alongside messages so
                        # the pill tracks the same cadence as the chat view.
                        observed_model = await asyncio.to_thread(_read_last_used_model, store_path)
                        await _post_model_change_if_new(
                            client,
                            session_id=session_id,
                            state=model_state,
                            model=observed_model,
                        )
                # Turn over the cursor ``stop`` hook's turn-completion markers to
                # an ``external_session_status: idle`` edge — the signal that wakes
                # a parent orchestrator (the PTY watcher's spinner status never
                # does). This block sits at the poll-loop body level, deliberately
                # OUTSIDE the ``if store_path`` mirroring branch above: the stop
                # hook writes turn-end markers independently of the SQLite store,
                # so a turn-end is never missed even on a poll where the store is
                # unbound or empty. Deduped against a persisted posted-count so a
                # supervisor restart never re-wakes the parent for a turn it
                # already reported. Best-effort: a failed post leaves the count
                # unadvanced so the next poll retries.
                total_turn_ends = await asyncio.to_thread(
                    cursor_native_status.count_turn_ends, bridge_dir
                )
                if drained_cleanly and total_turn_ends > await asyncio.to_thread(
                    cursor_native_status.read_posted_count, bridge_dir
                ):
                    # Close the open turn's live card with the SAME id its running
                    # edge carried, so the web UI settles that turn's tool cards
                    # (spinner → done) rather than leaving them spinning; a bare
                    # turn-agnostic edge (no open turn) still wakes the parent inbox.
                    await post_external_session_status(
                        client,
                        session_id=session_id,
                        status="idle",
                        response_id=running_posted_for,
                    )
                    await asyncio.to_thread(
                        cursor_native_status.write_posted_count, bridge_dir, total_turn_ends
                    )
                    running_posted_for = None
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
