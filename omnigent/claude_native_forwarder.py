"""Background transcript forwarding for native Claude Code sessions."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from omnigent._native_post_delivery import post_may_have_been_delivered
from omnigent.claude_native_bridge import (
    BRIDGE_ID_LABEL_KEY,
    ClaudeHookRecord,
    ClaudeMessageDelta,
    ClaudeTranscriptItem,
    HookReadResult,
    TranscriptReadResult,
    compute_transcript_cumulative_cost,
    read_active_session_id,
    read_bridge_id,
    read_claude_context_state,
    read_claude_session_id,
    read_hook_events_from_offset,
    read_hook_events_since_with_position,
    read_message_deltas_from_offset,
    read_transcript_items_from_offset,
    read_transcript_items_since_with_position,
    read_transcript_path,
    transcript_has_forked_from_marker,
    transcript_has_recent_local_command,
    url_component,
    write_active_session_id,
)
from omnigent.claude_native_message_display_hook import MESSAGE_DELTAS_FILE
from omnigent.entities.session_resources import terminal_resource_id
from omnigent.reasoning_effort import CLAUDE_EFFORTS, EFFORT_CLEAR_VALUES

_FORWARDER_STATE_FILE = "transcript_forwarder.json"
_HOOK_STATE_FILE = "hook_forwarder.json"
_SUBAGENT_STATE_FILE = "subagent_forwarder.json"
_DELTA_STATE_FILE = "message_deltas_forwarder.json"
_HOOKS_FILE = "hooks.jsonl"

# Cap on the in-memory ``(message_id, index)`` dedupe ring for streamed
# deltas. The byte offset already prevents re-reading on the normal
# path; this guards the rare truncation/rewind case where the deltas
# file is reset and the reader restarts from 0. Generous because one
# prose answer can be hundreds of chunks.
_MAX_SEEN_DELTA_KEYS = 5000

# Max time an assistant ``message`` item is held waiting for its
# streamed deltas to forward first. The transcript and deltas file have
# independent writers, so a short reply's record can hit disk a poll
# BEFORE its deltas — inverting the deltas-before-done order and
# rendering the message twice. ~8 polls at 0.25s: well past the one-poll
# race, short enough that an unmatched item (dropped deltas, or a
# multi-block message that never byte-equals the whole-message stream)
# posts with barely noticeable delay — and has no preview to duplicate.
_ASSISTANT_ITEM_DELTA_HOLD_S = 2.0

# Cap on the delta-ordering bookkeeping. Entries are consumed on match /
# never revisited after post, so this is a backstop against pathological
# sessions, not a working-set size.
_MAX_DELTA_ORDERING_ENTRIES = 256


@dataclass
class _ForwardedDeltaText:
    """
    Forwarded streamed-text accumulation for one assistant message.

    :param parts: Forwarded delta strings in arrival order, e.g.
        ``["Hello ", "world"]``.
    :param final: Whether the ``final: true`` chunk has forwarded — only
        then is ``"".join(parts)`` the complete text, safe to byte-compare
        against a transcript item.
    """

    parts: list[str] = field(default_factory=list)
    final: bool = False


@dataclass
class _DeltaOrderingState:
    """
    Cross-poll state enforcing deltas-before-done item ordering.

    Filled by :func:`_forward_available_deltas` (forwarded chunk text per
    ``message_id``) and consumed by :func:`_hold_assistant_item_for_deltas`,
    which matches an assistant ``message`` item to its forwarded stream by
    byte-equal text (the transcript carries no ``message_id``).

    :param texts: ``message_id`` → forwarded delta text state. Popped
        when an item matches it.
    :param held_since: ``source_id`` → monotonic time first held. Kept
        after the timeout releases the item so a failed post's retry
        isn't re-held; bounded.
    """

    texts: dict[str, _ForwardedDeltaText] = field(default_factory=dict)
    held_since: dict[str, float] = field(default_factory=dict)


def _hold_monotonic() -> float:
    """
    Monotonic clock for the assistant-item hold timeout.

    Indirection so tests patch THIS, not the process-global
    ``time.monotonic`` (see the no-global-singleton-patch test rule).

    :returns: Seconds from an unspecified monotonic epoch.
    """
    return time.monotonic()


def _item_output_text(data: dict[str, Any]) -> str | None:
    """
    Join the ``output_text`` blocks of a message item's content.

    :param data: Item payload, e.g. ``{"role": "assistant", "content":
        [{"type": "output_text", "text": "Hi"}]}``.
    :returns: The joined text, or ``None`` when the item carries none.
    """
    content = data.get("content")
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "output_text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    if not parts:
        return None
    return "".join(parts)


def _hold_assistant_item_for_deltas(
    item: ClaudeTranscriptItem,
    ordering: _DeltaOrderingState | None,
    bridge_dir: Path,
) -> bool:
    """
    Decide whether to defer an assistant message item to a later poll.

    Enforces deltas-before-done: an assistant ``message`` item posts only
    once a complete (``final``-seen) forwarded stream byte-equals its
    text, or after :data:`_ASSISTANT_ITEM_DELTA_HOLD_S`. Holding returns
    ``True`` and the caller stops the batch here (cursor unadvanced) so
    later items can't overtake it. Items that can't have a preview — tool
    calls, user/text-less messages, no-deltas-file sessions — never hold.
    The timeout is safe: a message whose deltas never arrive has no live
    preview, so a late post renders once, like any non-streamed message.

    :param item: The transcript item about to be posted.
    :param ordering: Shared ordering state, or ``None`` to disable
        holding (parsing-only test paths).
    :param bridge_dir: Native Claude bridge directory (for the
        deltas-file existence check).
    :returns: ``True`` to hold the item (and the rest of the batch)
        until the next poll; ``False`` to post it now.
    """
    if ordering is None:
        return False
    if item.item_type != "message" or item.data.get("role") != "assistant":
        return False
    text = _item_output_text(item.data)
    if not text:
        return False
    if not (bridge_dir / MESSAGE_DELTAS_FILE).exists():
        return False
    for message_id, entry in ordering.texts.items():
        if entry.final and "".join(entry.parts) == text:
            # Deltas fully forwarded — consume the stream (a later
            # identical-text message matches its own) and post.
            ordering.texts.pop(message_id)
            ordering.held_since.pop(item.source_id, None)
            return False
    now = _hold_monotonic()
    first_held = ordering.held_since.setdefault(item.source_id, now)
    while len(ordering.held_since) > _MAX_DELTA_ORDERING_ENTRIES:
        del ordering.held_since[next(iter(ordering.held_since))]
    if now - first_held >= _ASSISTANT_ITEM_DELTA_HOLD_S:
        # Timestamp kept: a failed post's retry next poll is released
        # immediately by the elapsed check, not re-held for a full timeout.
        _logger.debug(
            "Posting assistant transcript item without matching forwarded "
            "deltas after %.1fs hold; source_id=%s",
            _ASSISTANT_ITEM_DELTA_HOLD_S,
            item.source_id,
        )
        return False
    return True


# Seconds of transcript inactivity after which we publish ``idle`` for
# a sub-agent. The transcript is the only signal we have for sub-agent
# completion in Phase A (no SubagentStop hook is subscribed); 5s is the
# shortest window that comfortably absorbs a stalled tool call without
# flickering the badge. Phase B will replace this with an authoritative
# hook signal and drop the heuristic.
_SUBAGENT_IDLE_QUIESCENCE_S = 5.0

# Meta-file glob inside ``~/.claude/projects/<encoded>/<session>/subagents/``.
# One per Claude Task-tool subagent; appears alongside the matching
# ``agent-<id>.jsonl`` transcript.
_SUBAGENT_META_GLOB = "agent-*.meta.json"
_DEFAULT_POLL_INTERVAL_S = 0.25
_POST_TIMEOUT_S = 10.0
_MAX_SEEN_SOURCE_IDS = 2000
_CURSOR_FINGERPRINT_BYTES = 256
_FORK_COMMAND_NAMES = frozenset({"/branch", "/fork"})
_HTTP_POST_MAX_PERMANENT_FAILURES = 3
_HTTP_POST_RETRY_BASE_DELAY_S = 1.0
_HTTP_POST_RETRY_MAX_DELAY_S = 30.0
_HTTP_TRANSIENT_STATUS_CODES = {408, 409, 425, 429}
# A 503 ``subagent_delivery_not_confirmed`` means the runner could not deliver a
# terminal sub-agent result to the parent inbox. It is retried (the work entry can
# be created slightly after the child reports terminal — a short dispatch race), but
# UNLIKE a generic 5xx it must NOT retry forever: when the parent host is gone the
# condition is permanent. Bounded so a single orphaned sub-agent cannot flood the
# shared server. The budget spans the backoff schedule (capped at 30 s) ⇒ a few
# minutes, comfortably covering the dispatch race.
_SUBAGENT_DELIVERY_NOT_CONFIRMED_MAX_ATTEMPTS = 12
_SUPERVISOR_INITIAL_BACKOFF_S = 1.0
_SUPERVISOR_MAX_BACKOFF_S = 30.0
_SUPERVISOR_HEALTHY_UPTIME_S = 60.0

# Claude Code hook event names → Omnigent session-status values
# published on the per-conversation SSE stream. Unmapped events emit
# no status.
#
# ``Stop`` → idle and ``StopFailure`` → failed are the authoritative
# turn-end edges (each fires once when Claude finishes / errors a turn);
# they drive sub-agent terminal delivery via the codex-shared
# ``external_session_status`` path (→ parent inbox + wake). The
# PTY-activity ``idle`` cannot: it is a ~1s-quiescence heuristic that
# oscillates on every mid-turn lull, so delivering on it fired a
# premature completion and idempotently locked out the real one.
# ``UserPromptSubmit`` → running stays PTY-derived — the pane watcher
# drives the UI running/idle badge and catches what ``Stop`` misses
# (interrupts, compaction failures, TUI edits). ``_publish_status``
# keeps ``failed`` sticky against the trailing PTY idle.
_HOOK_EVENT_TO_STATUS: dict[str, str] = {
    "Stop": "idle",
    "StopFailure": "failed",
}

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HookForwardState:
    """
    Durable cursor for the hooks-to-status forwarder.

    :param event_cursor: One-based hook record index already
        forwarded. ``0`` means no hook events have been forwarded yet.
    :param byte_offset: Byte offset already forwarded. ``None`` means
        the state was written by an older line-cursor-only forwarder
        and must be migrated with one compatibility scan.
    :param cursor_fingerprint: Hash of bytes immediately before
        ``byte_offset``. Used to detect truncation/replacement before
        seeking into a stale offset.
    """

    event_cursor: int
    byte_offset: int | None = None
    cursor_fingerprint: str | None = None


@dataclass(frozen=True)
class SubagentEntry:
    """
    Per-sub-agent forwarder cursor.

    One of these per Claude-side sub-agent. Tracks the Omnigent child
    Conversation id we minted (so subsequent items POST to the
    right session), the transcript file byte offset already
    forwarded, and the wall-clock timestamp of the last item we
    saw (for the idle-status heuristic).

    :param subagent_id: Stable Claude-side identifier, also the
        ``agent-<id>`` filename stem, e.g. ``"a5c7effac5a9a35ab"``.
    :param child_conversation_id: Omnigent child Conversation id minted
        by the server's ``external_subagent_start`` handler,
        e.g. ``"conv_child456"``.
    :param byte_offset: Bytes already forwarded from the sub-agent's
        ``.jsonl``. ``0`` means we haven't read anything yet (the
        common case when the sub-agent has just been created).
    :param seen_source_ids: Recently-posted transcript item source
        ids for this child. Preserved separately from ``byte_offset``
        so a failed later item can leave the cursor behind without
        re-posting earlier accepted items on the next poll.
    :param last_activity_ts: Unix timestamp of the most recent item
        observed in this sub-agent's transcript. Used by the idle
        heuristic — when ``now - last_activity_ts >
        _SUBAGENT_IDLE_QUIESCENCE_S`` we publish an
        ``external_session_status: idle`` event. ``None`` when no
        items have been seen yet (so the heuristic doesn't fire
        before there's anything to be quiescent about).
    :param last_status: Last status string POSTed for this
        sub-agent — used to dedupe so we don't spam ``running`` or
        ``idle`` events on every tick when nothing changed. ``None``
        means no status has been posted yet.
    """

    subagent_id: str
    child_conversation_id: str
    byte_offset: int = 0
    seen_source_ids: tuple[str, ...] = ()
    last_activity_ts: float | None = None
    last_status: str | None = None


@dataclass(frozen=True)
class SubagentForwardState:
    """
    Durable cursor map for the claude-native sub-agent forwarder.

    Persisted at ``{bridge_dir}/subagent_forwarder.json`` so a
    forwarder restart picks up where we left off — re-reading the
    on-disk ``subagents/`` directory and posting only items past
    each tracked sub-agent's ``byte_offset``.

    :param subagents: Map from Claude-side ``subagent_id`` to the
        per-sub-agent entry. New sub-agents discovered on disk are
        inserted here after the Omnigent server returns a child
        Conversation id.
    """

    subagents: dict[str, SubagentEntry]


@dataclass(frozen=True)
class TranscriptForwardState:
    """
    Durable cursor for a Claude transcript forwarder.

    :param transcript_path: Transcript JSONL file whose cursor was
        recorded.
    :param line_cursor: One-based line cursor already forwarded into
        AP. ``0`` means no lines from the current transcript have
        been forwarded yet.
    :param byte_offset: Transcript byte offset already forwarded.
        ``None`` means the state was written by an older
        line-cursor-only forwarder and must be migrated with one
        compatibility scan.
    :param current_response_id: Response id for a Claude assistant
        turn that spans multiple forwarder polls.
    :param seen_source_ids: Recently-posted transcript item source
        ids. This makes retries and restarts idempotent even if the
        line cursor was not advanced before a cancellation.
    :param cursor_fingerprint: Hash of bytes immediately before
        ``byte_offset``. Used to detect truncation/replacement before
        seeking into a stale offset.
    """

    transcript_path: Path
    line_cursor: int
    byte_offset: int | None = None
    current_response_id: str | None = None
    seen_source_ids: tuple[str, ...] = ()
    cursor_fingerprint: str | None = None


@dataclass(frozen=True)
class DeltaForwardState:
    """
    Durable cursor for the assistant-text delta forwarder.

    Tracks the byte offset already consumed from
    ``<bridge_dir>/message_deltas.jsonl``. Unlike the transcript cursor
    this is NOT tied to a transcript path and is NOT reset on
    ``/clear`` / ``/fork``: the deltas file belongs to the long-lived
    Claude process and keeps growing across Omnigent session rotations, so the
    offset stays monotonic and each new chunk is forwarded to whatever
    Omnigent session is active when it is read.

    :param byte_offset: Byte offset after the last forwarded chunk.
        ``0`` means nothing has been forwarded yet.
    """

    byte_offset: int = 0


@dataclass
class _ForwardDedupeState:
    """
    Last values the forwarder POSTed, kept to suppress duplicate
    ``external_*`` events when Claude rewrites the same block each poll.

    Mutated in place by :func:`_forward_available_items` so the run loop
    carries the dedupe baseline across polls without threading a
    positional tuple back out. Reset on ``/clear`` and ``/fork``
    rotations alongside the other per-session state.

    :param usage: Last ``message.usage`` snapshot POSTed via
        ``external_session_usage``, or ``None`` if none yet.
    :param context_window: Last context-window POSTed, or ``None``.
    :param observed_model: Last tier alias seen in the transcript,
        sticky across polls (the incremental window often carries no
        fresh ``message.model``), e.g. ``"opus"``. ``None`` until first
        seen.
    :param posted_model: Last tier alias POSTed via
        ``external_model_change``. Seeded from the first observation
        WITHOUT a POST so a passive spawn default never overwrites a
        pending silent model handoff; only a later in-TUI switch is
        mirrored. Left behind ``observed_model`` on a failed POST so the
        next poll retries. ``None`` until the first observation.
    :param posted_cost: Last DISPLAY cost (USD) POSTed as
        ``cumulative_cost_usd`` — the statusLine total ``S`` verbatim.
        ``None`` until the first cost post. Used to dedupe so a steady
        cost isn't re-POSTed every poll.
    :param posted_policy_cost: Last POLICY/budget cost (USD) POSTed as
        ``policy_cost_usd`` — ``max(S, transcript estimate)``, the
        real-time figure the cost-budget gate reads. Tracked separately
        from ``posted_cost`` because it advances mid-turn (with in-flight
        sub-agent spend) while ``S`` stays frozen. ``None`` until first
        post.
    """

    usage: dict[str, float] | None = None
    context_window: int | None = None
    observed_model: str | None = None
    posted_model: str | None = None
    # Last DISPLAY cost (USD) POSTed as ``cumulative_cost_usd`` — the
    # statusLine total ``S`` verbatim (matches /cost in the Claude TUI).
    # Kept to suppress duplicate posts when S hasn't advanced.
    posted_cost: float | None = None
    # Last POLICY/budget cost (USD) POSTed as ``policy_cost_usd`` —
    # ``max(S, forwarder transcript estimate)``, which reflects in-flight
    # sub-agent spend so the gate can block mid-turn. Separate baseline
    # because it can advance while ``posted_cost`` (S) is frozen.
    posted_policy_cost: float | None = None


@dataclass(frozen=True)
class _TranscriptCostCacheEntry:
    """
    Cached cumulative-cost computation for one transcript file.

    The cost is recomputed only when the file's byte size changes, so the
    forwarder doesn't re-parse an unchanged transcript on every (0.25s)
    poll. Append-only JSONL makes byte size a sound cache key.

    :param size: File size in bytes when ``cost_usd`` was computed,
        e.g. ``81920``.
    :param cost_usd: Cumulative USD cost computed from the transcript at
        that size, or ``None`` when nothing could be priced.
    """

    size: int
    cost_usd: float | None


@dataclass
class _PostRetryEntry:
    """
    In-memory retry state for one outbound Omnigent event.

    :param attempts: Number of failed post attempts observed.
    :param next_attempt_at: Monotonic timestamp before which the
        forwarder should not retry this event.
    """

    attempts: int = 0
    next_attempt_at: float = 0.0


@dataclass(frozen=True)
class _PostRetryDecision:
    """
    Result of recording one outbound Omnigent post failure.

    :param attempts: Number of failed attempts for this event after
        the current failure.
    :param delay_s: Seconds until the next retry should be attempted.
    :param exhausted: Whether a permanent failure exceeded the retry
        budget and the cursor should advance past the event.
    :param permanent: Whether the failure is classified as a
        permanent HTTP rejection.
    """

    attempts: int
    delay_s: float
    exhausted: bool
    permanent: bool


class _PostRetryTracker:
    """
    Track bounded retries and backoff for Omnigent event posts.

    Permanent 4xx-style HTTP rejections are retried a small number of
    times before the forwarder marks the item failed and advances the
    cursor. Transient HTTP/network failures keep retrying with
    backoff so Omnigent outages do not silently drop transcript data.

    This is not a :mod:`tenacity` wrapper because retry attempts must
    be interleaved with durable cursor writes and the forwarder's poll
    loop. Sleeping inside a decorator would block unrelated hook/item
    work behind one poisoned event.
    """

    def __init__(
        self,
        *,
        max_permanent_attempts: int = _HTTP_POST_MAX_PERMANENT_FAILURES,
        max_not_confirmed_attempts: int = _SUBAGENT_DELIVERY_NOT_CONFIRMED_MAX_ATTEMPTS,
        base_delay_s: float = _HTTP_POST_RETRY_BASE_DELAY_S,
        max_delay_s: float = _HTTP_POST_RETRY_MAX_DELAY_S,
    ) -> None:
        """
        Initialize an empty retry tracker.

        :param max_permanent_attempts: Attempts before a permanent
            failure is exhausted.
        :param max_not_confirmed_attempts: Attempts before a
            ``subagent_delivery_not_confirmed`` 503 is exhausted.
        :param base_delay_s: Initial retry delay in seconds.
        :param max_delay_s: Maximum retry delay in seconds.
        :returns: None.
        """
        self._max_permanent_attempts = max(1, max_permanent_attempts)
        self._max_not_confirmed_attempts = max(1, max_not_confirmed_attempts)
        self._base_delay_s = max(0.0, base_delay_s)
        self._max_delay_s = max(0.0, max_delay_s)
        self._entries: dict[str, _PostRetryEntry] = {}

    def retry_delay_s(self, key: str) -> float | None:
        """
        Return remaining delay for ``key`` if a retry is not due yet.

        :param key: Stable retry key, e.g. ``"item:source-1"``.
        :returns: Remaining seconds to wait, or ``None`` when the
            caller may attempt the post now.
        """
        entry = self._entries.get(key)
        if entry is None:
            return None
        remaining = entry.next_attempt_at - time.monotonic()
        if remaining <= 0:
            return None
        return remaining

    def clear(self, key: str) -> None:
        """
        Remove retry state for a successfully handled event.

        :param key: Stable retry key, e.g. ``"hook:2:idle"``.
        :returns: None.
        """
        self._entries.pop(key, None)

    def record_failure(self, key: str, exc: httpx.HTTPError) -> _PostRetryDecision:
        """
        Record one failed post and compute the next retry action.

        :param key: Stable retry key, e.g. ``"item:source-1"``.
        :param exc: HTTP exception raised while posting the event.
        :returns: Retry decision for this failure.
        """
        entry = self._entries.get(key)
        if entry is None:
            entry = _PostRetryEntry()
            self._entries[key] = entry
        entry.attempts += 1
        permanent = _is_permanent_http_error(exc)
        not_confirmed = _is_subagent_delivery_not_confirmed(exc)
        give_up = (
            (permanent and entry.attempts >= self._max_permanent_attempts)
            or (not_confirmed and entry.attempts >= self._max_not_confirmed_attempts)
        )
        if give_up:
            self._entries.pop(key, None)
            return _PostRetryDecision(
                attempts=entry.attempts,
                delay_s=0.0,
                exhausted=True,
                permanent=permanent,
            )
        delay_s = min(
            self._base_delay_s * (2 ** max(0, entry.attempts - 1)),
            self._max_delay_s,
        )
        entry.next_attempt_at = time.monotonic() + delay_s
        return _PostRetryDecision(
            attempts=entry.attempts,
            delay_s=delay_s,
            exhausted=False,
            permanent=permanent,
        )


async def forward_claude_transcript_to_session(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    bridge_dir: Path,
    agent_name: str,
    start_at_end: bool,
    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
    auth: httpx.Auth | None = None,
    skip_user_messages: bool = False,
) -> None:
    """
    Tail Claude's JSONL transcript and mirror semantic items into AP.

    This loop is intentionally independent of Claude Channels. It
    runs while the native terminal is attached, watches the transcript
    path reported by Claude hooks, and posts new user text,
    assistant text, tool calls, and tool results as external AP
    conversation items.

    :param base_url: Omnigent server base URL.
    :param headers: Static HTTP headers for Omnigent requests. Authorization
        is normally supplied via ``auth`` instead so OAuth tokens are
        refreshed per request; any ``Authorization`` value here is
        overridden by ``auth`` when both are set.
    :param session_id: Omnigent session/conversation id.
    :param bridge_dir: Native Claude bridge directory.
    :param agent_name: Agent/model name to stamp on mirrored output.
    :param start_at_end: When ``True`` and no prior forward cursor
        exists, start from the current transcript end. This is used
        for reattach so old transcript lines are not duplicated.
    :param poll_interval_s: Seconds between transcript polls.
    :param auth: Optional httpx Auth that mints a fresh bearer token
        per request, e.g. ``_server_auth(profile)`` for a Databricks
        Apps deployment. ``None`` for local servers that don't need
        auth. Required for long-lived remote sessions — Databricks
        OAuth tokens expire after ~1 hour and a static header captured
        at startup would stop authenticating mid-session.
    :returns: Never normally returns; cancel the task to stop it.
    """
    state = _read_forward_state(bridge_dir)
    hook_state: HookForwardState | None = None
    subagent_state = _read_subagent_forward_state(bridge_dir)
    # Live assistant-text streaming. The delta cursor is independent of
    # the transcript/subagent cursors and survives /clear and /fork
    # (the deltas file belongs to the long-lived Claude process). The
    # dedupe ring is per-process and not persisted: the byte offset
    # prevents re-reads on the normal path.
    delta_state = _read_delta_forward_state(bridge_dir)
    seen_delta_keys: dict[tuple[str, int], None] = {}
    # Deltas-before-done ordering across the two independent tails: the
    # deltas forwarder records each message's forwarded text, the items
    # forwarder holds an assistant item until its text matches a complete
    # forwarded stream (or a short timeout). Per-process like
    # ``seen_delta_keys``; survives /clear and /fork (message_ids belong
    # to the long-lived Claude process).
    delta_ordering = _DeltaOrderingState()
    item_retries = _PostRetryTracker()
    status_retries = _PostRetryTracker()
    subagent_start_retries = _PostRetryTracker()
    subagent_item_retries = _PostRetryTracker()
    subagent_status_retries = _PostRetryTracker()
    # Dedupe: Claude rewrites the same usage block every poll until
    # the next assistant entry; only POST on real change. Mutated in
    # place by ``_forward_available_items`` and carried across polls.
    dedupe = _ForwardDedupeState()
    # Size-keyed transcript cost cache for ``_forward_session_cost`` — keeps
    # the per-poll cost reconciliation from re-parsing unchanged transcripts.
    # Reset on /clear and /fork rotations alongside ``dedupe``.
    cost_cache: dict[Path, _TranscriptCostCacheEntry] = {}
    # Per-process latch: once we PATCH the conversation with the
    # Claude-native session id, never PATCH again. Persists for the
    # lifetime of the forwarder task; the server's idempotence handles
    # the rare case where two forwarder processes race the same conv.
    external_session_id_mirrored = False
    # Native task system state: maps and ordered list accumulated from
    # TaskCreated / TaskCompleted / PostToolUse/TaskUpdate hook events.
    # Reset on /clear and /fork rotations alongside other session state.
    task_subjects: dict[str, str] = {}
    task_statuses: dict[str, str] = {}
    task_order: list[str] = []
    timeout = httpx.Timeout(_POST_TIMEOUT_S)
    async with httpx.AsyncClient(
        base_url=base_url, headers=headers, auth=auth, timeout=timeout
    ) as client:
        while True:
            try:
                current_session_id = read_active_session_id(bridge_dir) or session_id
                if hook_state is None:
                    hook_state = await _ensure_hook_state(
                        bridge_dir,
                        start_at_end=start_at_end,
                        session_id=current_session_id,
                    )
                rotation = await _maybe_rotate_session_on_clear(
                    client=client,
                    session_id=current_session_id,
                    bridge_dir=bridge_dir,
                    state=hook_state,
                )
                if rotation is not None:
                    # Tell the superseded (old) conversation it was cleared:
                    # persist a notice linking to the rotated-to session and
                    # emit a live redirect event. Use the loop's ``session_id``
                    # (the session being forwarded BEFORE this poll), NOT
                    # ``current_session_id``: when the hook rotated the bridge's
                    # active session synchronously, ``current_session_id`` already
                    # reads the NEW id, whereas ``session_id`` is not reassigned
                    # to ``rotation`` until below. The call is fully best-effort
                    # (swallows its own errors) so the state reset below always
                    # runs.
                    await _post_clear_supersession(
                        client,
                        old_session_id=session_id,
                        new_session_id=rotation,
                        agent_name=agent_name,
                    )
                    session_id = rotation
                    state = None
                    hook_state = None
                    # After a /clear or /fork the parent now resolves
                    # to a new ``<session_uuid>/subagents/`` directory
                    # on disk, so old sub-agent entries are dead. Drop
                    # them; the watcher will rediscover any new ones
                    # under the rotated session's dir.
                    subagent_state = SubagentForwardState(subagents={})
                    await _write_subagent_forward_state_async(bridge_dir, subagent_state)
                    item_retries = _PostRetryTracker()
                    status_retries = _PostRetryTracker()
                    subagent_start_retries = _PostRetryTracker()
                    subagent_item_retries = _PostRetryTracker()
                    subagent_status_retries = _PostRetryTracker()
                    external_session_id_mirrored = False
                    task_subjects = {}
                    task_statuses = {}
                    task_order = []
                    # A rotated session is a fresh dedupe context — reseed
                    # so the new session's first model observation doesn't
                    # post against the prior session's baseline.
                    dedupe = _ForwardDedupeState()
                    # The rotated session resolves to a new transcript +
                    # subagents/ dir, so prior cost entries are dead; drop
                    # them so cost is recomputed fresh for the new session.
                    cost_cache = {}
                    await asyncio.sleep(poll_interval_s)
                    continue
                rotation = await _maybe_rotate_session_on_fork(
                    client=client,
                    session_id=current_session_id,
                    bridge_dir=bridge_dir,
                    state=hook_state,
                )
                if rotation is not None:
                    session_id = rotation
                    state = None
                    hook_state = None
                    # After a /clear or /fork the parent now resolves
                    # to a new ``<session_uuid>/subagents/`` directory
                    # on disk, so old sub-agent entries are dead. Drop
                    # them; the watcher will rediscover any new ones
                    # under the rotated session's dir.
                    subagent_state = SubagentForwardState(subagents={})
                    await _write_subagent_forward_state_async(bridge_dir, subagent_state)
                    item_retries = _PostRetryTracker()
                    status_retries = _PostRetryTracker()
                    subagent_start_retries = _PostRetryTracker()
                    subagent_item_retries = _PostRetryTracker()
                    subagent_status_retries = _PostRetryTracker()
                    external_session_id_mirrored = False
                    task_subjects = {}
                    task_statuses = {}
                    task_order = []
                    # A rotated session is a fresh dedupe context — reseed
                    # so the new session's first model observation doesn't
                    # post against the prior session's baseline.
                    dedupe = _ForwardDedupeState()
                    # The rotated session resolves to a new transcript +
                    # subagents/ dir, so prior cost entries are dead; drop
                    # them so cost is recomputed fresh for the new session.
                    cost_cache = {}
                    await asyncio.sleep(poll_interval_s)
                    continue
                if not external_session_id_mirrored:
                    external_session_id_mirrored = await _maybe_mirror_external_session_id(
                        client=client,
                        session_id=current_session_id,
                        bridge_dir=bridge_dir,
                    )
                transcript_path = read_transcript_path(bridge_dir)
                if transcript_path is not None:
                    state = await _ensure_state_for_transcript(
                        bridge_dir=bridge_dir,
                        state=state,
                        transcript_path=transcript_path,
                        start_at_end=start_at_end,
                        session_id=current_session_id,
                    )
                    # Forward streamed deltas BEFORE the transcript items so a
                    # message's live chunks (incl. its ``final`` chunk) always
                    # precede its own authoritative ``output_item.done``. If
                    # items led, a message's final chunk — written to the
                    # deltas file moments before the transcript record flushed
                    # — would land just AFTER its done event and re-create the
                    # already-finalized preview on the client (duplicate bubble
                    # + a stale trailing preview). See the web reconciler.
                    # Within-poll order can't cover the cross-poll race
                    # (transcript flushed, hook delta write not yet);
                    # ``delta_ordering`` closes it by holding the assistant
                    # item until its deltas byte-match or a timeout expires.
                    delta_state = await _forward_available_deltas(
                        client=client,
                        session_id=current_session_id,
                        bridge_dir=bridge_dir,
                        state=delta_state,
                        seen_keys=seen_delta_keys,
                        ordering=delta_ordering,
                    )
                    state = await _forward_available_items(
                        client=client,
                        session_id=current_session_id,
                        bridge_dir=bridge_dir,
                        agent_name=agent_name,
                        state=state,
                        retry_tracker=item_retries,
                        skip_user_messages=skip_user_messages,
                        dedupe=dedupe,
                        ordering=delta_ordering,
                    )
                    hook_state = await _forward_available_status_events(
                        client=client,
                        session_id=current_session_id,
                        bridge_dir=bridge_dir,
                        state=hook_state,
                        retry_tracker=status_retries,
                        task_subjects=task_subjects,
                        task_statuses=task_statuses,
                        task_order=task_order,
                    )
                    subagent_state = await _forward_available_subagents(
                        client=client,
                        parent_session_id=current_session_id,
                        bridge_dir=bridge_dir,
                        transcript_path=transcript_path,
                        state=subagent_state,
                        agent_name=agent_name,
                        start_retry_tracker=subagent_start_retries,
                        item_retry_tracker=subagent_item_retries,
                        status_retry_tracker=subagent_status_retries,
                    )
                    # Reconcile + POST cumulative cost AFTER sub-agents are
                    # forwarded so the estimate sees this poll's sub-agent
                    # transcript growth. This is what lets the parent's
                    # cost-budget policy block a sub-agent's tool calls
                    # mid-turn (the statusLine total alone lags until the
                    # sub-agent finishes).
                    await _forward_session_cost(
                        client=client,
                        session_id=current_session_id,
                        bridge_dir=bridge_dir,
                        parent_transcript_path=transcript_path,
                        subagent_state=subagent_state,
                        dedupe=dedupe,
                        cost_cache=cost_cache,
                    )
                    # Mirror the live statusLine model EVERY poll (not just
                    # when a turn produced new transcript items, which
                    # _forward_available_items early-returns without). This
                    # propagates an in-pane /model switch to model_override
                    # before the user's next message, so model-gated policies
                    # (cost-budget hard cap) no longer lag a switch by one turn.
                    await _forward_model_from_status(
                        client=client,
                        session_id=current_session_id,
                        bridge_dir=bridge_dir,
                        dedupe=dedupe,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception(
                    "Claude transcript forwarder loop failed; session=%s bridge_dir=%s",
                    session_id,
                    bridge_dir,
                )
            await asyncio.sleep(poll_interval_s)


def _subagents_dir_for_transcript(transcript_path: Path) -> Path:
    """
    Resolve the on-disk ``subagents/`` directory for a Claude session.

    Claude Code writes each Task-tool sub-agent's transcript to
    ``~/.claude/projects/<encoded>/<session>/subagents/agent-*.jsonl``
    where ``<session>`` matches the parent transcript's filename stem.
    The parent transcript itself lives at
    ``~/.claude/projects/<encoded>/<session>.jsonl`` alongside that
    directory.

    :param transcript_path: Parent's transcript JSONL,
        e.g. ``"~/.claude/projects/-Users-x-repo/85a2b8ac.jsonl"``.
    :returns: Path to the parent's ``subagents/`` directory (may not
        exist yet — caller is responsible for handling the "no
        sub-agents have been spawned yet" case).
    """
    return transcript_path.parent / transcript_path.stem / "subagents"


def _read_subagent_forward_state(bridge_dir: Path) -> SubagentForwardState:
    """
    Read the sub-agent forwarder's durable cursor map.

    Returns an empty state when no file has been persisted yet (the
    first time the watcher runs for this bridge directory). Malformed
    JSON / corrupt rows are treated as empty so a botched write can't
    permanently wedge the watcher.

    :param bridge_dir: Native Claude bridge directory.
    :returns: A :class:`SubagentForwardState`, possibly empty.
    """
    try:
        raw = json.loads((bridge_dir / _SUBAGENT_STATE_FILE).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return SubagentForwardState(subagents={})
    if not isinstance(raw, dict):
        return SubagentForwardState(subagents={})
    subagents_raw = raw.get("subagents", {})
    if not isinstance(subagents_raw, dict):
        return SubagentForwardState(subagents={})
    entries: dict[str, SubagentEntry] = {}
    for subagent_id, row in subagents_raw.items():
        if not isinstance(subagent_id, str) or not isinstance(row, dict):
            continue
        child_id = row.get("child_conversation_id")
        byte_offset = row.get("byte_offset", 0)
        seen_source_ids = row.get("seen_source_ids", [])
        last_activity_ts = row.get("last_activity_ts")
        last_status = row.get("last_status")
        # Empty string is a valid parked sentinel written by
        # ``_forward_available_subagents`` after the start POST exhausts
        # its permanent-failure budget. Preserving it across restarts is
        # what keeps the parked sub-agent from being retried.
        if not isinstance(child_id, str):
            continue
        if not isinstance(byte_offset, int) or byte_offset < 0:
            byte_offset = 0
        if not isinstance(seen_source_ids, list) or not all(
            isinstance(source_id, str) for source_id in seen_source_ids
        ):
            seen_source_ids = []
        if last_activity_ts is not None and not isinstance(last_activity_ts, (int, float)):
            last_activity_ts = None
        if last_status is not None and not isinstance(last_status, str):
            last_status = None
        entries[subagent_id] = SubagentEntry(
            subagent_id=subagent_id,
            child_conversation_id=child_id,
            byte_offset=byte_offset,
            seen_source_ids=tuple(seen_source_ids),
            last_activity_ts=last_activity_ts,
            last_status=last_status,
        )
    return SubagentForwardState(subagents=entries)


def _write_subagent_forward_state(bridge_dir: Path, state: SubagentForwardState) -> None:
    """
    Write the sub-agent forwarder's cursor map to disk atomically.

    :param bridge_dir: Native Claude bridge directory.
    :param state: Cursor map to persist.
    :returns: None.
    """
    bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "subagents": {
            entry.subagent_id: {
                "child_conversation_id": entry.child_conversation_id,
                "byte_offset": entry.byte_offset,
                "seen_source_ids": list(entry.seen_source_ids),
                "last_activity_ts": entry.last_activity_ts,
                "last_status": entry.last_status,
            }
            for entry in state.subagents.values()
        },
        "updated_at": time.time(),
    }
    _write_json_atomic(bridge_dir / _SUBAGENT_STATE_FILE, payload)


async def _write_subagent_forward_state_async(
    bridge_dir: Path,
    state: SubagentForwardState,
) -> None:
    """
    Persist sub-agent state without blocking the asyncio event loop.

    :param bridge_dir: Native Claude bridge directory.
    :param state: Cursor map to persist.
    :returns: None.
    """
    await asyncio.to_thread(_write_subagent_forward_state, bridge_dir, state)


def _parse_json_response(resp: httpx.Response, *, context: str) -> Any:
    """
    Parse an Omnigent JSON response, failing loudly on a non-JSON body.

    The forwarder calls ``resp.json()`` on Sessions API responses after
    ``resp.raise_for_status()``. That guards non-2xx statuses but not a
    2xx body that simply is not JSON: an auth or proxy layer in front of
    the server — most commonly an expired Databricks Apps OAuth session —
    can serve an HTML login or error page with a 200 status. A bare
    ``resp.json()`` then raises an opaque ``json.JSONDecodeError``
    ("Expecting value: line 1 column 1 (char 0)") with no hint that the
    body was HTML, and the forwarder supervisor turns that into a silent
    restart loop. This wrapper re-raises with the response content type
    and a body snippet so the cause is obvious in logs.

    :param resp: HTTP response whose body is expected to be JSON.
    :param context: Short request description for the error message,
        e.g. ``"session conv_abc123 snapshot"``.
    :returns: The parsed JSON value (object, array, or scalar).
    :raises RuntimeError: If the response body is not valid JSON.
    """
    try:
        return resp.json()
    except ValueError as exc:
        content_type = resp.headers.get("content-type") or "<unknown>"
        snippet = " ".join(resp.text[:200].split())
        raise RuntimeError(
            f"{context} returned a non-JSON body (content-type "
            f"{content_type!r}); an auth or proxy page was likely served "
            f"instead of the API response (e.g. an expired login session). "
            f"Body starts with: {snippet!r}"
        ) from exc


async def _post_external_subagent_start(
    client: httpx.AsyncClient,
    *,
    parent_session_id: str,
    subagent_id: str,
    agent_type: str,
    description: str,
    tool_use_id: str,
) -> str:
    """
    POST ``external_subagent_start`` to the Omnigent server and return the
    minted child Conversation id.

    :param client: Omnigent HTTP client.
    :param parent_session_id: Parent (claude-native) conversation id,
        e.g. ``"conv_parent987"``.
    :param subagent_id: Stable Claude-side identifier read from
        ``agent-<id>.meta.json``'s filename, e.g.
        ``"a5c7effac5a9a35ab"``.
    :param agent_type: Claude sub-agent type from the meta file,
        e.g. ``"Explore"``.
    :param description: Free-form description from the meta file,
        e.g. ``"Investigate web UI session data flow"``.
    :param tool_use_id: Parent transcript's ``Task`` tool-use block
        id this sub-agent was spawned from, e.g. ``"toolu_..."``.
    :returns: The Omnigent child conversation id, e.g. ``"conv_child456"``.
    :raises httpx.HTTPError: If the Omnigent request fails or is rejected.
    :raises KeyError: If the server response is missing
        ``child_session_id`` — indicates a server/forwarder version
        mismatch and is unrecoverable for this sub-agent.
    :raises RuntimeError: If the server response body is not JSON.
    """
    resp = await client.post(
        f"/v1/sessions/{parent_session_id}/events",
        json={
            "type": "external_subagent_start",
            "data": {
                "subagent_id": subagent_id,
                "agent_type": agent_type,
                "description": description,
                "tool_use_id": tool_use_id,
            },
        },
    )
    resp.raise_for_status()
    body = _parse_json_response(resp, context=f"sub-agent start for {parent_session_id!r}")
    return body["child_session_id"]


def _read_subagent_meta(meta_path: Path) -> dict[str, str] | None:
    """
    Read a Claude sub-agent's ``.meta.json`` file, validating the
    fields the forwarder needs.

    Returns ``None`` (rather than raising) when the file is missing
    or malformed so the watcher can skip it gracefully and try again
    on the next tick.

    :param meta_path: Path to ``agent-<id>.meta.json``.
    :returns: A dict with string-typed ``agentType``, ``description``,
        and ``toolUseId``; or ``None`` when the file is missing /
        malformed / missing any required key.
    """
    try:
        raw = json.loads(meta_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(raw, dict):
        return None
    agent_type = raw.get("agentType")
    description = raw.get("description")
    tool_use_id = raw.get("toolUseId")
    if not isinstance(agent_type, str) or not agent_type:
        return None
    if not isinstance(description, str):
        return None
    if not isinstance(tool_use_id, str) or not tool_use_id:
        return None
    return {
        "agentType": agent_type,
        "description": description,
        "toolUseId": tool_use_id,
    }


async def _forward_available_subagents(
    *,
    client: httpx.AsyncClient,
    parent_session_id: str,
    bridge_dir: Path,
    transcript_path: Path,
    state: SubagentForwardState,
    agent_name: str,
    start_retry_tracker: _PostRetryTracker,
    item_retry_tracker: _PostRetryTracker,
    status_retry_tracker: _PostRetryTracker,
) -> SubagentForwardState:
    """
    Discover new Claude Task-tool sub-agents on disk, mint Omnigent child
    conversations for them, tail their transcripts, and publish
    quiescence-based status.

    Idempotent across forwarder restarts: ``state`` (persisted to
    ``subagent_forwarder.json``) holds the Omnigent child id and byte
    offset for every sub-agent already seen. Sub-agents whose
    ``.meta.json`` appears for the first time are registered with AP
    via ``external_subagent_start``; sub-agents already in ``state``
    just have their ``.jsonl`` tailed forward.

    :param client: Omnigent HTTP client.
    :param parent_session_id: Parent (claude-native) conversation id.
    :param bridge_dir: Native Claude bridge directory.
    :param transcript_path: Parent's transcript JSONL — used to
        locate the sibling ``subagents/`` directory.
    :param state: Current sub-agent cursor map.
    :param agent_name: Agent/model name to stamp on mirrored items
        (mirrors the value used for the parent's transcript).
    :param start_retry_tracker: Backoff tracker for failed
        ``external_subagent_start`` POSTs (keyed by ``subagent_id``).
    :param item_retry_tracker: Backoff tracker for failed
        ``external_conversation_item`` POSTs (keyed by source id).
    :param status_retry_tracker: Backoff tracker for failed
        ``external_session_status`` POSTs (keyed by
        ``status:<child_id>``).
    :returns: Updated state with new sub-agents registered and
        existing sub-agents' cursors advanced.
    """
    subagents_dir = _subagents_dir_for_transcript(transcript_path)
    if not subagents_dir.is_dir():
        return state

    # ── Register newly-appeared sub-agents ──────────────
    # ``glob`` is sync; offload to a thread so we don't stat the
    # filesystem on the event loop.
    meta_paths = await asyncio.to_thread(lambda: sorted(subagents_dir.glob(_SUBAGENT_META_GLOB)))
    updated = state
    for meta_path in meta_paths:
        # ``agent-<id>.meta.json`` → ``<id>``
        subagent_id = meta_path.stem.removeprefix("agent-").removesuffix(".meta")
        if subagent_id in updated.subagents:
            continue
        retry_key = f"subagent_start:{subagent_id}"
        if start_retry_tracker.retry_delay_s(retry_key) is not None:
            continue
        meta = await asyncio.to_thread(_read_subagent_meta, meta_path)
        if meta is None:
            # File may be mid-write; try again on the next tick.
            continue
        try:
            child_id = await _post_external_subagent_start(
                client,
                parent_session_id=parent_session_id,
                subagent_id=subagent_id,
                agent_type=meta["agentType"],
                description=meta["description"],
                tool_use_id=meta["toolUseId"],
            )
        except httpx.HTTPError as exc:
            decision = start_retry_tracker.record_failure(retry_key, exc)
            if decision.exhausted:
                _logger.error(
                    "Dropping claude-native sub-agent after permanent HTTP failures; "
                    "parent_session=%s subagent_id=%s attempts=%s http_status=%s",
                    parent_session_id,
                    subagent_id,
                    decision.attempts,
                    _http_status_for_log(exc),
                )
                # Park this sub-agent: insert a sentinel entry so we
                # don't keep retrying. ``child_conversation_id=""``
                # is filtered out by the tail / status loops below.
                updated = SubagentForwardState(
                    subagents={
                        **updated.subagents,
                        subagent_id: SubagentEntry(
                            subagent_id=subagent_id,
                            child_conversation_id="",
                        ),
                    }
                )
                await _write_subagent_forward_state_async(bridge_dir, updated)
                continue
            _logger.warning(
                "Failed to register claude-native sub-agent; parent_session=%s "
                "subagent_id=%s attempt=%s permanent=%s next_retry_s=%.3f "
                "http_status=%s",
                parent_session_id,
                subagent_id,
                decision.attempts,
                decision.permanent,
                decision.delay_s,
                _http_status_for_log(exc),
                exc_info=True,
            )
            continue
        start_retry_tracker.clear(retry_key)
        updated = SubagentForwardState(
            subagents={
                **updated.subagents,
                subagent_id: SubagentEntry(
                    subagent_id=subagent_id,
                    child_conversation_id=child_id,
                ),
            }
        )
        await _write_subagent_forward_state_async(bridge_dir, updated)

    # ── Tail each tracked sub-agent's transcript ────────
    now = time.time()
    for subagent_id, entry in list(updated.subagents.items()):
        if not entry.child_conversation_id:
            # Parked after exhausted start retries — nothing to tail.
            continue
        jsonl_path = subagents_dir / f"agent-{subagent_id}.jsonl"
        if not jsonl_path.exists():
            continue
        # Reuse the parent-transcript parser, but pass
        # ``include_sidechains=True`` — every record in a sub-agent's
        # own ``agent-<id>.jsonl`` carries ``isSidechain: true``
        # (that's the whole point of the file's existence as a
        # separate transcript), and the parser's default ``False``
        # would strip every line and leave the child conversation
        # empty.
        result = await asyncio.to_thread(
            read_transcript_items_from_offset,
            jsonl_path,
            entry.byte_offset,
            start_line=0,
            agent_name=agent_name,
            current_response_id=None,
            include_sidechains=True,
        )
        new_entry = entry
        had_item = False
        items_failed = False
        seen_source_ids = list(entry.seen_source_ids)
        seen = set(seen_source_ids)
        for item in result.items:
            if item.source_id in seen:
                continue
            retry_key = f"subagent_item:{entry.child_conversation_id}:{item.source_id}"
            if item_retry_tracker.retry_delay_s(retry_key) is not None:
                # Try again on a later tick — leave the cursor where
                # it was so we re-read the same items.
                items_failed = True
                break
            try:
                await _post_external_conversation_item(
                    client,
                    session_id=entry.child_conversation_id,
                    item=item,
                )
            except httpx.HTTPError as exc:
                decision = item_retry_tracker.record_failure(retry_key, exc)
                if decision.exhausted:
                    _logger.error(
                        "Dropping claude-native sub-agent transcript item after "
                        "permanent HTTP failures; child=%s source_id=%s "
                        "attempts=%s http_status=%s",
                        entry.child_conversation_id,
                        item.source_id,
                        decision.attempts,
                        _http_status_for_log(exc),
                    )
                    # Skip this item and continue — alternative is to
                    # block the whole sub-agent forever on one poison
                    # record. The full transcript is still on disk if
                    # someone needs to recover it.
                    seen.add(item.source_id)
                    seen_source_ids.append(item.source_id)
                    new_entry = SubagentEntry(
                        subagent_id=entry.subagent_id,
                        child_conversation_id=entry.child_conversation_id,
                        byte_offset=entry.byte_offset,
                        seen_source_ids=_bounded_seen_source_ids(seen_source_ids),
                        last_activity_ts=new_entry.last_activity_ts,
                        last_status=new_entry.last_status,
                    )
                    updated = SubagentForwardState(
                        subagents={**updated.subagents, subagent_id: new_entry}
                    )
                    await _write_subagent_forward_state_async(bridge_dir, updated)
                    continue
                if post_may_have_been_delivered(exc):
                    # Ambiguous failure: the item may already be committed
                    # (no external-item dedup), so a retry would duplicate
                    # it. Skip rather than re-post.
                    _logger.warning(
                        "Skipping claude-native sub-agent item after an ambiguous POST "
                        "failure (may already be committed); not retrying to avoid a "
                        "duplicate; child=%s source_id=%s http_status=%s",
                        entry.child_conversation_id,
                        item.source_id,
                        _http_status_for_log(exc),
                        exc_info=True,
                    )
                    item_retry_tracker.clear(retry_key)
                    seen.add(item.source_id)
                    seen_source_ids.append(item.source_id)
                    new_entry = SubagentEntry(
                        subagent_id=entry.subagent_id,
                        child_conversation_id=entry.child_conversation_id,
                        byte_offset=entry.byte_offset,
                        seen_source_ids=_bounded_seen_source_ids(seen_source_ids),
                        last_activity_ts=new_entry.last_activity_ts,
                        last_status=new_entry.last_status,
                    )
                    updated = SubagentForwardState(
                        subagents={**updated.subagents, subagent_id: new_entry}
                    )
                    await _write_subagent_forward_state_async(bridge_dir, updated)
                    continue
                _logger.warning(
                    "Failed to forward claude-native sub-agent item; child=%s "
                    "source_id=%s attempt=%s permanent=%s next_retry_s=%.3f "
                    "http_status=%s",
                    entry.child_conversation_id,
                    item.source_id,
                    decision.attempts,
                    decision.permanent,
                    decision.delay_s,
                    _http_status_for_log(exc),
                    exc_info=True,
                )
                # Hold byte_offset where it was so the next tick
                # re-reads the failed item (and everything after).
                # ``seen_source_ids`` suppresses successfully-posted
                # earlier items locally so retry safety does not
                # depend on AP-side item-id idempotency.
                items_failed = True
                break
            item_retry_tracker.clear(retry_key)
            had_item = True
            seen.add(item.source_id)
            seen_source_ids.append(item.source_id)
            new_entry = SubagentEntry(
                subagent_id=entry.subagent_id,
                child_conversation_id=entry.child_conversation_id,
                byte_offset=entry.byte_offset,
                seen_source_ids=_bounded_seen_source_ids(seen_source_ids),
                last_activity_ts=now,
                last_status=new_entry.last_status,
            )
            updated = SubagentForwardState(subagents={**updated.subagents, subagent_id: new_entry})
            await _write_subagent_forward_state_async(bridge_dir, updated)
        # Only advance the cursor when every item this tick was
        # posted successfully (or there were no items at all).
        # Advancing past a failed item permanently skips it.
        if not items_failed and (result.byte_offset != entry.byte_offset or had_item):
            new_entry = SubagentEntry(
                subagent_id=entry.subagent_id,
                child_conversation_id=entry.child_conversation_id,
                byte_offset=result.byte_offset,
                seen_source_ids=_bounded_seen_source_ids(seen_source_ids),
                last_activity_ts=now if had_item else entry.last_activity_ts,
                last_status=entry.last_status,
            )
        elif had_item:
            # Items DID flow but a later post failed — still record
            # the activity timestamp so the status badge advances,
            # but leave byte_offset at the previous tick's value so
            # the failed items get retried.
            new_entry = SubagentEntry(
                subagent_id=entry.subagent_id,
                child_conversation_id=entry.child_conversation_id,
                byte_offset=entry.byte_offset,
                seen_source_ids=_bounded_seen_source_ids(seen_source_ids),
                last_activity_ts=now,
                last_status=entry.last_status,
            )

        # Quiescence-based status. Sub-agent transcripts don't carry
        # an explicit "done" record (Claude doesn't expose one), so
        # we infer "running" from item flow and "idle" from quiet
        # time. The dedupe on ``last_status`` avoids spamming the
        # cache on every tick when nothing changed.
        desired_status: str | None = None
        if had_item:
            desired_status = "running"
        elif (
            new_entry.last_activity_ts is not None
            and now - new_entry.last_activity_ts > _SUBAGENT_IDLE_QUIESCENCE_S
            and new_entry.last_status != "idle"
        ):
            desired_status = "idle"
        if desired_status is not None and desired_status != new_entry.last_status:
            retry_key = f"subagent_status:{entry.child_conversation_id}"
            if status_retry_tracker.retry_delay_s(retry_key) is None:
                try:
                    await _post_external_session_status(
                        client,
                        session_id=entry.child_conversation_id,
                        status=desired_status,
                    )
                except httpx.HTTPError as exc:
                    decision = status_retry_tracker.record_failure(retry_key, exc)
                    _logger.warning(
                        "Failed to forward claude-native sub-agent status; "
                        "child=%s status=%s attempt=%s next_retry_s=%.3f "
                        "http_status=%s",
                        entry.child_conversation_id,
                        desired_status,
                        decision.attempts,
                        decision.delay_s,
                        _http_status_for_log(exc),
                        exc_info=True,
                    )
                else:
                    status_retry_tracker.clear(retry_key)
                    new_entry = SubagentEntry(
                        subagent_id=new_entry.subagent_id,
                        child_conversation_id=new_entry.child_conversation_id,
                        byte_offset=new_entry.byte_offset,
                        seen_source_ids=new_entry.seen_source_ids,
                        last_activity_ts=new_entry.last_activity_ts,
                        last_status=desired_status,
                    )

        if new_entry is not entry:
            updated = SubagentForwardState(subagents={**updated.subagents, subagent_id: new_entry})
            await _write_subagent_forward_state_async(bridge_dir, updated)

    return updated


def _cumulative_cost_from_status_state(state: dict[str, Any] | None) -> float | None:
    """
    Extract Claude Code's cumulative session cost from a statusLine snapshot.

    :param state: Parsed ``context.json`` payload from
        :func:`read_claude_context_state`, or ``None`` when none captured
        yet.
    :returns: ``state["total_cost_usd"]`` as a non-negative float, or
        ``None`` when absent / malformed. This is the authoritative
        whole-session total — it includes Task sub-agent spend once Claude
        Code settles it — but lags while a sub-agent is still running.
    """
    if not isinstance(state, dict):
        return None
    raw = state.get("total_cost_usd")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    if raw < 0:
        return None
    return float(raw)


def _transcript_cost_size_cached(
    transcript_path: Path,
    *,
    include_sidechains: bool,
    cache: dict[Path, _TranscriptCostCacheEntry],
) -> float | None:
    """
    Cumulative transcript cost, recomputed only when the file grows.

    Wraps :func:`compute_transcript_cumulative_cost` with a per-process
    size-keyed cache so an unchanged transcript isn't re-parsed every
    poll. On a forwarder restart the cache starts empty and the first
    call recomputes from the full file, so the estimate is correct across
    restarts (unlike an in-memory running sum, which would lose the
    pre-restart portion).

    :param transcript_path: Transcript JSONL path.
    :param include_sidechains: Forwarded to
        :func:`compute_transcript_cumulative_cost` — ``False`` for a
        parent transcript (sub-agent records are sidechains counted
        elsewhere), ``True`` for a sub-agent's own transcript.
    :param cache: Per-session cache mapping transcript path to its last
        computed :class:`_TranscriptCostCacheEntry`. Mutated in place.
    :returns: Cumulative USD cost, or ``None`` when nothing is priceable
        (missing file included).
    """
    try:
        size = transcript_path.stat().st_size
    except OSError:
        return None
    cached = cache.get(transcript_path)
    if cached is not None and cached.size == size:
        return cached.cost_usd
    cost = compute_transcript_cumulative_cost(
        transcript_path, include_sidechains=include_sidechains
    )
    cache[transcript_path] = _TranscriptCostCacheEntry(size=size, cost_usd=cost)
    return cost


def _session_cost_estimate(
    *,
    parent_transcript_path: Path,
    active_subagents: list[SubagentEntry],
    status_cost: float | None,
    cost_cache: dict[Path, _TranscriptCostCacheEntry],
) -> float | None:
    """
    Compute ``max(S, C)`` for the parent session's POLICY/budget cost.

    This is the value the cost-budget gate reads (``policy_cost_usd``),
    NOT the displayed cost — display uses ``S`` alone so the badge matches
    the Claude TUI ``/cost``. Synchronous (does transcript file I/O) —
    call via :func:`asyncio.to_thread`. ``C`` is the forwarder's real-time
    estimate: the parent transcript's own cost (sidechains excluded) plus
    the sum of each tracked sub-agent's own transcript cost (each priced
    once per ``requestId`` — see
    :func:`compute_transcript_cumulative_cost`). ``S`` is the statusLine
    total. See :func:`_forward_session_cost` for why the two are combined
    with ``max`` rather than added.

    :param parent_transcript_path: Parent transcript JSONL path; its
        sibling ``subagents/`` directory holds the sub-agent transcripts.
    :param active_subagents: Sub-agents with a minted child conversation
        (only these have an ``agent-<id>.jsonl`` on disk to price).
    :param status_cost: ``S`` — the statusLine total, or ``None`` when
        not captured yet.
    :param cost_cache: Per-session size-keyed transcript cost cache,
        mutated in place.
    :returns: ``max(S, C)`` in USD, or ``None`` when neither source
        yields a priceable cost.
    """
    subagents_dir = _subagents_dir_for_transcript(parent_transcript_path)
    estimate: float | None = _transcript_cost_size_cached(
        parent_transcript_path, include_sidechains=False, cache=cost_cache
    )
    for entry in active_subagents:
        jsonl_path = subagents_dir / f"agent-{entry.subagent_id}.jsonl"
        sub_cost = _transcript_cost_size_cached(
            jsonl_path, include_sidechains=True, cache=cost_cache
        )
        if sub_cost is not None:
            # Seed the accumulator from the parent cost, or 0.0 when the parent
            # had nothing priceable — so sub-agent cost still contributes to C.
            estimate = (estimate or 0.0) + sub_cost
    candidates = [cost for cost in (status_cost, estimate) if cost is not None]
    if not candidates:
        return None
    return max(candidates)


async def _forward_session_cost(
    *,
    client: httpx.AsyncClient,
    session_id: str,
    bridge_dir: Path,
    parent_transcript_path: Path,
    subagent_state: SubagentForwardState,
    dedupe: _ForwardDedupeState,
    cost_cache: dict[Path, _TranscriptCostCacheEntry],
) -> None:
    """
    POST the parent session's cost as TWO values: display and policy.

    The parent session's cost-budget policy gates EVERY tool call in the
    Claude process — including a Task sub-agent's, whose ``PreToolUse``
    hook the runner evaluates against this parent session (the bridge has
    one active session id; there is no per-sub-agent policy routing). But
    Claude Code's statusLine ``total_cost_usd`` (``S``) is **frozen for
    the entire duration of a sub-agent run** — the statusLine isn't even
    invoked while a sub-agent runs; ``S`` jumps to the sub-agent-inclusive
    total only when the sub-agent returns (verified live). So a value
    based on ``S`` alone can't gate a runaway sub-agent mid-turn.

    Display and enforcement therefore need different numbers, posted as
    two separate fields the server persists independently:

    - ``cumulative_cost_usd`` = ``S`` verbatim — the DISPLAY cost. The
      parent badge then matches ``/cost`` in the Claude TUI exactly (``S``
      is Claude's own billing and already includes sub-agent spend once
      settled). It is frozen during a sub-agent run; that's acceptable
      for display.
    - ``policy_cost_usd`` = ``max(S, C)`` — the POLICY/budget cost. ``C``
      is the forwarder's real-time estimate (parent transcript own
      messages + each tracked sub-agent's transcript, each priced once
      per ``requestId``). ``C`` advances while ``S`` is frozen, so the
      gate sees in-flight sub-agent spend and can block mid-turn. With no
      sub-agent there is no lag, so it equals ``S``.

    The brief intra-turn divergence (badge shows frozen ``S`` while the
    gate uses the higher live ``C``) is intentional and reconciles at the
    turn boundary when ``S`` jumps; ``max`` keeps both monotonic.

    Best-effort, like the other forwarder posts: a failed POST is retried
    on the next poll (the ``dedupe`` baselines advance only on success).

    :param client: Omnigent HTTP client.
    :param session_id: Parent (claude-native) conversation id the cost is
        attributed to, e.g. ``"conv_abc123"``.
    :param bridge_dir: Native Claude bridge directory (holds the
        statusLine snapshot read for ``S``).
    :param parent_transcript_path: Parent transcript JSONL path — used for
        the ``C`` estimate and to locate the ``subagents/`` directory.
    :param subagent_state: Current sub-agent cursor map; its tracked
        sub-agents' transcripts contribute to ``C``.
    :param dedupe: Carries ``posted_cost`` (display ``S``) and
        ``posted_policy_cost`` (``max(S, C)``) so steady values aren't
        re-POSTed each poll; mutated in place on a successful post.
    :param cost_cache: Per-session size-keyed transcript cost cache,
        mutated in place.
    :returns: None.
    """
    status_state = await asyncio.to_thread(read_claude_context_state, bridge_dir)
    status_cost = _cumulative_cost_from_status_state(status_state)
    active_subagents = [
        entry for entry in subagent_state.subagents.values() if entry.child_conversation_id
    ]
    # Display cost: the statusLine total S verbatim (matches /cost).
    display_cost = status_cost
    # Policy/budget cost: with no sub-agent it equals S; with a sub-agent
    # running it is max(S, real-time transcript estimate) so the gate sees
    # in-flight spend while S is frozen.
    if not active_subagents:
        policy_cost = status_cost
    else:
        policy_cost = await asyncio.to_thread(
            _session_cost_estimate,
            parent_transcript_path=parent_transcript_path,
            active_subagents=active_subagents,
            status_cost=status_cost,
            cost_cache=cost_cache,
        )
    # Build the payload from whichever values are present AND have advanced.
    # Monotonic per field: never walk a total backwards — guards a transient
    # lower transcript read (e.g. just after a rotation) and suppresses
    # steady-state churn. The two fields advance independently (policy_cost
    # moves mid-turn while display_cost/S is frozen).
    payload: dict[str, float | str] = {}
    if display_cost is not None and (
        dedupe.posted_cost is None or display_cost > dedupe.posted_cost
    ):
        payload["cumulative_cost_usd"] = display_cost
    if policy_cost is not None and (
        dedupe.posted_policy_cost is None or policy_cost > dedupe.posted_policy_cost
    ):
        payload["policy_cost_usd"] = policy_cost
    if not payload:
        return
    # Tag a display-cost (S) advance with the active model captured by the
    # statusLine wrapper (``{"model": "claude-opus-4-8", ...}`` in context.json).
    # claude-native sends no token counts with its cost, so the server has
    # nothing to attribute the cost to per-model without this — leaving it out
    # of the TOKEN USAGE breakdown while the session total still counts it. Sent
    # only when the display cost moves: that is the value being attributed
    # (``policy_cost_usd``-only mid-turn posts carry no new display cost).
    if "cumulative_cost_usd" in payload and isinstance(status_state, dict):
        model = status_state.get("model")
        if isinstance(model, str) and model:
            payload["model"] = model
    try:
        await _post_external_session_usage(
            client,
            session_id=session_id,
            usage=payload,
        )
    except httpx.HTTPError as exc:
        _logger.warning(
            "Failed to forward Claude session cost; session=%s bridge_dir=%s http_status=%s",
            session_id,
            bridge_dir,
            _http_status_for_log(exc),
            exc_info=True,
        )
        return
    if "cumulative_cost_usd" in payload:
        dedupe.posted_cost = display_cost
    if "policy_cost_usd" in payload:
        dedupe.posted_policy_cost = policy_cost


async def _supervisor_sleep(seconds: float) -> None:
    """
    Sleep helper used between forwarder restarts.

    Exists as a private indirection so tests can stub the wait
    without monkeypatching the global ``asyncio.sleep`` (which would
    leak across the whole pytest process; see project test rule 14).

    :param seconds: Duration to sleep, e.g. ``1.0``.
    """
    await asyncio.sleep(seconds)


def _supervisor_monotonic() -> float:
    """
    Monotonic clock reading used to measure forwarder uptime.

    Exists as a private indirection so tests can drive the
    healthy-uptime branch deterministically without touching the
    global ``time.monotonic`` (same module-singleton hazard as
    ``asyncio.sleep``).

    :returns: Seconds from an unspecified monotonic epoch.
    """
    return time.monotonic()


async def supervise_forwarder(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    bridge_dir: Path,
    agent_name: str,
    start_at_end: bool,
    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
    auth: httpx.Auth | None = None,
    skip_user_messages: bool = False,
) -> None:
    """
    Run :func:`forward_claude_transcript_to_session` under a restart supervisor.

    The forwarder's own loop catches :class:`Exception` per iteration,
    but an error raised outside that catch (e.g. during the
    ``async with httpx.AsyncClient`` setup) or an unexpected normal
    return would otherwise kill the task silently and leave the chat
    view permanently desynced from the running terminal. This
    supervisor restarts the forwarder with bounded exponential
    backoff so a transient crash recovers without operator action.

    Cancellation is honored: :class:`asyncio.CancelledError` exits
    the loop cleanly so the parent's teardown sequence (terminal
    stop, bridge cleanup) runs as before. Other
    :class:`BaseException` subclasses (``KeyboardInterrupt``,
    ``SystemExit``, ``GeneratorExit``) also propagate — only
    :class:`Exception` subclasses trigger a restart, so process-
    shutdown signals are not swallowed.

    The on-disk cursor in ``bridge_dir`` is the durable source of
    truth for progress, so restarts resume exactly where the prior
    run left off — ``start_at_end`` is only consulted on a cold
    bridge with no persisted cursor.

    :param base_url: Omnigent server base URL, e.g.
        ``"http://localhost:6767"``.
    :param headers: Static HTTP headers for Omnigent requests. Authorization
        is normally supplied via ``auth`` instead so OAuth tokens are
        refreshed per request.
    :param session_id: Omnigent session/conversation id, e.g.
        ``"conv_abc123"``.
    :param bridge_dir: Native Claude bridge directory.
    :param agent_name: Agent/model name to stamp on mirrored output.
    :param start_at_end: When ``True`` and no prior forward cursor
        exists, start from the current transcript end.
    :param poll_interval_s: Seconds between transcript polls inside
        the forwarder loop. Forwarded verbatim.
    :param auth: Optional httpx Auth that mints a fresh bearer token
        per request, e.g. ``_server_auth(profile)``. Forwarded verbatim
        to :func:`forward_claude_transcript_to_session`.
    :returns: Never normally returns; cancel the task to stop it.
    """
    backoff_s = _SUPERVISOR_INITIAL_BACKOFF_S
    while True:
        run_started_at = _supervisor_monotonic()
        crash_exc: Exception | None = None
        try:
            await forward_claude_transcript_to_session(
                base_url=base_url,
                headers=headers,
                session_id=session_id,
                bridge_dir=bridge_dir,
                agent_name=agent_name,
                start_at_end=start_at_end,
                poll_interval_s=poll_interval_s,
                auth=auth,
                skip_user_messages=skip_user_messages,
            )
            # The forwarder loop is ``while True`` and is not expected
            # to return normally. Treat any normal return as a crash
            # and restart.
            _logger.warning(
                "Claude transcript forwarder returned unexpectedly; restarting; "
                "session=%s bridge_dir=%s",
                session_id,
                bridge_dir,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — supervisor restarts on any Exception
            crash_exc = exc
        run_duration_s = _supervisor_monotonic() - run_started_at
        if run_duration_s >= _SUPERVISOR_HEALTHY_UPTIME_S:
            backoff_s = _SUPERVISOR_INITIAL_BACKOFF_S
        if crash_exc is not None:
            # Log AFTER the healthy-uptime reset so the reported delay
            # matches the sleep that actually follows.
            _logger.error(
                "Claude transcript forwarder crashed; restarting in %.1fs; "
                "session=%s bridge_dir=%s",
                backoff_s,
                session_id,
                bridge_dir,
                exc_info=crash_exc,
            )
        await _supervisor_sleep(backoff_s)
        backoff_s = min(backoff_s * 2.0, _SUPERVISOR_MAX_BACKOFF_S)


async def _maybe_rotate_session_on_clear(
    *,
    client: httpx.AsyncClient,
    session_id: str,
    bridge_dir: Path,
    state: HookForwardState,
) -> str | None:
    """
    Rotate the active Omnigent session when Claude reports ``/clear``.

    :param client: Omnigent HTTP client.
    :param session_id: Currently active Omnigent session id, e.g.
        ``"conv_old"``.
    :param bridge_dir: Native Claude bridge directory.
    :param state: Current hook cursor state.
    :returns: New active session id when rotation succeeded, otherwise
        ``None`` (no clear pending, or the rotation failed and was consumed
        to avoid a re-rotation loop).
    """
    result = await asyncio.to_thread(_read_hook_events_for_state, bridge_dir, state)
    clear_record = next(
        (
            record
            for record in result.records
            if record.event_name == "SessionStart" and record.source == "clear"
        ),
        None,
    )
    if clear_record is None:
        return None

    # Consume this clear hook EXACTLY ONCE. If the rotation raises partway
    # (e.g. the terminal transfer returns 400 because the target already owns a
    # terminal), we must still advance the cursor: otherwise the forwarder's
    # next poll re-reads the same clear record and re-rotates — creating a fresh
    # replacement session every poll, unbounded. A single /clear rotates at most
    # once; a failed rotation is logged and skipped (the old session simply
    # keeps running) rather than retried forever.
    durable = HookForwardState(
        event_cursor=clear_record.event_cursor,
        byte_offset=clear_record.byte_offset,
        cursor_fingerprint=_jsonl_cursor_fingerprint(
            bridge_dir / _HOOKS_FILE,
            clear_record.byte_offset,
        ),
    )
    new_session_id: str | None = None
    try:
        if clear_record.clear_rotated_to:
            new_session_id = clear_record.clear_rotated_to
        else:
            new_session_id = await _create_clear_replacement_session(
                client=client,
                old_session_id=session_id,
                bridge_dir=bridge_dir,
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        _logger.exception(
            "Claude /clear rotation failed; consuming the clear hook to avoid a "
            "re-rotation loop. old_session=%s",
            session_id,
        )
    await _write_hook_state_async(bridge_dir, durable)
    reset_transcript_forward_state(bridge_dir, reset_hooks=False)
    return new_session_id


async def _seed_fork_transcript_forward_state(
    *,
    bridge_dir: Path,
    transcript_path: Path | None,
) -> None:
    """
    Seed transcript forwarding after Omnigent has forked history.

    Claude fork transcripts start with copied source-session records.
    The Omnigent fork endpoint has already copied those conversation items,
    so forwarding must begin at the current end of the new Claude
    transcript rather than replaying the copied prefix.

    :param bridge_dir: Native Claude bridge directory.
    :param transcript_path: New Claude fork transcript path, e.g.
        ``"/home/user/.claude/projects/x/session.jsonl"``. ``None``
        falls back to removing the stale cursor.
    :returns: None.
    """
    if transcript_path is None:
        reset_transcript_forward_state(bridge_dir, reset_hooks=False)
        return
    reset_transcript_forward_state(bridge_dir, reset_hooks=False)
    byte_offset = await asyncio.to_thread(_transcript_end_offset, transcript_path)
    state = TranscriptForwardState(
        transcript_path=transcript_path,
        line_cursor=0,
        byte_offset=byte_offset,
        cursor_fingerprint=_jsonl_cursor_fingerprint(transcript_path, byte_offset),
    )
    await _write_forward_state_async(bridge_dir, state)


async def _create_clear_replacement_session(
    *,
    client: httpx.AsyncClient,
    old_session_id: str,
    bridge_dir: Path,
) -> str:
    """
    Create the fresh Omnigent session for a Claude ``/clear`` event.

    :param client: Omnigent HTTP client.
    :param old_session_id: Session being rotated away from, e.g.
        ``"conv_old"``.
    :param bridge_dir: Native Claude bridge directory.
    :returns: New Omnigent session id, e.g. ``"conv_new"``.
    :raises httpx.HTTPError: If Omnigent rejects session creation, new-session
        binding, or terminal transfer. Clearing the old runner binding is
        best-effort after the bridge has rotated.
    :raises RuntimeError: If the old session snapshot is malformed.
    """
    old = await _fetch_session_snapshot(client, old_session_id)
    agent_id = old.get("agent_id")
    if not isinstance(agent_id, str) or not agent_id:
        raise RuntimeError(f"session {old_session_id!r} has no agent_id")
    runner_id = old.get("runner_id")
    labels = old.get("labels") if isinstance(old.get("labels"), dict) else {}
    labels = {str(key): str(value) for key, value in labels.items()}
    labels.setdefault(BRIDGE_ID_LABEL_KEY, read_bridge_id(bridge_dir) or old_session_id)

    create_resp = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent_id,
            "labels": labels,
        },
    )
    create_resp.raise_for_status()
    created = _parse_json_response(create_resp, context="clear-replacement session create")
    new_session_id = created.get("id")
    if not isinstance(new_session_id, str) or not new_session_id:
        raise RuntimeError("clear replacement session response did not include id")

    if isinstance(runner_id, str) and runner_id:
        bind_resp = await client.patch(
            f"/v1/sessions/{url_component(new_session_id)}",
            json={"runner_id": runner_id},
        )
        bind_resp.raise_for_status()

    terminal_id = terminal_resource_id("claude", "main")
    transfer_resp = await client.post(
        (
            f"/v1/sessions/{url_component(old_session_id)}"
            f"/resources/terminals/{url_component(terminal_id)}/transfer"
        ),
        json={"target_session_id": new_session_id},
    )
    transfer_resp.raise_for_status()

    write_active_session_id(bridge_dir, new_session_id)
    clear_resp = await client.patch(
        f"/v1/sessions/{url_component(old_session_id)}",
        json={
            "runner_id": "",
            # Re-key the superseded session onto a DISTINCT "-cleared" bridge id.
            # The new session keeps the original bridge id (set above) and owns
            # the live terminal/pane in D(original); the old session must NOT
            # share that dir, or resuming it (host wake-on-message /
            # ``omnigent claude --resume``) would put a second forwarder on the
            # live transcript (duplicate items) and trip the executor's
            # "no longer active after /clear" guard. ``_auto_create_claude_terminal``
            # recognises this exact marker and cold-resumes the old session in
            # its own isolated D("{id}-cleared"); the executor spawn_env resolves
            # the same label, so both agree.
            "labels": {BRIDGE_ID_LABEL_KEY: f"{old_session_id}-cleared"},
        },
    )
    if clear_resp.status_code >= 400:
        _logger.warning(
            "Failed to clear old claude-native runner binding after /clear; "
            "old_session=%s new_session=%s status=%s body=%s",
            old_session_id,
            new_session_id,
            clear_resp.status_code,
            clear_resp.text,
        )
    return new_session_id


async def _maybe_rotate_session_on_fork(
    *,
    client: httpx.AsyncClient,
    session_id: str,
    bridge_dir: Path,
    state: HookForwardState,
) -> str | None:
    """
    Fork the active Omnigent session when Claude reports ``/fork``/``/branch``.

    The hook annotates branch-created ``SessionStart source=resume``
    records before recording them. The forwarder consumes that
    annotation so it does not have to infer branch state after
    ``state.json`` already points at the new Claude session id.

    :param client: Omnigent HTTP client.
    :param session_id: Currently active Omnigent session id, e.g.
        ``"conv_old"``.
    :param bridge_dir: Native Claude bridge directory.
    :param state: Current hook cursor state.
    :returns: New active session id when fork rotation succeeded, otherwise
        ``None`` (no fork pending, or the rotation failed and was consumed to
        avoid a re-rotation loop).
    """
    result = await asyncio.to_thread(_read_hook_events_for_state, bridge_dir, state)
    fork_record = next((record for record in result.records if _is_fork_hook_record(record)), None)
    if fork_record is None:
        return None

    # Consume this fork hook EXACTLY ONCE — see the matching guard in
    # _maybe_rotate_session_on_clear. A rotation that raises partway (e.g. a
    # terminal-transfer 400) must still advance the cursor so the next poll does
    # not re-read the same fork record and create another replacement session
    # without bound.
    durable = HookForwardState(
        event_cursor=fork_record.event_cursor,
        byte_offset=fork_record.byte_offset,
        cursor_fingerprint=_jsonl_cursor_fingerprint(
            bridge_dir / _HOOKS_FILE,
            fork_record.byte_offset,
        ),
    )
    new_session_id: str | None = None
    try:
        if fork_record.fork_rotated_to:
            new_session_id = fork_record.fork_rotated_to
        else:
            new_session_id = await _create_fork_replacement_session(
                client=client,
                old_session_id=session_id,
                bridge_dir=bridge_dir,
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        _logger.exception(
            "Claude /fork rotation failed; consuming the fork hook to avoid a "
            "re-rotation loop. old_session=%s",
            session_id,
        )
    await _write_hook_state_async(bridge_dir, durable)
    await _seed_fork_transcript_forward_state(
        bridge_dir=bridge_dir,
        transcript_path=fork_record.transcript_path,
    )
    return new_session_id


async def _create_fork_replacement_session(
    *,
    client: httpx.AsyncClient,
    old_session_id: str,
    bridge_dir: Path,
) -> str:
    """
    Create the forked Omnigent session for a Claude ``/fork``/``/branch``.

    :param client: Omnigent HTTP client.
    :param old_session_id: Session being forked away from, e.g.
        ``"conv_old"``.
    :param bridge_dir: Native Claude bridge directory.
    :returns: New Omnigent session id, e.g. ``"conv_fork"``.
    :raises httpx.HTTPError: If Omnigent rejects session fetch, fork,
        new-session binding, or terminal transfer. Clearing the old
        runner binding is best-effort after the bridge has rotated.
    :raises RuntimeError: If the Omnigent fork response is malformed.
    """
    old = await _fetch_session_snapshot(client, old_session_id)
    runner_id = old.get("runner_id")

    fork_resp = await client.post(
        f"/v1/sessions/{url_component(old_session_id)}/fork",
        json={},
    )
    fork_resp.raise_for_status()
    forked = _parse_json_response(fork_resp, context=f"fork of session {old_session_id!r}")
    new_session_id = forked.get("id")
    if not isinstance(new_session_id, str) or not new_session_id:
        raise RuntimeError("fork replacement session response did not include id")

    if isinstance(runner_id, str) and runner_id:
        bind_resp = await client.patch(
            f"/v1/sessions/{url_component(new_session_id)}",
            json={"runner_id": runner_id},
        )
        bind_resp.raise_for_status()

    terminal_id = terminal_resource_id("claude", "main")
    transfer_resp = await client.post(
        (
            f"/v1/sessions/{url_component(old_session_id)}"
            f"/resources/terminals/{url_component(terminal_id)}/transfer"
        ),
        json={"target_session_id": new_session_id},
    )
    transfer_resp.raise_for_status()

    write_active_session_id(bridge_dir, new_session_id)
    clear_resp = await client.patch(
        f"/v1/sessions/{url_component(old_session_id)}",
        json={"runner_id": ""},
    )
    if clear_resp.status_code >= 400:
        _logger.warning(
            "Failed to clear old claude-native runner binding after /fork; "
            "old_session=%s new_session=%s status=%s body=%s",
            old_session_id,
            new_session_id,
            clear_resp.status_code,
            clear_resp.text,
        )
    return new_session_id


def _is_subagent_hook_record(record: ClaudeHookRecord) -> bool:
    """
    Return whether a hook record originated from a Claude subagent.

    Claude Code subagent transcripts live under a ``subagents/``
    subdirectory (e.g.
    ``~/.claude/projects/<encoded>/<session>/subagents/agent-<id>.jsonl``).
    When a subagent fires a lifecycle hook (``Stop``,
    ``UserPromptSubmit``), its ``transcript_path`` contains that
    ``subagents`` component. The parent process's transcript lives
    one level up (``<session>.jsonl``) and never contains it.

    :param record: Claude hook record read from ``hooks.jsonl``.
    :returns: ``True`` when the record's transcript path indicates a
        subagent, ``False`` otherwise (including when no transcript
        path is available — conservative default so parent events
        are never accidentally dropped).
    """
    if record.transcript_path is None:
        return False
    return "subagents" in record.transcript_path.parts


def _is_fork_hook_record(record: ClaudeHookRecord) -> bool:
    """
    Return whether a hook record represents Claude ``/fork``.

    The stable signal comes from Claude's structured ``forkedFrom``
    transcript metadata or a recent local-command record, not from the
    human-facing session title. Hook-side annotations are still
    honored for idempotency when the synchronous hook has already
    completed the Omnigent fork.

    :param record: Claude hook record read from hooks.jsonl.
    :returns: ``True`` when the active Omnigent session should be forked.
    """
    if record.fork_detected or record.fork_rotated_to:
        return True
    if record.event_name != "SessionStart" or record.source != "resume":
        return False
    if record.transcript_path is None or record.claude_session_id is None:
        return False
    if record.recorded_at is None:
        return False
    if record.previous_claude_session_id is None:
        return False
    if record.claude_session_was_seen is not False:
        return False
    return transcript_has_forked_from_marker(
        record.transcript_path,
        claude_session_id=record.claude_session_id,
        source_claude_session_id=record.previous_claude_session_id,
    ) or transcript_has_recent_local_command(
        record.transcript_path,
        claude_session_id=record.claude_session_id,
        recorded_at=record.recorded_at,
        command_names=_FORK_COMMAND_NAMES,
    )


async def _fetch_session_snapshot(
    client: httpx.AsyncClient,
    session_id: str,
) -> dict[str, Any]:
    """
    Fetch one Omnigent session snapshot.

    :param client: Omnigent HTTP client.
    :param session_id: Omnigent session id, e.g. ``"conv_abc123"``.
    :returns: Parsed JSON snapshot.
    :raises httpx.HTTPError: If Omnigent returns a non-2xx status.
    :raises RuntimeError: If the response body is not a JSON object.
    """
    resp = await client.get(f"/v1/sessions/{url_component(session_id)}")
    resp.raise_for_status()
    payload = _parse_json_response(resp, context=f"session {session_id!r} snapshot")
    if not isinstance(payload, dict):
        raise RuntimeError(f"session {session_id!r} snapshot was not an object")
    return payload


async def _maybe_mirror_external_session_id(
    *,
    client: httpx.AsyncClient,
    session_id: str,
    bridge_dir: Path,
) -> bool:
    """
    Mirror Claude's native session id onto the Omnigent conversation row.

    Reads the latest captured Claude-native session id from the
    bridge state file and, if present, PATCHes
    ``external_session_id`` on the Omnigent conversation. Best-effort: a
    transient HTTP failure logs a warning and returns ``False`` so
    the caller retries on the next poll. Once the PATCH succeeds we
    return ``True`` and the caller latches off — the value is
    durable server-side from that point on.

    A 4xx (e.g. the server rejects an attempted overwrite of an
    already-set different value) also latches off — the divergence
    is logged loudly but retrying would just hammer the server.

    :param client: Omnigent HTTP client.
    :param session_id: Omnigent session/conversation id.
    :param bridge_dir: Native Claude bridge directory; the source of
        the captured Claude session id.
    :returns: ``True`` once mirroring is finished (or has been
        determined to be unrecoverable); ``False`` to retry next
        poll.
    """
    claude_sid = read_claude_session_id(bridge_dir)
    if claude_sid is None:
        return False
    try:
        await _patch_external_session_id(
            client,
            session_id=session_id,
            external_session_id=claude_sid,
        )
    except httpx.HTTPStatusError as exc:
        # 4xx means the server rejected the write outright (e.g.
        # overwrite conflict or schema validation). Retrying won't
        # help; latch off and let the operator see the log.
        if 400 <= exc.response.status_code < 500:
            _logger.warning(
                "AP rejected external_session_id PATCH (%s); session=%s claude_sid=%s",
                exc.response.status_code,
                session_id,
                claude_sid,
            )
            return True
        _logger.warning(
            "Transient Omnigent error PATCHing external_session_id (%s); session=%s — will retry",
            exc.response.status_code,
            session_id,
        )
        return False
    except httpx.HTTPError:
        _logger.warning(
            "Transient transport error PATCHing external_session_id; session=%s — will retry",
            session_id,
            exc_info=True,
        )
        return False
    return True


def reset_transcript_forward_state(bridge_dir: Path, *, reset_hooks: bool = True) -> None:
    """
    Remove the durable transcript-forward cursor for a fresh launch.

    :param bridge_dir: Native Claude bridge directory.
    :param reset_hooks: Whether to also remove the hook cursor. Keep
        ``False`` after consuming a ``/clear`` hook so the same clear
        record is not processed again.
    :returns: None.
    """
    filenames = [
        _FORWARDER_STATE_FILE,
        "transcript_forwarder.pause.json",
    ]
    if reset_hooks:
        filenames.append(_HOOK_STATE_FILE)
    for filename in filenames:
        with contextlib.suppress(FileNotFoundError):
            (bridge_dir / filename).unlink()


async def _ensure_hook_state(
    bridge_dir: Path,
    *,
    start_at_end: bool,
    session_id: str,
) -> HookForwardState:
    """
    Return the hook cursor state, seeding it on first use.

    :param bridge_dir: Native Claude bridge directory.
    :param start_at_end: When ``True`` and no prior cursor exists,
        start after the current complete hook records so prior records
        (e.g. an earlier ``Stop`` from a stale session) are not
        re-published on reattach while a partial trailing record can
        still complete and be read.
    :param session_id: Omnigent session/conversation id, e.g.
        ``"conv_abc123"``. Used for stale-cursor diagnostics.
    :returns: The cursor state to use for the next hook poll.
    """
    state = _read_hook_state(bridge_dir)
    if state is not None:
        return _validated_hook_state(bridge_dir, state, session_id=session_id)
    byte_offset = 0
    if start_at_end:
        byte_offset = await asyncio.to_thread(_hook_end_offset, bridge_dir)
    state = HookForwardState(
        event_cursor=0,
        byte_offset=byte_offset,
        cursor_fingerprint=_jsonl_cursor_fingerprint(bridge_dir / _HOOKS_FILE, byte_offset),
    )
    await _write_hook_state_async(bridge_dir, state)
    return state


def _compaction_status_for_record(record: ClaudeHookRecord) -> str | None:
    """
    Map a hook record to a compaction-status value, if it is one.

    Claude Code brackets a compaction with two hooks the forwarder
    translates into ``external_compaction_status`` events:

    * ``PreCompact`` → ``"in_progress"`` — fires right before Claude
      compacts (manual ``/compact`` or automatic context overflow).
    * ``SessionStart`` with ``source == "compact"`` → ``"completed"``
      — fires when Claude resumes on the freshly-compacted context.
      (Claude Code has no dedicated post-compaction hook, so the
      ``source == "compact"`` SessionStart is the completion signal.)

    Other ``SessionStart`` sources (``startup`` / ``resume`` /
    ``clear``) are not compaction and return ``None``.

    :param record: One parsed hook JSONL record.
    :returns: ``"in_progress"``, ``"completed"``, or ``None`` when the
        record is not a compaction boundary.
    """
    if record.event_name == "PreCompact":
        return "in_progress"
    if record.event_name == "SessionStart" and record.source == "compact":
        return "completed"
    return None


async def _forward_available_status_events(
    *,
    client: httpx.AsyncClient,
    session_id: str,
    bridge_dir: Path,
    state: HookForwardState,
    retry_tracker: _PostRetryTracker,
    task_subjects: dict[str, str],
    task_statuses: dict[str, str],
    task_order: list[str],
) -> HookForwardState:
    """
    Forward currently available hook events as ``session.status``.

    Maps ``Stop`` → ``idle`` and ``StopFailure`` → ``failed`` via
    ``POST /v1/sessions/{id}/events`` with type ``external_session_status``
    — the authoritative turn-end edges that drive sub-agent terminal
    delivery (see :data:`_HOOK_EVENT_TO_STATUS`). ``running`` stays
    PTY-derived (the pane-activity watcher drives the UI badge). Other hook
    event names advance the cursor without emitting (no status meaning).

    Also forwards native task state changes (``TaskCreated``,
    ``TaskCompleted``, ``PostToolUse``/``TaskUpdate``) and
    ``PostToolUse``/``TodoWrite`` todo updates as
    ``external_session_todos`` events. The ``task_subjects``,
    ``task_statuses``, and ``task_order`` dicts are mutated in-place
    to accumulate per-session task state across polls.

    :param client: Omnigent HTTP client.
    :param session_id: Omnigent session/conversation id.
    :param bridge_dir: Native Claude bridge directory.
    :param state: Current hook cursor state.
    :param retry_tracker: In-memory retry/backoff tracker for hook
        status posts.
    :param task_subjects: Mutable map of task_id → subject text for the
        native task system, e.g. ``{"1": "Create folder 'abc'"}``.
        Updated in-place from ``TaskCreated`` hook events.
    :param task_statuses: Mutable map of task_id → status string for the
        native task system, e.g. ``{"1": "in_progress", "2": "pending"}``.
        Updated in-place from ``TaskCreated``, ``TaskCompleted``, and
        ``PostToolUse``/``TaskUpdate`` hook events.
    :param task_order: Mutable ordered list of task ids in creation order,
        e.g. ``["1", "2", "3"]``. Appended in-place from ``TaskCreated``
        events. Used to render the task list in a stable order.
    :returns: Updated state. On post failure, returns the last
        durable state so successfully-posted statuses are not
        retried and the failing event is retried later.
    """
    result = await asyncio.to_thread(_read_hook_events_for_state, bridge_dir, state)
    if not result.records:
        if result.event_cursor == state.event_cursor and result.byte_offset == (
            state.byte_offset or 0
        ):
            return state
        durable = HookForwardState(
            event_cursor=result.event_cursor,
            byte_offset=result.byte_offset,
            cursor_fingerprint=_jsonl_cursor_fingerprint(
                bridge_dir / _HOOKS_FILE, result.byte_offset
            ),
        )
        await _write_hook_state_async(bridge_dir, durable)
        return durable
    durable = state
    for record in result.records:
        status = _HOOK_EVENT_TO_STATUS.get(record.event_name or "")
        next_durable = HookForwardState(
            event_cursor=record.event_cursor,
            byte_offset=record.byte_offset,
            cursor_fingerprint=_jsonl_cursor_fingerprint(
                bridge_dir / _HOOKS_FILE, record.byte_offset
            ),
        )
        # Subagent lifecycle hooks land in the same hooks.jsonl as parent
        # events because subagent processes inherit the parent's hook
        # settings. With running/idle now PTY-derived, the only mapped
        # status left is ``StopFailure`` → ``failed``: a subagent's
        # failure must NOT flip the parent session to ``failed`` — the
        # parent turn is still running while it awaits the Agent tool
        # result.
        if status is not None and _is_subagent_hook_record(record):
            _logger.debug(
                "Skipping subagent hook status; session=%s event=%s status=%s transcript=%s",
                session_id,
                record.event_name,
                status,
                record.transcript_path,
            )
            durable = next_durable
            await _write_hook_state_async(bridge_dir, durable)
            continue
        if status is None:
            # Compaction boundary (PreCompact / SessionStart source=compact)
            # → forward as a compaction-status event so the web UI brackets
            # Claude's real terminal compaction with its spinner. Best-effort:
            # advance the cursor on failure so one failed post doesn't stall
            # the rest of the hook stream.
            compaction_status = _compaction_status_for_record(record)
            if compaction_status is not None:
                try:
                    await _post_external_compaction_status(
                        client,
                        session_id=session_id,
                        status=compaction_status,
                    )
                except httpx.HTTPError:
                    _logger.warning(
                        "Failed to forward Claude compaction status; "
                        "session=%s event_cursor=%s status=%s",
                        session_id,
                        record.event_cursor,
                        compaction_status,
                        exc_info=True,
                    )
                # Persist a compaction item so session resume knows
                # the compaction boundary. Without this, rebuilding
                # the transcript from the conversation DB loads the
                # full pre-compaction history.
                if compaction_status == "completed":
                    try:
                        await _persist_native_compaction_item(
                            client,
                            session_id=session_id,
                            bridge_dir=bridge_dir,
                        )
                    except Exception:  # noqa: BLE001
                        _logger.warning(
                            "Failed to persist compaction item for %s",
                            session_id,
                            exc_info=True,
                        )
                durable = next_durable
                await _write_hook_state_async(bridge_dir, durable)
                continue
            # Handle native task system events (TaskCreated, TaskCompleted,
            # PostToolUse/TaskUpdate). Mutate the caller-owned maps in-place
            # so task state accumulates across multiple polls within a session.
            native_todos_changed = False
            if record.event_name == "TaskCreated" and record.task_id is not None:
                if record.task_id not in task_subjects:
                    task_order.append(record.task_id)
                if record.task_subject is not None:
                    task_subjects[record.task_id] = record.task_subject
                task_statuses[record.task_id] = "pending"
                native_todos_changed = True
            elif record.event_name == "TaskCompleted" and record.task_id is not None:
                task_statuses[record.task_id] = "completed"
                native_todos_changed = True
            elif (
                record.event_name == "PostToolUse"
                and record.task_id is not None
                and record.task_status is not None
            ):
                # PostToolUse/TaskUpdate — update status only; subject
                # already in map from the TaskCreated event.
                task_statuses[record.task_id] = record.task_status
                native_todos_changed = True

            # Forward todo updates from PostToolUse/TodoWrite hook events.
            # Best-effort: log and advance the cursor on failure so a
            # single failed post doesn't stall hook processing.
            todos_to_post: list[dict[str, Any]] | None = None
            if record.todos is not None:
                todos_to_post = record.todos
            elif native_todos_changed and task_order:
                todos_to_post = [
                    {
                        "content": task_subjects.get(tid, tid),
                        "status": task_statuses.get(tid, "pending"),
                        # activeForm is the gerund form used by Claude's TodoWrite tool.
                        # Native task hooks don't provide it, so we intentionally
                        # reuse the content string here. TodoPanel reads activeForm
                        # for in-progress items when it differs from content, so
                        # keeping them equal suppresses duplicate rendering.
                        "activeForm": task_subjects.get(tid, tid),
                    }
                    for tid in task_order
                ]
            if todos_to_post is not None:
                try:
                    await _post_external_session_todos(
                        client,
                        session_id=session_id,
                        todos=todos_to_post,
                    )
                except httpx.HTTPError:
                    _logger.warning(
                        "Failed to forward Claude todos from hook; session=%s event_cursor=%s",
                        session_id,
                        record.event_cursor,
                        exc_info=True,
                    )
            durable = next_durable
            await _write_hook_state_async(bridge_dir, durable)
            continue
        retry_key = f"hook:{record.event_cursor}:{record.byte_offset}:{status}"
        if retry_tracker.retry_delay_s(retry_key) is not None:
            return durable
        try:
            await _post_external_session_status(
                client,
                session_id=session_id,
                status=status,
            )
        except httpx.HTTPError as exc:
            decision = retry_tracker.record_failure(retry_key, exc)
            if decision.exhausted:
                _logger.error(
                    "Dropping Claude hook status after permanent HTTP failures; "
                    "session=%s bridge_dir=%s event_cursor=%s status=%s "
                    "attempts=%s http_status=%s",
                    session_id,
                    bridge_dir,
                    record.event_cursor,
                    status,
                    decision.attempts,
                    _http_status_for_log(exc),
                )
                if status != "failed":
                    await _post_forwarder_failed_status(
                        client,
                        session_id=session_id,
                        bridge_dir=bridge_dir,
                        reason=f"hook status {status} rejected",
                    )
                durable = next_durable
                await _write_hook_state_async(bridge_dir, durable)
                continue
            _logger.warning(
                "Failed to forward Claude hook status; session=%s bridge_dir=%s "
                "event_cursor=%s status=%s attempt=%s permanent=%s "
                "next_retry_s=%.3f http_status=%s",
                session_id,
                bridge_dir,
                record.event_cursor,
                status,
                decision.attempts,
                decision.permanent,
                decision.delay_s,
                _http_status_for_log(exc),
                exc_info=True,
            )
            return durable
        retry_tracker.clear(retry_key)
        durable = next_durable
        await _write_hook_state_async(bridge_dir, durable)
    durable = HookForwardState(
        event_cursor=result.event_cursor,
        byte_offset=result.byte_offset,
        cursor_fingerprint=_jsonl_cursor_fingerprint(bridge_dir / _HOOKS_FILE, result.byte_offset),
    )
    await _write_hook_state_async(bridge_dir, durable)
    return durable


async def _ensure_state_for_transcript(
    *,
    bridge_dir: Path,
    state: TranscriptForwardState | None,
    transcript_path: Path,
    start_at_end: bool,
    session_id: str,
) -> TranscriptForwardState:
    """
    Return a cursor state compatible with the observed transcript.

    :param bridge_dir: Native Claude bridge directory.
    :param state: Existing cursor state, or ``None``.
    :param transcript_path: Current transcript path from hooks.
    :param start_at_end: Whether a missing cursor should skip the
        transcript's existing lines.
    :param session_id: Omnigent session/conversation id, e.g.
        ``"conv_abc123"``. Used for stale-cursor diagnostics.
    :returns: Cursor state for ``transcript_path``.
    """
    if state is not None and state.transcript_path == transcript_path:
        validated = _validated_transcript_state(
            state,
            bridge_dir=bridge_dir,
            session_id=session_id,
        )
        if validated != state:
            await _write_forward_state_async(bridge_dir, validated)
        return validated
    disk_state = _read_forward_state(bridge_dir)
    if disk_state is not None and disk_state.transcript_path == transcript_path:
        validated = _validated_transcript_state(
            disk_state,
            bridge_dir=bridge_dir,
            session_id=session_id,
        )
        if validated != disk_state:
            await _write_forward_state_async(bridge_dir, validated)
        return validated
    byte_offset = 0
    if start_at_end:
        byte_offset = await asyncio.to_thread(_transcript_end_offset, transcript_path)
    state = TranscriptForwardState(
        transcript_path=transcript_path,
        line_cursor=0,
        byte_offset=byte_offset,
        cursor_fingerprint=_jsonl_cursor_fingerprint(transcript_path, byte_offset),
    )
    await _write_forward_state_async(bridge_dir, state)
    return state


async def _forward_available_items(
    *,
    client: httpx.AsyncClient,
    session_id: str,
    bridge_dir: Path,
    agent_name: str,
    state: TranscriptForwardState,
    retry_tracker: _PostRetryTracker,
    skip_user_messages: bool = False,
    dedupe: _ForwardDedupeState,
    ordering: _DeltaOrderingState | None = None,
) -> TranscriptForwardState:
    """
    Forward currently available transcript items after ``state``.

    :param client: Omnigent HTTP client.
    :param session_id: Omnigent session/conversation id.
    :param bridge_dir: Native Claude bridge directory.
    :param agent_name: Agent/model name to stamp on mirrored output.
    :param state: Current transcript cursor state.
    :param retry_tracker: In-memory retry/backoff tracker for
        transcript item posts.
    :param dedupe: Last usage / context-window / model values POSTed;
        mutated in place to suppress duplicate ``external_*`` events.
    :param ordering: Delta-ordering state shared with
        :func:`_forward_available_deltas`. An assistant ``message`` item
        whose deltas haven't fully forwarded is held (batch stops, cursor
        unadvanced) until they have or a timeout expires — see
        :func:`_hold_assistant_item_for_deltas`. ``None`` disables holding.
    :returns: The updated transcript cursor state. On post failure it
        is the last durable cursor so retries don't re-post successful
        items.
    """
    result = await asyncio.to_thread(_read_transcript_items_for_state, state, agent_name)
    items = result.items
    if not items:
        if result.line_cursor == state.line_cursor and result.byte_offset == (
            state.byte_offset or 0
        ):
            return state
    current_response_id = result.current_response_id
    seen_source_ids = list(state.seen_source_ids)
    seen = set(seen_source_ids)
    # NOTE: the old "re-assert running on resumed agent output" hack lived
    # here. It only existed to paper over the hook model's compaction
    # blind spot (``Stop`` → idle, then an ``isCompactSummary`` resume that
    # never fired ``UserPromptSubmit``). PTY-activity status makes it
    # obsolete: the pane keeps changing through a mid-turn compaction, so
    # the runner's watcher holds the session ``running`` directly.
    updated = state
    for item in items:
        if item.source_id in seen:
            continue
        if skip_user_messages and item.item_type == "message" and item.data.get("role") == "user":
            seen_source_ids.append(item.source_id)
            seen.add(item.source_id)
            continue
        # Deltas-before-done: defer an assistant message whose deltas
        # haven't forwarded yet. Stop the batch here (cursor before this
        # item) so later items can't overtake it.
        if _hold_assistant_item_for_deltas(item, ordering, bridge_dir):
            return updated
        retry_key = f"item:{item.source_id}"
        if retry_tracker.retry_delay_s(retry_key) is not None:
            return updated
        try:
            await _post_external_conversation_item(
                client,
                session_id=session_id,
                item=item,
            )
        except httpx.HTTPError as exc:
            decision = retry_tracker.record_failure(retry_key, exc)
            if decision.exhausted:
                _logger.error(
                    "Dropping Claude transcript item after permanent HTTP failures; "
                    "session=%s bridge_dir=%s source_id=%s item_type=%s "
                    "attempts=%s http_status=%s",
                    session_id,
                    bridge_dir,
                    item.source_id,
                    item.item_type,
                    decision.attempts,
                    _http_status_for_log(exc),
                )
                await _post_forwarder_failed_status(
                    client,
                    session_id=session_id,
                    bridge_dir=bridge_dir,
                    reason=f"transcript item {item.source_id} rejected",
                )
                seen.add(item.source_id)
                seen_source_ids.append(item.source_id)
                updated = TranscriptForwardState(
                    transcript_path=state.transcript_path,
                    line_cursor=state.line_cursor,
                    byte_offset=state.byte_offset,
                    current_response_id=current_response_id,
                    seen_source_ids=_bounded_seen_source_ids(seen_source_ids),
                    cursor_fingerprint=state.cursor_fingerprint,
                )
                await _write_forward_state_async(bridge_dir, updated)
                continue
            if post_may_have_been_delivered(exc):
                # Ambiguous failure: the server may have committed this
                # item before the response was lost. External items aren't
                # deduped, so a retry would duplicate the bubble —
                # skip it. At worst one item is lost on a flaky POST.
                _logger.warning(
                    "Skipping Claude transcript item after an ambiguous POST failure "
                    "(may already be committed); not retrying to avoid a duplicate; "
                    "session=%s bridge_dir=%s source_id=%s item_type=%s http_status=%s",
                    session_id,
                    bridge_dir,
                    item.source_id,
                    item.item_type,
                    _http_status_for_log(exc),
                    exc_info=True,
                )
                retry_tracker.clear(retry_key)
                seen.add(item.source_id)
                seen_source_ids.append(item.source_id)
                updated = TranscriptForwardState(
                    transcript_path=state.transcript_path,
                    line_cursor=state.line_cursor,
                    byte_offset=state.byte_offset,
                    current_response_id=current_response_id,
                    seen_source_ids=_bounded_seen_source_ids(seen_source_ids),
                    cursor_fingerprint=state.cursor_fingerprint,
                )
                await _write_forward_state_async(bridge_dir, updated)
                continue
            _logger.warning(
                "Failed to forward Claude transcript item; session=%s bridge_dir=%s "
                "source_id=%s item_type=%s attempt=%s permanent=%s "
                "next_retry_s=%.3f http_status=%s",
                session_id,
                bridge_dir,
                item.source_id,
                item.item_type,
                decision.attempts,
                decision.permanent,
                decision.delay_s,
                _http_status_for_log(exc),
                exc_info=True,
            )
            return updated
        retry_tracker.clear(retry_key)
        await _maybe_sync_effort_from_slash_command(client, session_id=session_id, item=item)
        seen.add(item.source_id)
        seen_source_ids.append(item.source_id)
        updated = TranscriptForwardState(
            transcript_path=state.transcript_path,
            line_cursor=state.line_cursor,
            byte_offset=state.byte_offset,
            current_response_id=current_response_id,
            seen_source_ids=_bounded_seen_source_ids(seen_source_ids),
            cursor_fingerprint=state.cursor_fingerprint,
        )
        await _write_forward_state_async(bridge_dir, updated)
    updated = TranscriptForwardState(
        transcript_path=state.transcript_path,
        line_cursor=result.line_cursor,
        byte_offset=result.byte_offset,
        current_response_id=current_response_id,
        seen_source_ids=_bounded_seen_source_ids(seen_source_ids),
        cursor_fingerprint=_jsonl_cursor_fingerprint(state.transcript_path, result.byte_offset),
    )
    await _write_forward_state_async(bridge_dir, updated)
    # POST usage AFTER items so the ring never leads the transcript.
    # Best-effort: a failed post is retried on the next poll.
    #
    # Authoritative source for both numerator and denominator is the
    # statusLine stdin captured by ``omnigent.claude_native_status``
    # — Claude Code knows the real context window for the active
    # model + beta tier. The JSONL ``message.usage`` is used as a
    # numerator fallback only when the statusLine hasn't fired yet
    # (e.g. cold-resume before the first render tick).
    status_state = await asyncio.to_thread(read_claude_context_state, bridge_dir)
    resolved_context_window = (
        status_state.get("context_window_size") if status_state is not None else None
    )
    usage_from_status = (
        _usage_from_status_state(status_state) if status_state is not None else None
    )
    posted_usage = usage_from_status if usage_from_status is not None else result.latest_usage
    # Cost (``cumulative_cost_usd``) is POSTed separately by
    # ``_forward_session_cost``, which reconciles the statusLine total with the
    # forwarder's real-time sub-agent transcript estimate via max(). Strip it
    # here so this token/context-window post and the cost post don't both SET
    # ``total_cost_usd`` with different values and flap it on alternating polls.
    if posted_usage is not None and "cumulative_cost_usd" in posted_usage:
        posted_usage = {
            key: value for key, value in posted_usage.items() if key != "cumulative_cost_usd"
        }
    usage_changed = posted_usage is not None and posted_usage != dedupe.usage
    window_changed = (
        resolved_context_window is not None and resolved_context_window != dedupe.context_window
    )
    if usage_changed or window_changed:
        try:
            await _post_external_session_usage(
                client,
                session_id=session_id,
                usage=posted_usage,
                context_window=resolved_context_window,
            )
            if usage_changed:
                dedupe.usage = posted_usage
            if window_changed:
                dedupe.context_window = resolved_context_window
        except httpx.HTTPError as exc:
            _logger.warning(
                "Failed to forward Claude transcript usage; session=%s bridge_dir=%s "
                "http_status=%s",
                session_id,
                bridge_dir,
                _http_status_for_log(exc),
                exc_info=True,
            )
    # Mirror a TUI-side `/model` switch to the web picker. The transcript
    # records the resolved concrete id (e.g. "claude-opus-4-8"); collapse
    # it to the picker's tier alias. This transcript-derived observation
    # only fires when a turn produces a fresh ``message.model``, so it lags
    # an in-pane switch by one turn — the per-poll statusLine sync
    # (:func:`_forward_model_from_status`) is the primary, low-latency
    # source; this stays as a fallback for cold-resume before the first
    # statusLine render. Both share ``dedupe`` so neither double-posts.
    await _post_model_change_if_new(
        client,
        session_id=session_id,
        dedupe=dedupe,
        alias=_model_alias_for(result.latest_model),
    )
    return updated


def _read_hook_events_for_state(
    bridge_dir: Path,
    state: HookForwardState,
) -> HookReadResult:
    """
    Read hook events using the best cursor available in ``state``.

    :param bridge_dir: Native Claude bridge directory.
    :param state: Current hook forwarder state.
    :returns: Hook records and updated cursors. States without a
        byte offset are migrated by one line-cursor compatibility scan.
    """
    if state.byte_offset is None:
        return read_hook_events_since_with_position(bridge_dir, state.event_cursor)
    return read_hook_events_from_offset(
        bridge_dir,
        state.byte_offset,
        start_event_count=state.event_cursor,
    )


def _validated_hook_state(
    bridge_dir: Path,
    state: HookForwardState,
    *,
    session_id: str,
) -> HookForwardState:
    """
    Reset a hook cursor if its byte-offset fingerprint is stale.

    :param bridge_dir: Native Claude bridge directory.
    :param state: Hook cursor loaded from memory or disk.
    :param session_id: Omnigent session/conversation id, e.g.
        ``"conv_abc123"``. Used for diagnostics.
    :returns: ``state`` when its byte cursor still matches the file,
        otherwise a fresh cursor at the beginning of ``hooks.jsonl``.
    """
    if state.byte_offset is None:
        return state
    hooks_path = bridge_dir / _HOOKS_FILE
    current_fingerprint = _jsonl_cursor_fingerprint(hooks_path, state.byte_offset)
    if current_fingerprint is None:
        _logger.warning(
            "Claude hook JSONL cursor invalid; resetting cursor; "
            "session=%s bridge_dir=%s byte_offset=%s",
            session_id,
            bridge_dir,
            state.byte_offset,
        )
    elif state.cursor_fingerprint is None:
        _logger.warning(
            "Claude hook JSONL cursor missing fingerprint; resetting cursor; "
            "session=%s bridge_dir=%s byte_offset=%s",
            session_id,
            bridge_dir,
            state.byte_offset,
        )
    elif current_fingerprint == state.cursor_fingerprint:
        return state
    else:
        _logger.warning(
            "Claude hook JSONL cursor fingerprint changed; resetting cursor; "
            "session=%s bridge_dir=%s byte_offset=%s",
            session_id,
            bridge_dir,
            state.byte_offset,
        )
    return HookForwardState(
        event_cursor=0,
        byte_offset=0,
        cursor_fingerprint=_jsonl_cursor_fingerprint(hooks_path, 0),
    )


def _read_transcript_items_for_state(
    state: TranscriptForwardState,
    agent_name: str,
) -> TranscriptReadResult:
    """
    Read transcript items using the best cursor available in ``state``.

    :param state: Current transcript forwarder state.
    :param agent_name: Agent/model name to stamp on mirrored output.
    :returns: Transcript items and updated cursors. States without a
        byte offset are migrated by one line-cursor compatibility scan.
    """
    if state.byte_offset is None:
        return read_transcript_items_since_with_position(
            state.transcript_path,
            state.line_cursor,
            agent_name=agent_name,
            current_response_id=state.current_response_id,
        )
    return read_transcript_items_from_offset(
        state.transcript_path,
        state.byte_offset,
        start_line=state.line_cursor,
        agent_name=agent_name,
        current_response_id=state.current_response_id,
    )


def _validated_transcript_state(
    state: TranscriptForwardState,
    *,
    bridge_dir: Path,
    session_id: str,
) -> TranscriptForwardState:
    """
    Reset a transcript cursor if its byte-offset fingerprint is stale.

    :param state: Transcript cursor loaded from memory or disk.
    :param bridge_dir: Native Claude bridge directory.
    :param session_id: Omnigent session/conversation id, e.g.
        ``"conv_abc123"``. Used for diagnostics.
    :returns: ``state`` unchanged when its byte cursor still matches
        the file; ``state`` with an adopted fingerprint (no reset)
        when the cursor is at byte 0 / line 0 and the file just
        appeared; otherwise a cursor skipped to end-of-file with
        ``seen_source_ids`` preserved so already-forwarded items
        are not re-posted.
    """
    if state.byte_offset is None:
        return state
    current_fingerprint = _jsonl_cursor_fingerprint(state.transcript_path, state.byte_offset)
    if current_fingerprint is None:
        if not state.transcript_path.exists():
            return state
        _logger.warning(
            "Claude transcript cursor invalid; skipping to end of transcript; "
            "session=%s bridge_dir=%s transcript=%s byte_offset=%s",
            session_id,
            bridge_dir,
            state.transcript_path,
            state.byte_offset,
        )
    elif state.cursor_fingerprint is None:
        if state.byte_offset == 0 and state.line_cursor == 0:
            # State was written before the transcript file existed (fingerprint
            # was None because the file was missing). The file now exists and
            # the cursor is still at the start — adopt the computed fingerprint
            # without resetting seen_source_ids.
            return TranscriptForwardState(
                transcript_path=state.transcript_path,
                line_cursor=state.line_cursor,
                byte_offset=state.byte_offset,
                current_response_id=state.current_response_id,
                seen_source_ids=state.seen_source_ids,
                cursor_fingerprint=current_fingerprint,
            )
        _logger.warning(
            "Claude transcript cursor missing fingerprint; skipping to end of transcript; "
            "session=%s bridge_dir=%s transcript=%s byte_offset=%s",
            session_id,
            bridge_dir,
            state.transcript_path,
            state.byte_offset,
        )
    elif current_fingerprint == state.cursor_fingerprint:
        return state
    else:
        _logger.warning(
            "Claude transcript cursor fingerprint changed; skipping to end of transcript; "
            "session=%s bridge_dir=%s transcript=%s byte_offset=%s",
            session_id,
            bridge_dir,
            state.transcript_path,
            state.byte_offset,
        )
    end_offset = _transcript_end_offset(state.transcript_path)
    return TranscriptForwardState(
        transcript_path=state.transcript_path,
        line_cursor=0,
        byte_offset=end_offset,
        cursor_fingerprint=_jsonl_cursor_fingerprint(state.transcript_path, end_offset),
        seen_source_ids=state.seen_source_ids,
    )


async def _post_clear_supersession(
    client: httpx.AsyncClient,
    *,
    old_session_id: str,
    new_session_id: str,
    agent_name: str,
) -> None:
    """
    Notify the superseded session that a ``/clear`` rotated it away.

    Posts three best-effort events to the OLD conversation, in order:

    1. An ``external_session_status: idle`` so the old conversation's
       "Working…" spinner stops — its terminal moved to the new session,
       so it will never receive the turn-end edge that would normally
       clear it.
    2. A persisted assistant ``message`` item linking to the new
       conversation, so a later reload of the cleared conversation
       explains what happened and offers the continuation link. This is
       the durable record — it survives reconnects.
    3. A transient ``external_session_superseded`` event the server
       republishes as ``session.superseded``, so a client *actively*
       viewing the old conversation auto-redirects to the new one.

    Each failure is logged and swallowed: the rotation has already
    completed and reset forwarder state, and a notification error must
    not disrupt the poll loop or stop the new session from forwarding.

    :param client: Omnigent HTTP client (``base_url`` = AP server).
    :param old_session_id: Superseded conversation id, e.g. ``"conv_old"``.
    :param new_session_id: Rotated-to conversation id, e.g. ``"conv_new"``.
    :param agent_name: Agent name to stamp on the notice message — an
        assistant ``message`` item requires one.
    :returns: None.
    """
    if old_session_id == new_session_id:
        # Defensive: never address the notice/redirect at the live session.
        # The caller resolves the old id from the pre-rotation forwarder
        # state, but if that ever collapses to the new id, posting here
        # would dump the "you were cleared" banner onto the active chat.
        return
    try:
        status_resp = await client.post(
            f"/v1/sessions/{url_component(old_session_id)}/events",
            json={
                "type": "external_session_status",
                "data": {"status": "idle"},
            },
        )
        status_resp.raise_for_status()
    except httpx.HTTPError:
        _logger.warning(
            "Failed to post /clear supersession idle status; old_session=%s new_session=%s",
            old_session_id,
            new_session_id,
            exc_info=True,
        )
    notice = (
        "This conversation was ended by `/clear`. "
        f"Continue in [the new chat](/c/{new_session_id}). "
        "You can also send a message here to resume this conversation."
    )
    try:
        item_resp = await client.post(
            f"/v1/sessions/{url_component(old_session_id)}/events",
            json={
                "type": "external_conversation_item",
                "data": {
                    "item_type": "message",
                    "item_data": {
                        "role": "assistant",
                        "agent": agent_name,
                        "content": [{"type": "output_text", "text": notice}],
                    },
                },
            },
        )
        item_resp.raise_for_status()
    except httpx.HTTPError:
        _logger.warning(
            "Failed to post /clear supersession notice; old_session=%s new_session=%s",
            old_session_id,
            new_session_id,
            exc_info=True,
        )
    try:
        event_resp = await client.post(
            f"/v1/sessions/{url_component(old_session_id)}/events",
            json={
                "type": "external_session_superseded",
                "data": {"target_conversation_id": new_session_id},
            },
        )
        event_resp.raise_for_status()
    except httpx.HTTPError:
        _logger.warning(
            "Failed to post /clear supersession redirect event; old_session=%s new_session=%s",
            old_session_id,
            new_session_id,
            exc_info=True,
        )


async def _post_external_conversation_item(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    item: ClaudeTranscriptItem,
) -> None:
    """
    Post one mirrored transcript item to the Sessions API.

    :param client: Omnigent HTTP client.
    :param session_id: Omnigent session/conversation id.
    :param item: Transcript-derived conversation item.
    :returns: None.
    :raises httpx.HTTPError: If the Omnigent request fails or is rejected.
    """
    resp = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "external_conversation_item",
            "data": {
                "item_type": item.item_type,
                "item_data": item.data,
                "response_id": item.response_id,
            },
        },
    )
    resp.raise_for_status()


async def _post_external_output_text_delta(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    delta: ClaudeMessageDelta,
) -> None:
    """
    Post one streamed assistant-text chunk to the Sessions API.

    Published as a transient ``response.output_text.delta`` SSE event
    (no persistence). ``message_id``/``index``/``final`` let the web UI
    scope an in-flight buffer per message, order chunks, and know when
    the live stream for a message ends; the authoritative final text
    still arrives separately via ``external_conversation_item``.

    :param client: Omnigent HTTP client.
    :param session_id: Omnigent session/conversation id.
    :param delta: Parsed streamed chunk.
    :returns: None.
    :raises httpx.HTTPError: If the Omnigent request fails or is rejected.
    """
    resp = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "external_output_text_delta",
            "data": {
                "delta": delta.delta,
                "message_id": delta.message_id,
                "index": delta.index,
                "final": delta.final,
            },
        },
    )
    resp.raise_for_status()


async def _forward_available_deltas(
    *,
    client: httpx.AsyncClient,
    session_id: str,
    bridge_dir: Path,
    state: DeltaForwardState,
    seen_keys: dict[tuple[str, int], None],
    ordering: _DeltaOrderingState | None = None,
) -> DeltaForwardState:
    """
    Forward newly appended assistant-text deltas to the active session.

    Reads complete records appended to ``message_deltas.jsonl`` after
    the current byte offset and publishes each as a transient
    ``external_output_text_delta``. Deltas are best-effort live preview:
    a per-chunk POST failure is logged and dropped (the authoritative
    final message still arrives via ``external_conversation_item``)
    rather than retried, so a transient blip can never wedge the tail.

    :param client: Omnigent HTTP client.
    :param session_id: Omnigent session/conversation id deltas are forwarded
        to — the currently active session, so chunks streamed after a
        ``/clear`` land on the rotated session.
    :param bridge_dir: Native Claude bridge directory.
    :param state: Current delta cursor state.
    :param seen_keys: In-memory ``(message_id, index)`` dedupe ring,
        mutated in place. Guards the rare file-truncation rewind where
        the reader restarts from offset ``0``.
    :param ordering: Delta-ordering state, mutated in place: each
        forwarded chunk's text accumulates under its ``message_id`` for
        :func:`_hold_assistant_item_for_deltas` to byte-match. Accumulated
        on read, not POST success — a dropped chunk should let the item
        post, not wait on text that never completes. ``None`` disables it.
    :returns: The updated delta cursor state (offset advanced past the
        records just read).
    """
    # The deltas file only exists once the MessageDisplay hook has fired
    # for this Claude process. Skip the worker-thread read until then so
    # idle / non-streaming polls don't churn the thread pool (this loop
    # polls every ~0.25s). A bare ``exists()`` is a cheap stat consistent
    # with the other sync reads this loop already does each poll.
    if not (bridge_dir / MESSAGE_DELTAS_FILE).exists():
        return state
    result = await asyncio.to_thread(
        read_message_deltas_from_offset, bridge_dir, state.byte_offset
    )
    if result.byte_offset == state.byte_offset and not result.deltas:
        return state
    for delta in result.deltas:
        key = (delta.message_id, delta.index)
        if key in seen_keys:
            continue
        seen_keys[key] = None
        # Bound the dedupe ring by evicting the oldest key (dicts are
        # insertion-ordered) so a very long session can't grow it without
        # limit.
        while len(seen_keys) > _MAX_SEEN_DELTA_KEYS:
            del seen_keys[next(iter(seen_keys))]
        if ordering is not None:
            entry = ordering.texts.setdefault(delta.message_id, _ForwardedDeltaText())
            entry.parts.append(delta.delta)
            if delta.final:
                entry.final = True
            while len(ordering.texts) > _MAX_DELTA_ORDERING_ENTRIES:
                del ordering.texts[next(iter(ordering.texts))]
        try:
            await _post_external_output_text_delta(client, session_id=session_id, delta=delta)
        except httpx.HTTPError as exc:
            _logger.debug(
                "Dropping Claude streamed delta after HTTP failure; session=%s "
                "bridge_dir=%s message_id=%s index=%s http_status=%s",
                session_id,
                bridge_dir,
                delta.message_id,
                delta.index,
                _http_status_for_log(exc),
            )
    updated = DeltaForwardState(byte_offset=result.byte_offset)
    await _write_delta_forward_state_async(bridge_dir, updated)
    return updated


async def _post_external_session_usage(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    usage: Mapping[str, float | str] | None,
    context_window: int | None = None,
) -> None:
    """
    Post one ``external_session_usage`` event to the Sessions API.

    At least one of ``usage`` / ``context_window`` must be set; a
    payload with neither is a no-op (the server would 400 it).

    :param client: Omnigent HTTP client.
    :param session_id: Omnigent session/conversation id.
    :param usage: ``message.usage`` snapshot, or ``None`` to skip. Values are
        numeric counters/costs, plus an optional ``model`` string tagging the
        cost with the active model for per-model attribution.
    :param context_window: Resolved window in tokens, or ``None`` to
        leave the server's persisted value untouched.
    :raises httpx.HTTPError: If the Omnigent request fails or is rejected.
    """
    payload: dict[str, Any] = {}
    if usage is not None:
        payload.update(usage)
    if context_window is not None:
        payload["context_window"] = context_window
    if not payload:
        return
    resp = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={"type": "external_session_usage", "data": payload},
    )
    resp.raise_for_status()


def _model_alias_for(model: str | None) -> str | None:
    """
    Collapse a concrete Claude model id to the picker's tier alias.

    The web model picker speaks Claude Code's version-agnostic aliases
    (``"fable"`` / ``"opus"`` / ``"sonnet"`` / ``"haiku"``); the
    transcript records the resolved concrete id (e.g.
    ``"claude-opus-4-8"`` or ``"databricks-claude-sonnet-4-6"``).
    Mapping to the tier keeps the mirrored value in the picker's
    vocabulary and makes a web→TUI round-trip a no-op.

    :param model: Concrete model id from the transcript, e.g.
        ``"claude-opus-4-8"``; ``None`` when none observed yet.
    :returns: ``"fable"`` / ``"opus"`` / ``"sonnet"`` / ``"haiku"``
        when the id carries a known tier token, else ``None`` (the
        caller skips the post rather than surface an id the picker
        can't render).
    """
    if not model:
        return None
    lowered = model.lower()
    for tier in ("fable", "opus", "sonnet", "haiku"):
        if tier in lowered:
            return tier
    return None


async def _post_external_model_change(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    model: str,
) -> None:
    """
    Post one ``external_model_change`` event to the Sessions API.

    Lets the web model picker reflect a model switch made inside the
    Claude Code terminal (a ``/model`` command or the in-TUI picker).

    :param client: Omnigent HTTP client.
    :param session_id: Omnigent session/conversation id, e.g.
        ``"conv_abc123"``.
    :param model: Tier alias the session is now on, e.g. ``"opus"``.
    :raises httpx.HTTPError: If the Omnigent request fails or is rejected.
    """
    resp = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={"type": "external_model_change", "data": {"model": model}},
    )
    resp.raise_for_status()


async def _post_model_change_if_new(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    dedupe: _ForwardDedupeState,
    alias: str | None,
) -> None:
    """
    Mirror an observed model tier alias to ``model_override``, deduped.

    Shared by the transcript-driven path (:func:`_forward_available_items`)
    and the statusLine-driven per-poll path
    (:func:`_forward_model_from_status`). The FIRST observation is the
    session's spawn default, not a switch, so it seeds the dedupe baseline
    WITHOUT posting (posting it could clobber a pending silent model
    handoff). Every later change posts ``external_model_change``. Both
    callers pass the same ``dedupe`` so whichever observes a switch first
    posts it and the other no-ops. Best-effort: a failed POST leaves
    ``posted_model`` behind ``observed_model`` so the next poll retries.

    :param client: Omnigent HTTP client.
    :param session_id: Omnigent session/conversation id.
    :param dedupe: Shared per-session dedupe state; mutated in place.
    :param alias: Tier alias just observed (``"opus"`` / ``"sonnet"`` /
        …), or ``None`` when this source carried no recognizable model on
        this poll. ``observed_model`` is sticky across polls, so passing
        ``None`` does NOT clear it — it just means "no fresh observation,"
        and a previously-observed-but-unposted model is still reconciled
        (retried) here.
    """
    if alias is not None:
        dedupe.observed_model = alias
    if dedupe.observed_model is None or dedupe.observed_model == dedupe.posted_model:
        return
    if dedupe.posted_model is None:
        # First observation = the spawn default; seed the baseline without
        # posting so it can't clobber a pending silent model handoff.
        dedupe.posted_model = dedupe.observed_model
        return
    try:
        await _post_external_model_change(
            client,
            session_id=session_id,
            model=dedupe.observed_model,
        )
        dedupe.posted_model = dedupe.observed_model
    except httpx.HTTPError:
        # Leave posted_model behind observed_model so the next poll retries.
        _logger.warning(
            "Failed to mirror model change to Omnigent session=%s; model pill / "
            "cost-budget gate may lag until the next poll",
            session_id,
            exc_info=True,
        )


async def _forward_model_from_status(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    bridge_dir: Path,
    dedupe: _ForwardDedupeState,
) -> None:
    """
    Mirror the statusLine-reported active model to ``model_override`` each poll.

    Claude Code rewrites the statusLine stdin on every TUI render — including
    right after an in-pane ``/model`` switch, BEFORE the next turn runs. The
    wrapper (:mod:`omnigent.claude_native_status`) persists that model into
    ``context.json``. Reading it here, every poll and independently of new
    transcript items, is what lets a policy that gates on the active model
    (e.g. the session cost-budget hard cap, which only blocks expensive
    tiers) see the new model on the user's NEXT message — instead of one
    turn later, which is what happened when the model was derived solely
    from the next turn's transcript ``message.model``.

    Best-effort and idempotent: shares ``dedupe`` with the transcript path,
    so a no-op when the model is unchanged.

    :param client: Omnigent HTTP client.
    :param session_id: Omnigent session/conversation id.
    :param bridge_dir: Native Claude bridge directory.
    :param dedupe: Shared per-session model dedupe state.
    """
    status_state = await asyncio.to_thread(read_claude_context_state, bridge_dir)
    if status_state is None:
        return
    model = status_state.get("model")
    alias = _model_alias_for(model if isinstance(model, str) else None)
    await _post_model_change_if_new(
        client,
        session_id=session_id,
        dedupe=dedupe,
        alias=alias,
    )


async def _post_external_session_status(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    status: str,
) -> None:
    """
    Post one ``external_session_status`` event to the Sessions API.

    :param client: Omnigent HTTP client.
    :param session_id: Omnigent session/conversation id.
    :param status: Session status value, e.g. ``"idle"`` or
        ``"failed"``.
    :returns: None.
    :raises httpx.HTTPError: If the Omnigent request fails or is rejected.
    """
    resp = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "external_session_status",
            "data": {"status": status},
        },
    )
    resp.raise_for_status()


async def _post_external_compaction_status(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    status: str,
) -> None:
    """
    Post one ``external_compaction_status`` event to the Sessions API.

    Brackets Claude Code's own compaction so the web UI can show its
    "Compacting conversation…" spinner while Claude runs the real
    compaction in the terminal. ``"in_progress"`` is sent from the
    ``PreCompact`` hook and ``"completed"`` from the post-compaction
    ``SessionStart`` (``source == "compact"``) hook. The Omnigent server maps
    these to the ``response.compaction.in_progress`` /
    ``response.compaction.completed`` SSE events the web client already
    renders.

    :param client: Omnigent HTTP client.
    :param session_id: Omnigent session/conversation id.
    :param status: Compaction status value, ``"in_progress"`` or
        ``"completed"``.
    :returns: None.
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
    bridge_dir: Path,
) -> None:
    """
    Persist a compaction boundary item to the conversation store.

    Called when the forwarder observes a compaction-completed signal
    (``SessionStart source=compact``). Queries the latest conversation
    item to use as ``last_item_id`` so session resume knows the
    compaction boundary — items before this marker are summarized
    and don't need to be loaded.

    After writing the boundary, it also reads the post-compaction
    transcript from Claude's own session state via
    ``get_session_messages`` and includes them as ``compacted_messages``
    so session resume in ephemeral environments can reconstruct context
    without the CLI's local transcript files.

    :param client: Omnigent HTTP client.
    :param session_id: Omnigent session/conversation id.
    :param bridge_dir: Bridge directory path used to look up the
        Claude-native session id.
    """
    # Find the last persisted item to use as the compaction boundary.
    resp = await client.get(
        f"/v1/sessions/{session_id}/items",
        params={"limit": 1, "order": "desc"},
    )
    resp.raise_for_status()
    items = resp.json().get("data", [])
    last_item_id = items[0]["id"] if items else f"compact_boundary_{session_id}"

    # Read the post-compaction session messages so session resume can
    # reconstruct context in ephemeral environments.
    compacted_messages: list[dict[str, Any]] | None = None
    try:
        from claude_agent_sdk import get_session_messages

        claude_sid = read_claude_session_id(bridge_dir)
        if claude_sid:
            msgs = get_session_messages(claude_sid)
            compacted_messages = [
                {"type": "message", "role": m.type, "content": m.message.get("content", [])}
                for m in msgs
                if isinstance(m.message, dict)
            ]
    except Exception:  # noqa: BLE001
        _logger.debug(
            "Failed to read Claude session messages for compaction persist",
            exc_info=True,
        )

    event_data: dict[str, Any] = {
        "summary": "[Claude Code compaction — context was compacted in the terminal]",
        "last_item_id": last_item_id,
        "model": "unknown",
        "token_count": 0,
    }
    if compacted_messages is not None:
        event_data["compacted_messages"] = compacted_messages

    resp = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "compaction",
            "data": event_data,
        },
    )
    resp.raise_for_status()


async def _patch_external_session_id(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    external_session_id: str,
) -> None:
    """
    PATCH the Omnigent conversation row with the Claude-native session id.

    The server's ``set_external_session_id`` store call is idempotent
    on same-value writes and rejects overwrite of an already-set
    different value with ``400 invalid_input``. Wrapper bridges should
    PATCH the value once when they first observe it from Claude.

    :param client: Omnigent HTTP client.
    :param session_id: Omnigent session/conversation id, e.g.
        ``"conv_abc123"``.
    :param external_session_id: Runtime-native session id captured
        from a Claude hook event,
        e.g. ``"a1b2c3d4-1234-5678-9abc-def012345678"``.
    :returns: None.
    :raises httpx.HTTPError: If the Omnigent request fails or is rejected.
    """
    resp = await client.patch(
        f"/v1/sessions/{session_id}",
        json={"external_session_id": external_session_id},
    )
    resp.raise_for_status()


async def _maybe_sync_effort_from_slash_command(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    item: ClaudeTranscriptItem,
) -> None:
    """
    Mirror an in-pane ``/effort`` change onto the Omnigent session row.

    The pane changes the binary but doesn't touch AP; PATCH
    ``reasoning_effort`` (``silent=True`` to avoid re-injecting ``/effort``
    into the pane) so the pill tracks it. Best-effort — logged, not raised.

    :param client: Omnigent HTTP client.
    :param session_id: Omnigent session/conversation id, e.g. ``"conv_abc123"``.
    :param item: A just-forwarded item; only a ``slash_command`` named
        ``"effort"`` triggers a PATCH.
    :returns: None.
    """
    if item.item_type != "slash_command" or item.data.get("name") != "effort":
        return
    arguments = item.data.get("arguments")
    if not isinstance(arguments, str):
        return
    # Bare level (set) or clear alias changes state; bare /effort is a show no-op.
    level = arguments.strip().lower()
    if level not in CLAUDE_EFFORTS and level not in EFFORT_CLEAR_VALUES:
        return
    try:
        resp = await client.patch(
            f"/v1/sessions/{session_id}",
            json={"reasoning_effort": level, "silent": True},
        )
        resp.raise_for_status()
    except httpx.HTTPError:
        _logger.warning(
            "Failed to mirror in-pane /effort=%s to Omnigent session=%s; "
            "effort pill may lag until the next change",
            level,
            session_id,
            exc_info=True,
        )


async def _post_forwarder_failed_status(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    bridge_dir: Path,
    reason: str,
) -> None:
    """
    Best-effort publish a failed status after dropping a poison event.

    :param client: Omnigent HTTP client.
    :param session_id: Omnigent session/conversation id.
    :param bridge_dir: Native Claude bridge directory.
    :param reason: Diagnostic reason for the failure event, e.g.
        ``"transcript item item-1 rejected"``.
    :returns: None.
    """
    try:
        await _post_external_session_status(client, session_id=session_id, status="failed")
    except httpx.HTTPError:
        _logger.warning(
            "Failed to publish Claude forwarder failure status; "
            "session=%s bridge_dir=%s reason=%s",
            session_id,
            bridge_dir,
            reason,
            exc_info=True,
        )


async def _post_external_session_todos(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    todos: list[dict[str, Any]],
) -> None:
    """
    Post one ``external_session_todos`` event to the Sessions API.

    :param client: Omnigent HTTP client.
    :param session_id: Omnigent session/conversation id, e.g. ``"conv_abc123"``.
    :param todos: Current Claude todo list, e.g.
        ``[{"content": "Write tests", "status": "in_progress",
        "activeForm": "Writing tests"}]``.
    :returns: None.
    :raises httpx.HTTPError: If the Omnigent request fails or is rejected.
    """
    resp = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={"type": "external_session_todos", "data": {"todos": todos}},
    )
    resp.raise_for_status()


def _is_permanent_http_error(exc: httpx.HTTPError) -> bool:
    """
    Return whether ``exc`` is a permanent Omnigent rejection.

    :param exc: HTTP exception raised while posting an Omnigent event.
    :returns: ``True`` for non-transient 4xx status responses,
        otherwise ``False``.
    """
    if not isinstance(exc, httpx.HTTPStatusError):
        return False
    status_code = exc.response.status_code
    return 400 <= status_code < 500 and status_code not in _HTTP_TRANSIENT_STATUS_CODES


def _is_subagent_delivery_not_confirmed(exc: httpx.HTTPError) -> bool:
    """
    Return whether ``exc`` is a runner ``subagent_delivery_not_confirmed`` 503.

    The runner returns this application-level 503 when a terminal sub-agent
    payload could not be delivered to the parent inbox (no work entry / inbox).
    It is a bounded-retry class, distinct from a generic transient 5xx.

    :param exc: HTTP exception raised while posting an Omnigent event.
    :returns: ``True`` only for a 503 whose JSON body carries
        ``error == "subagent_delivery_not_confirmed"``.
    """
    if not isinstance(exc, httpx.HTTPStatusError):
        return False
    if exc.response.status_code != 503:
        return False
    try:
        body = exc.response.json()
    except Exception:  # noqa: BLE001 — best-effort body parse
        return False
    return isinstance(body, dict) and body.get("error") == "subagent_delivery_not_confirmed"


def _http_status_for_log(exc: httpx.HTTPError) -> int | None:
    """
    Extract an HTTP status code from ``exc`` when present.

    :param exc: HTTP exception raised while posting an Omnigent event.
    :returns: Numeric HTTP status code, or ``None`` for transport
        failures that did not receive a response.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code
    return None


def _read_hook_state(bridge_dir: Path) -> HookForwardState | None:
    """
    Read the durable hook forwarder cursor from the bridge directory.

    :param bridge_dir: Native Claude bridge directory.
    :returns: Cursor state, or ``None`` if no usable state exists.
    """
    try:
        raw = json.loads((bridge_dir / _HOOK_STATE_FILE).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(raw, dict):
        return None
    event_cursor = raw.get("event_cursor")
    byte_offset = raw.get("byte_offset")
    cursor_fingerprint = raw.get("cursor_fingerprint")
    if not isinstance(event_cursor, int) or event_cursor < 0:
        return None
    if byte_offset is not None and (not isinstance(byte_offset, int) or byte_offset < 0):
        return None
    if cursor_fingerprint is not None and not isinstance(cursor_fingerprint, str):
        return None
    return HookForwardState(
        event_cursor=event_cursor,
        byte_offset=byte_offset,
        cursor_fingerprint=cursor_fingerprint,
    )


def _write_hook_state(bridge_dir: Path, state: HookForwardState) -> None:
    """
    Write the durable hook forwarder cursor to the bridge directory.

    :param bridge_dir: Native Claude bridge directory.
    :param state: Cursor state to persist.
    :returns: None.
    """
    bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "event_cursor": state.event_cursor,
        "updated_at": time.time(),
    }
    if state.byte_offset is not None:
        payload["byte_offset"] = state.byte_offset
    if state.cursor_fingerprint is not None:
        payload["cursor_fingerprint"] = state.cursor_fingerprint
    _write_json_atomic(bridge_dir / _HOOK_STATE_FILE, payload)


async def _write_hook_state_async(bridge_dir: Path, state: HookForwardState) -> None:
    """
    Persist hook state without blocking the asyncio event loop.

    :param bridge_dir: Native Claude bridge directory.
    :param state: Cursor state to persist.
    :returns: None.
    """
    await asyncio.to_thread(_write_hook_state, bridge_dir, state)


def _usage_from_status_state(state: dict[str, Any]) -> dict[str, float] | None:
    """
    Convert statusLine ``current_usage`` (+ cost) into the Omnigent usage shape.

    Sums input + cache_creation + cache_read for ``context_tokens``
    (matches claude-hud's ``getTotalTokens``: only input-side tokens
    occupy the next prompt's budget). When the statusLine also captured
    Claude Code's cumulative ``total_cost_usd``, it's surfaced as
    ``cumulative_cost_usd`` so the server can persist native session cost
    (SET semantics). Returns ``None`` when the state has no usable
    ``current_usage`` so the caller falls back to the JSONL-derived value.

    :param state: Parsed ``context.json`` payload.
    :returns: Usage dict (token counts plus optional
        ``cumulative_cost_usd``), or ``None``.
    """
    usage = state.get("current_usage")
    if not isinstance(usage, dict):
        return None
    input_tokens = usage.get("input_tokens")
    if not isinstance(input_tokens, int):
        return None
    cc = usage.get("cache_creation_input_tokens")
    cr = usage.get("cache_read_input_tokens")
    output_tokens = usage.get("output_tokens")
    cc_i = cc if isinstance(cc, int) else 0
    cr_i = cr if isinstance(cr, int) else 0
    out_i = output_tokens if isinstance(output_tokens, int) else 0
    # Token counts stay ``int`` (the server validates context_tokens with
    # ``isinstance(int)``); only ``cumulative_cost_usd`` is a float. ``float``
    # annotation is fine — ``int`` is a subtype under the numeric tower.
    result: dict[str, float] = {
        "context_tokens": input_tokens + cc_i + cr_i,
        "input_tokens": input_tokens,
        "output_tokens": out_i,
    }
    total_cost = state.get("total_cost_usd")
    if (
        isinstance(total_cost, (int, float))
        and not isinstance(total_cost, bool)
        and total_cost >= 0
    ):
        result["cumulative_cost_usd"] = float(total_cost)
    return result


def _bounded_seen_source_ids(seen_source_ids: list[str]) -> tuple[str, ...]:
    """
    Return a bounded tuple of recently forwarded source ids.

    :param seen_source_ids: Source ids accumulated in observation
        order.
    :returns: Tuple capped to the most recent source ids. The cap
        prevents the state file from growing without bound while
        retaining enough idempotency history for retries.
    """
    return tuple(seen_source_ids[-_MAX_SEEN_SOURCE_IDS:])


def _read_forward_state(bridge_dir: Path) -> TranscriptForwardState | None:
    """
    Read the durable forwarder cursor from the bridge directory.

    :param bridge_dir: Native Claude bridge directory.
    :returns: Cursor state, or ``None`` if no usable state exists.
    """
    try:
        raw = json.loads((bridge_dir / _FORWARDER_STATE_FILE).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(raw, dict):
        return None
    transcript_path = raw.get("transcript_path")
    line_cursor = raw.get("line_cursor")
    byte_offset = raw.get("byte_offset")
    current_response_id = raw.get("current_response_id")
    cursor_fingerprint = raw.get("cursor_fingerprint")
    seen_source_ids = raw.get("seen_source_ids", [])
    if not isinstance(transcript_path, str) or not isinstance(line_cursor, int):
        return None
    if line_cursor < 0:
        return None
    if byte_offset is not None and (not isinstance(byte_offset, int) or byte_offset < 0):
        return None
    if current_response_id is not None and not isinstance(current_response_id, str):
        return None
    if cursor_fingerprint is not None and not isinstance(cursor_fingerprint, str):
        return None
    if not isinstance(seen_source_ids, list) or not all(
        isinstance(source_id, str) for source_id in seen_source_ids
    ):
        seen_source_ids = []
    return TranscriptForwardState(
        transcript_path=Path(transcript_path),
        line_cursor=line_cursor,
        byte_offset=byte_offset,
        current_response_id=current_response_id,
        seen_source_ids=tuple(seen_source_ids),
        cursor_fingerprint=cursor_fingerprint,
    )


def _write_forward_state(bridge_dir: Path, state: TranscriptForwardState) -> None:
    """
    Write the durable forwarder cursor to the bridge directory.

    :param bridge_dir: Native Claude bridge directory.
    :param state: Cursor state to persist.
    :returns: None.
    """
    bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "transcript_path": str(state.transcript_path),
        "line_cursor": state.line_cursor,
        "current_response_id": state.current_response_id,
        "seen_source_ids": list(state.seen_source_ids),
        "updated_at": time.time(),
    }
    if state.byte_offset is not None:
        payload["byte_offset"] = state.byte_offset
    if state.cursor_fingerprint is not None:
        payload["cursor_fingerprint"] = state.cursor_fingerprint
    _write_json_atomic(bridge_dir / _FORWARDER_STATE_FILE, payload)


async def _write_forward_state_async(
    bridge_dir: Path,
    state: TranscriptForwardState,
) -> None:
    """
    Persist transcript state without blocking the asyncio event loop.

    :param bridge_dir: Native Claude bridge directory.
    :param state: Cursor state to persist.
    :returns: None.
    """
    await asyncio.to_thread(_write_forward_state, bridge_dir, state)


def _read_delta_forward_state(bridge_dir: Path) -> DeltaForwardState:
    """
    Read the durable delta-forwarder cursor from the bridge directory.

    :param bridge_dir: Native Claude bridge directory.
    :returns: Persisted cursor, or a fresh ``byte_offset=0`` state when
        none exists or it is unusable. Starting from ``0`` re-reads the
        deltas file; the ``(message_id, index)`` dedupe ring and the
        frontend's own provisional buffer absorb any re-sent chunks.
    """
    try:
        raw = json.loads((bridge_dir / _DELTA_STATE_FILE).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return DeltaForwardState()
    if not isinstance(raw, dict):
        return DeltaForwardState()
    byte_offset = raw.get("byte_offset")
    if not isinstance(byte_offset, int) or byte_offset < 0:
        return DeltaForwardState()
    return DeltaForwardState(byte_offset=byte_offset)


def _write_delta_forward_state(bridge_dir: Path, state: DeltaForwardState) -> None:
    """
    Write the durable delta-forwarder cursor to the bridge directory.

    :param bridge_dir: Native Claude bridge directory.
    :param state: Cursor state to persist.
    :returns: None.
    """
    bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write_json_atomic(
        bridge_dir / _DELTA_STATE_FILE,
        {"byte_offset": state.byte_offset, "updated_at": time.time()},
    )


async def _write_delta_forward_state_async(
    bridge_dir: Path,
    state: DeltaForwardState,
) -> None:
    """
    Persist delta state without blocking the asyncio event loop.

    :param bridge_dir: Native Claude bridge directory.
    :param state: Cursor state to persist.
    :returns: None.
    """
    await asyncio.to_thread(_write_delta_forward_state, bridge_dir, state)


def _transcript_end_offset(transcript_path: Path) -> int:
    """
    Return the byte offset after the last complete transcript record.

    :param transcript_path: Claude transcript path.
    :returns: Offset after the last newline-terminated record, or
        ``0`` when the transcript does not exist or has only a
        partial first record.
    """
    return _complete_jsonl_end_offset(transcript_path)


def _hook_end_offset(bridge_dir: Path) -> int:
    """
    Return the byte offset after the last complete hook JSONL record.

    :param bridge_dir: Native Claude bridge directory.
    :returns: Offset after the last newline-terminated hook record, or
        ``0`` when no complete hook record exists yet.
    """
    return _complete_jsonl_end_offset(bridge_dir / _HOOKS_FILE)


def _complete_jsonl_end_offset(path: Path) -> int:
    """
    Return the offset after the last newline-terminated JSONL record.

    :param path: JSONL file path.
    :returns: File size when it ends in ``"\\n"``, otherwise the byte
        offset immediately after the previous newline. Returns ``0``
        for missing files or a single partial first record.
    """
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            if size == 0:
                return 0
            handle.seek(size - 1)
            if handle.read(1) == b"\n":
                return size
            block_end = size
            while block_end > 0:
                block_start = max(0, block_end - 65_536)
                handle.seek(block_start)
                data = handle.read(block_end - block_start)
                newline_index = data.rfind(b"\n")
                if newline_index >= 0:
                    return block_start + newline_index + 1
                block_end = block_start
    except FileNotFoundError:
        return 0
    return 0


def _jsonl_cursor_fingerprint(path: Path, byte_offset: int) -> str | None:
    """
    Hash bytes immediately before a JSONL cursor for stale-cursor checks.

    :param path: JSONL file path.
    :param byte_offset: Cursor byte offset, e.g. ``4096``.
    :returns: SHA-256 digest for the bytes before the cursor, or
        ``None`` when the file does not exist or the offset is invalid.
    """
    if byte_offset < 0:
        return None
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            if byte_offset > size:
                return None
            sample_start = max(0, byte_offset - _CURSOR_FINGERPRINT_BYTES)
            handle.seek(sample_start)
            sample = handle.read(byte_offset - sample_start)
    except FileNotFoundError:
        return None
    payload = byte_offset.to_bytes(8, "big", signed=False) + sample
    return hashlib.sha256(payload).hexdigest()


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """
    Write JSON to *path* via a same-directory temporary file.

    :param path: Destination JSON file.
    :param payload: JSON-serializable payload.
    :returns: None.
    """
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
            handle.write(json.dumps(payload, separators=(",", ":")))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    finally:
        if tmp_path is not None:
            with contextlib.suppress(FileNotFoundError):
                tmp_path.unlink()
