"""Compaction recovery tests for Claude-native forwarding."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import omnigent.harnesses.claude_native.forwarder as forwarder
from omnigent.harnesses.claude_native.bridge import (
    ClaudeTranscriptItem,
    record_hook_event,
)
from tests.harnesses.claude_native.forwarder._support import (
    _get_recorded_request,
    _start_recording_server,
)

# ---------------------------------------------------------------------------
# _persist_native_compaction_item tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persist_native_compaction_item_posts_compaction_event(tmp_path: Path) -> None:
    """
    ``_persist_native_compaction_item`` queries the latest item and posts a compaction event.

    The function GETs ``/v1/sessions/{id}/items?limit=1&order=desc`` to
    find the most recent persisted item, reads post-compaction messages
    from the Claude session, then POSTs a ``compaction`` event using
    that item's id as ``last_item_id`` and the messages as
    ``compacted_messages``.
    """
    get_response = MagicMock()
    get_response.raise_for_status = MagicMock()
    get_response.json.return_value = {"data": [{"id": "item_123"}]}

    post_response = MagicMock()
    post_response.raise_for_status = MagicMock()

    client = AsyncMock()
    client.get.return_value = get_response
    client.post.return_value = post_response

    # Build a fake message returned by get_session_messages.
    fake_msg = MagicMock()
    fake_msg.type = "assistant"
    fake_msg.message = {"content": [{"type": "text", "text": "hello"}]}

    bridge_dir = tmp_path / "bridge"

    with (
        patch(
            "omnigent.harnesses.claude_native.forwarder.read_claude_session_id",
            return_value="claude-uuid-1",
        ),
        patch(
            "claude_agent_sdk.get_session_messages",
            return_value=[fake_msg],
        ),
    ):
        await forwarder._persist_native_compaction_item(
            client, session_id="conv_test", bridge_dir=bridge_dir
        )

    client.get.assert_called_once_with(
        "/v1/sessions/conv_test/items",
        params={"limit": 1, "order": "desc"},
    )
    client.post.assert_called_once()
    post_call = client.post.call_args
    assert post_call[0][0] == "/v1/sessions/conv_test/events"
    body = post_call[1]["json"] if "json" in post_call[1] else post_call[0][1]
    assert body["type"] == "compaction"
    assert body["data"]["last_item_id"] == "item_123"
    assert body["data"]["summary"] is not None
    assert body["data"]["model"] == "unknown"
    assert body["data"]["token_count"] == 0
    # compacted_messages should contain the converted fake message.
    assert body["data"]["compacted_messages"] == [
        {"type": "message", "role": "assistant", "content": [{"type": "text", "text": "hello"}]},
    ]


@pytest.mark.asyncio
async def test_persist_native_compaction_item_empty_items_uses_fallback(tmp_path: Path) -> None:
    """
    When no items exist, ``last_item_id`` falls back to a generated boundary id.

    If the session has no persisted items yet (e.g. the very first turn
    was compacted before anything was stored), the function generates
    ``compact_boundary_{session_id}`` as the boundary marker instead of
    crashing on an empty list.
    """
    get_response = MagicMock()
    get_response.raise_for_status = MagicMock()
    get_response.json.return_value = {"data": []}

    post_response = MagicMock()
    post_response.raise_for_status = MagicMock()

    client = AsyncMock()
    client.get.return_value = get_response
    client.post.return_value = post_response

    bridge_dir = tmp_path / "bridge"

    with (
        patch(
            "omnigent.harnesses.claude_native.forwarder.read_claude_session_id",
            return_value=None,
        ),
    ):
        await forwarder._persist_native_compaction_item(
            client, session_id="conv_empty", bridge_dir=bridge_dir
        )

    post_call = client.post.call_args
    body = post_call[1]["json"] if "json" in post_call[1] else post_call[0][1]
    assert body["data"]["last_item_id"].startswith("compact_boundary_")
    # No compacted_messages when claude_sid is None.
    assert "compacted_messages" not in body["data"]


@pytest.mark.asyncio
async def test_compaction_completed_triggers_persist(tmp_path: Path) -> None:
    """
    ``SessionStart source=compact`` triggers both status POST and item persistence.

    When the forwarder processes a ``SessionStart source=compact`` record
    (compaction completed), it must call ``_post_external_compaction_status``
    to surface the status AND ``_persist_native_compaction_item`` to write
    the compaction boundary item.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    # Initial SessionStart populates transcript_path.
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "claude-session",
            "transcript_path": str(transcript_path),
        },
    )
    # PreCompact mints the pending token the completion signal consumes.
    # A real compaction always fires PreCompact before the compact
    # SessionStart; the hook path only persists when that token exists.
    record_hook_event(
        bridge_dir,
        {"hook_event_name": "PreCompact", "session_id": "claude-session"},
    )
    # Post-compaction SessionStart — the completion signal.
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "source": "compact",
            "session_id": "claude-session",
        },
    )
    server, thread, base_url = _start_recording_server()
    persist_called = asyncio.Event()

    async def _persist_side_effect(*args: Any, **kwargs: Any) -> None:
        persist_called.set()

    persist_mock = AsyncMock(side_effect=_persist_side_effect)
    with patch(
        "omnigent.harnesses.claude_native.forwarder._persist_native_compaction_item",
        persist_mock,
    ):
        task = asyncio.create_task(
            forwarder.forward_claude_transcript_to_session(
                base_url=base_url,
                headers={},
                session_id="conv_persist",
                bridge_dir=bridge_dir,
                agent_name="claude-native-ui",
                start_at_end=False,
                poll_interval_s=0.01,
            )
        )
        try:
            # Wait for the compaction-completed status POST to arrive
            # (the leading PreCompact in_progress edge is skipped).
            request = None
            for _ in range(10):
                candidate = await _get_recorded_request(server)
                if (
                    candidate["body"].get("type") == "external_compaction_status"
                    and candidate["body"]["data"].get("status") == "completed"
                ):
                    request = candidate
                    break
            assert request is not None, "compaction-completed status was never posted"
            # Wait for _persist_native_compaction_item to be called
            # (it runs right after the POST in the same await chain).
            await asyncio.wait_for(persist_called.wait(), timeout=5.0)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            server.shutdown()
            server.server_close()
            thread.join(timeout=5.0)

    # The recording server captured the compaction-completed status POST.
    assert request["body"]["type"] == "external_compaction_status"
    assert request["body"]["data"]["status"] == "completed"
    # _persist_native_compaction_item was called with the right session id.
    persist_mock.assert_called_once()
    call_kwargs = persist_mock.call_args
    assert call_kwargs[1]["session_id"] == "conv_persist"


@pytest.mark.asyncio
async def test_compaction_in_progress_does_not_persist(tmp_path: Path) -> None:
    """
    ``PreCompact`` (in_progress) does NOT call ``_persist_native_compaction_item``.

    Only compaction *completion* (``SessionStart source=compact``) writes
    the boundary item. ``PreCompact`` merely forwards the ``in_progress``
    status so the UI shows a spinner — there is no boundary to persist yet.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "claude-session",
            "transcript_path": str(transcript_path),
        },
    )
    record_hook_event(
        bridge_dir,
        {"hook_event_name": "PreCompact", "session_id": "claude-session"},
    )
    server, thread, base_url = _start_recording_server()
    persist_mock = AsyncMock()
    with patch(
        "omnigent.harnesses.claude_native.forwarder._persist_native_compaction_item",
        persist_mock,
    ):
        task = asyncio.create_task(
            forwarder.forward_claude_transcript_to_session(
                base_url=base_url,
                headers={},
                session_id="conv_no_persist",
                bridge_dir=bridge_dir,
                agent_name="claude-native-ui",
                start_at_end=False,
                poll_interval_s=0.01,
            )
        )
        try:
            # Wait for the in_progress status POST to arrive.
            request = await _get_recorded_request(server)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            server.shutdown()
            server.server_close()
            thread.join(timeout=5.0)

    assert request["body"]["type"] == "external_compaction_status"
    assert request["body"]["data"]["status"] == "in_progress"
    # _persist_native_compaction_item must NOT be called for in_progress.
    persist_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Durable compaction-boundary reconciliation (native resume/replay fix)
# ---------------------------------------------------------------------------


def _compact_summary_item(text: str = "compaction summary text") -> ClaudeTranscriptItem:
    """
    Build a transcript item flagged as a Claude ``isCompactSummary`` record.

    :param text: The continuation-summary text carried by the item.
    :returns: A ``ClaudeTranscriptItem`` with ``is_compact_summary=True``.
    """
    return ClaudeTranscriptItem(
        source_id="summary-uuid:0:compact_summary",
        item_type="message",
        data={"role": "user", "content": [{"type": "input_text", "text": text}]},
        response_id="resp_summary",
        is_compact_summary=True,
    )


def _persist_mock() -> AsyncMock:
    """
    Build an ``AsyncMock`` standing in for ``_persist_native_compaction_item``.

    :returns: An async mock that records calls and returns ``None``.
    """
    return AsyncMock(return_value=None)


@pytest.mark.asyncio
async def test_missing_compact_session_start_still_persists_from_transcript(
    tmp_path: Path,
) -> None:
    """
    A transcript ``isCompactSummary`` record persists the boundary alone.

    Reproduces the core bug: the flaky ``SessionStart source=compact`` hook
    never fires, so only the transcript summary is available. The transcript
    path must still persist exactly one compaction boundary (carrying the
    summary text) once a ``PreCompact`` token is pending.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    await forwarder._note_precompact(
        bridge_dir, claude_session_id="claude-1", transcript_path="/t/session.jsonl"
    )

    persist = _persist_mock()
    with patch(
        "omnigent.harnesses.claude_native.forwarder._persist_native_compaction_item", persist
    ):
        handled = await forwarder._handle_compact_summary_item(
            AsyncMock(),
            session_id="conv_missing_hook",
            bridge_dir=bridge_dir,
            item=_compact_summary_item("the summary"),
            retry_tracker=forwarder._PostRetryTracker(),
        )

    assert handled is True
    persist.assert_called_once()
    assert persist.call_args[1]["session_id"] == "conv_missing_hook"
    assert persist.call_args[1]["summary_override"] == "the summary"
    # Boundary marked persisted; pending cleared.
    state = forwarder._read_compaction_state(bridge_dir)
    assert state.pending is None
    assert 1 in state.persisted_seqs


@pytest.mark.asyncio
async def test_normal_hook_after_transcript_does_not_double_persist(tmp_path: Path) -> None:
    """
    The completion hook does not re-persist a boundary the transcript wrote.

    After the transcript path persists the boundary and marks the sequence
    done, a later ``SessionStart source=compact`` hook finds no consumable
    pending token, so ``_consume_pending_compaction`` returns ``None`` and no
    second boundary is written.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    await forwarder._note_precompact(
        bridge_dir, claude_session_id="claude-1", transcript_path=None
    )

    persist = _persist_mock()
    with patch(
        "omnigent.harnesses.claude_native.forwarder._persist_native_compaction_item", persist
    ):
        # Transcript path persists first.
        await forwarder._handle_compact_summary_item(
            AsyncMock(),
            session_id="conv_dedupe",
            bridge_dir=bridge_dir,
            item=_compact_summary_item(),
            retry_tracker=forwarder._PostRetryTracker(),
        )
    assert persist.call_count == 1

    # Hook path arrives later — the token is already consumed.
    seq = await forwarder._consume_pending_compaction(
        bridge_dir, claude_session_id="claude-1", transcript_path=None
    )
    assert seq is None


@pytest.mark.asyncio
async def test_failed_boundary_post_is_retried_not_consumed(tmp_path: Path) -> None:
    """
    A hard POST failure leaves the summary unconsumed for retry.

    ``_handle_compact_summary_item`` must return ``False`` (so the caller
    holds the transcript cursor before the summary record) and must NOT mark
    the sequence persisted, so the boundary is retried on a later poll rather
    than silently lost — which would make resume reload the full history.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    await forwarder._note_precompact(
        bridge_dir, claude_session_id="claude-1", transcript_path=None
    )

    # A definitively-permanent 400 (not an ambiguous/network failure).
    request = httpx.Request("POST", "http://x/events")
    response = httpx.Response(400, request=request)
    failing = AsyncMock(
        side_effect=httpx.HTTPStatusError("bad", request=request, response=response)
    )

    with patch(
        "omnigent.harnesses.claude_native.forwarder._persist_native_compaction_item", failing
    ):
        handled = await forwarder._handle_compact_summary_item(
            AsyncMock(),
            session_id="conv_retry",
            bridge_dir=bridge_dir,
            item=_compact_summary_item(),
            retry_tracker=forwarder._PostRetryTracker(),
        )

    assert handled is False
    state = forwarder._read_compaction_state(bridge_dir)
    # Pending still set, nothing persisted — the summary will be retried.
    assert state.pending is not None
    assert state.pending.seq == 1
    assert state.persisted_seqs == ()


@pytest.mark.asyncio
async def test_restart_reattach_does_not_repersist_completed_boundary(tmp_path: Path) -> None:
    """
    An already-persisted boundary is never re-persisted after a rewind.

    Simulates a process restart / cursor rewind that re-reads a summary whose
    boundary already POSTed: ``persisted_seqs`` records the sequence, so
    ``_consume_pending_compaction`` returns ``None`` and
    ``_handle_compact_summary_item`` drops the record without persisting.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    # Durable state as it would exist after a completed compaction: seq 1
    # persisted, but a stale pending token for the same seq lingers (e.g.
    # crash between POST success and mark). The persisted set must win.
    forwarder._write_compaction_state(
        bridge_dir,
        forwarder.CompactionForwardState(
            pending=forwarder._PendingCompaction(seq=1, claude_session_id="claude-1"),
            last_seq=1,
            persisted_seqs=(1,),
        ),
    )

    persist = _persist_mock()
    with patch(
        "omnigent.harnesses.claude_native.forwarder._persist_native_compaction_item", persist
    ):
        handled = await forwarder._handle_compact_summary_item(
            AsyncMock(),
            session_id="conv_restart",
            bridge_dir=bridge_dir,
            item=_compact_summary_item(),
            retry_tracker=forwarder._PostRetryTracker(),
        )

    assert handled is True
    persist.assert_not_called()


@pytest.mark.asyncio
async def test_repeated_compactions_persist_distinct_boundaries(tmp_path: Path) -> None:
    """
    Two compaction cycles persist two distinct boundaries.

    Each ``PreCompact`` mints a fresh monotonic sequence, so a second
    compaction is not blocked by the first's ``persisted_seqs`` entry.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    persist = _persist_mock()

    with patch(
        "omnigent.harnesses.claude_native.forwarder._persist_native_compaction_item", persist
    ):
        # First compaction.
        await forwarder._note_precompact(
            bridge_dir, claude_session_id="claude-1", transcript_path=None
        )
        await forwarder._handle_compact_summary_item(
            AsyncMock(),
            session_id="conv_repeat",
            bridge_dir=bridge_dir,
            item=_compact_summary_item("first"),
            retry_tracker=forwarder._PostRetryTracker(),
        )
        # Second compaction, later in the same session.
        await forwarder._note_precompact(
            bridge_dir, claude_session_id="claude-1", transcript_path=None
        )
        await forwarder._handle_compact_summary_item(
            AsyncMock(),
            session_id="conv_repeat",
            bridge_dir=bridge_dir,
            item=_compact_summary_item("second"),
            retry_tracker=forwarder._PostRetryTracker(),
        )

    assert persist.call_count == 2
    state = forwarder._read_compaction_state(bridge_dir)
    assert state.pending is None
    assert set(state.persisted_seqs) == {1, 2}


@pytest.mark.asyncio
async def test_historical_summary_without_pending_is_skipped(tmp_path: Path) -> None:
    """
    An ``isCompactSummary`` record with no pending PreCompact is dropped.

    On a cold resume the transcript may contain a historical compact-summary
    record from a prior compaction with no live ``PreCompact`` token. It must
    not persist a spurious boundary, and must not be forwarded as a user
    bubble — ``_handle_compact_summary_item`` returns handled with no persist.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()  # no _note_precompact — no pending token

    persist = _persist_mock()
    with patch(
        "omnigent.harnesses.claude_native.forwarder._persist_native_compaction_item", persist
    ):
        handled = await forwarder._handle_compact_summary_item(
            AsyncMock(),
            session_id="conv_historical",
            bridge_dir=bridge_dir,
            item=_compact_summary_item(),
            retry_tracker=forwarder._PostRetryTracker(),
        )

    assert handled is True
    persist.assert_not_called()
    assert forwarder._read_compaction_state(bridge_dir).persisted_seqs == ()


@pytest.mark.asyncio
async def test_ambiguous_boundary_post_marks_persisted(tmp_path: Path) -> None:
    """
    An ambiguous POST failure is treated as delivered (no duplicate boundary).

    Mirrors the item-forwarding rule: when the server may already have
    committed the boundary (e.g. a dropped response on a 2xx), retrying would
    risk a duplicate compaction bubble, so the sequence is marked persisted
    and the record advanced.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    await forwarder._note_precompact(
        bridge_dir, claude_session_id="claude-1", transcript_path=None
    )

    ambiguous = AsyncMock(side_effect=httpx.ReadError("connection dropped mid-response"))

    with (
        patch(
            "omnigent.harnesses.claude_native.forwarder._persist_native_compaction_item", ambiguous
        ),
        patch(
            "omnigent.harnesses.claude_native.forwarder.post_may_have_been_delivered",
            return_value=True,
        ),
    ):
        handled = await forwarder._handle_compact_summary_item(
            AsyncMock(),
            session_id="conv_ambiguous",
            bridge_dir=bridge_dir,
            item=_compact_summary_item(),
            retry_tracker=forwarder._PostRetryTracker(),
        )

    assert handled is True
    state = forwarder._read_compaction_state(bridge_dir)
    assert 1 in state.persisted_seqs
    assert state.pending is None


@pytest.mark.asyncio
async def test_precompact_and_summary_same_poll_persists_boundary(tmp_path: Path) -> None:
    """
    P1-1: a PreCompact + summary first visible in one poll persists a boundary.

    The transcript forwarder (which consumes the ``isCompactSummary`` record)
    runs before the hook forwarder (which mints the ``PreCompact`` token)
    within a single poll. Without the pre-items prescan, a ``PreCompact`` and
    its summary that both first appear in the same poll would lose the
    boundary — the summary is consumed with no token yet minted.
    ``_prescan_precompact_edges`` mints the token first, so the summary that
    follows in the same poll finds it.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    # A PreCompact hook is written but the hook cursor has NOT advanced past
    # it yet (mirrors the same-poll ordering: hooks are forwarded AFTER items).
    record_hook_event(
        bridge_dir,
        {"hook_event_name": "PreCompact", "session_id": "claude-1"},
    )
    hook_state = await forwarder._ensure_hook_state(
        bridge_dir, start_at_end=False, session_id="conv_same_poll"
    )

    # No pending token before the prescan.
    assert forwarder._read_compaction_state(bridge_dir).pending is None

    # Prescan mints the token BEFORE the transcript summary is processed.
    await forwarder._prescan_precompact_edges(bridge_dir, hook_state)
    state = forwarder._read_compaction_state(bridge_dir)
    assert state.pending is not None
    assert state.pending.seq == 1

    # The summary in the same poll now finds the token and persists once.
    persist = _persist_mock()
    with patch(
        "omnigent.harnesses.claude_native.forwarder._persist_native_compaction_item", persist
    ):
        handled = await forwarder._handle_compact_summary_item(
            AsyncMock(),
            session_id="conv_same_poll",
            bridge_dir=bridge_dir,
            item=_compact_summary_item("same-poll summary"),
            retry_tracker=forwarder._PostRetryTracker(),
        )

    assert handled is True
    persist.assert_called_once()
    state = forwarder._read_compaction_state(bridge_dir)
    assert 1 in state.persisted_seqs
    assert state.pending is None


@pytest.mark.asyncio
async def test_prescan_is_idempotent_with_hook_phase(tmp_path: Path) -> None:
    """
    P1-1: the prescan and the main hook phase mint one token per PreCompact.

    Both scans see the same ``PreCompact`` record each poll. The
    ``event_cursor`` idempotency key must keep them converging on a single
    pending token — never two — so a re-mint cannot overwrite a token whose
    boundary is mid-persist.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    record_hook_event(
        bridge_dir,
        {"hook_event_name": "PreCompact", "session_id": "claude-1"},
    )
    hook_state = await forwarder._ensure_hook_state(
        bridge_dir, start_at_end=False, session_id="conv_idem"
    )

    # Prescan mints seq 1.
    await forwarder._prescan_precompact_edges(bridge_dir, hook_state)
    first = forwarder._read_compaction_state(bridge_dir)
    assert first.pending is not None and first.pending.seq == 1
    assert first.last_precompact_cursor == 1

    # The main hook phase would note the SAME edge (same event_cursor=1).
    # It must be a no-op: same seq, no second token.
    await forwarder._note_precompact(
        bridge_dir, claude_session_id="claude-1", transcript_path=None, event_cursor=1
    )
    second = forwarder._read_compaction_state(bridge_dir)
    assert second.pending is not None and second.pending.seq == 1
    assert second.last_seq == 1

    # A genuinely NEW PreCompact edge (higher cursor) mints the next seq.
    await forwarder._note_precompact(
        bridge_dir, claude_session_id="claude-1", transcript_path=None, event_cursor=2
    )
    third = forwarder._read_compaction_state(bridge_dir)
    assert third.pending is not None and third.pending.seq == 2
    assert third.last_precompact_cursor == 2


@pytest.mark.asyncio
async def test_standalone_completion_hook_persists_without_pending(tmp_path: Path) -> None:
    """
    P1-2: a compact SessionStart with no pending token still persists a boundary.

    Restores the legacy standalone-completion safety. When the
    ``PreCompact`` hook was dropped (or the forwarder attached after it
    fired) AND no transcript summary has persisted a boundary, the
    ``SessionStart source=compact`` completion hook must still persist
    exactly one boundary — otherwise resume reloads the full pre-compaction
    history. ``_claim_standalone_completion`` mints the sequence for it.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    # No _note_precompact, no persisted boundary — genuinely standalone.
    seq = await forwarder._claim_standalone_completion(bridge_dir)
    assert seq == 1
    state = forwarder._read_compaction_state(bridge_dir)
    # A pending token is installed so a later transcript summary reconciles
    # against the same sequence instead of double-persisting.
    assert state.pending is not None
    assert state.pending.seq == 1

    # After the caller persists and marks it done, the boundary is recorded.
    await forwarder._mark_compaction_persisted(bridge_dir, seq)
    final = forwarder._read_compaction_state(bridge_dir)
    assert 1 in final.persisted_seqs
    assert final.pending is None


@pytest.mark.asyncio
async def test_completion_hook_after_transcript_persist_is_absorbed(tmp_path: Path) -> None:
    """
    P1-2: a completion hook trailing a transcript-persisted boundary is absorbed.

    The transcript ``isCompactSummary`` path and the
    ``SessionStart source=compact`` hook are two completion signals for the
    SAME compaction. When the transcript path persists first it arms the
    completion-ack window; the trailing hook must be absorbed (return
    ``None``, no new sequence) rather than persist a spurious standalone
    boundary.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    await forwarder._note_precompact(
        bridge_dir, claude_session_id="claude-1", transcript_path=None
    )

    persist = _persist_mock()
    with patch(
        "omnigent.harnesses.claude_native.forwarder._persist_native_compaction_item", persist
    ):
        # Transcript path persists the boundary; arms expect_completion_ack.
        await forwarder._handle_compact_summary_item(
            AsyncMock(),
            session_id="conv_absorb",
            bridge_dir=bridge_dir,
            item=_compact_summary_item(),
            retry_tracker=forwarder._PostRetryTracker(),
        )
    assert persist.call_count == 1
    armed = forwarder._read_compaction_state(bridge_dir)
    assert armed.expect_completion_ack is True

    # The trailing completion hook finds no pending token and is absorbed.
    seq = await forwarder._consume_pending_compaction(
        bridge_dir, claude_session_id="claude-1", transcript_path=None
    )
    assert seq is None
    seq = await forwarder._claim_standalone_completion(bridge_dir)
    assert seq is None  # absorbed, NOT a new standalone boundary
    after = forwarder._read_compaction_state(bridge_dir)
    assert after.expect_completion_ack is False
    assert after.persisted_seqs == (1,)  # still exactly one boundary


@pytest.mark.asyncio
async def test_precompact_miss_is_counted_and_warned(tmp_path: Path) -> None:
    """
    P1-3: a summary skipped with no token and no boundary is counted as a miss.

    A skipped ``isCompactSummary`` with no pending token AND no boundary ever
    persisted is the observable ``PreCompact``-miss failure mode — it must
    bump the ``precompact_miss`` counter (not be silently dropped). A skip
    that follows a persisted boundary is an expected replay/dedupe and bumps
    ``expected_skip`` instead.
    """
    forwarder._reset_compaction_skip_stats()
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()  # no PreCompact, no persisted boundary

    persist = _persist_mock()
    with patch(
        "omnigent.harnesses.claude_native.forwarder._persist_native_compaction_item", persist
    ):
        handled = await forwarder._handle_compact_summary_item(
            AsyncMock(),
            session_id="conv_miss",
            bridge_dir=bridge_dir,
            item=_compact_summary_item(),
            retry_tracker=forwarder._PostRetryTracker(),
        )

    assert handled is True
    persist.assert_not_called()
    assert forwarder._compaction_skip_stats.precompact_miss == 1
    assert forwarder._compaction_skip_stats.expected_skip == 0

    # A skip AFTER a boundary was persisted is an expected replay, not a miss.
    forwarder._write_compaction_state(
        bridge_dir,
        forwarder.CompactionForwardState(pending=None, last_seq=1, persisted_seqs=(1,)),
    )
    with patch(
        "omnigent.harnesses.claude_native.forwarder._persist_native_compaction_item", persist
    ):
        await forwarder._handle_compact_summary_item(
            AsyncMock(),
            session_id="conv_miss",
            bridge_dir=bridge_dir,
            item=_compact_summary_item(),
            retry_tracker=forwarder._PostRetryTracker(),
        )
    assert forwarder._compaction_skip_stats.precompact_miss == 1  # unchanged
    assert forwarder._compaction_skip_stats.expected_skip == 1


@pytest.mark.asyncio
async def test_stale_completion_ack_does_not_swallow_a_later_boundary(
    tmp_path: Path,
) -> None:
    """
    P2-1: a completion ack is bound to its seq and is one-shot per boundary.

    The lost-boundary hazard: compaction A persists via the transcript path
    and arms ``expect_completion_ack``; A's own ``SessionStart source=compact``
    hook never fires (flaky), so the flag stays armed. A later compaction B's
    ``PreCompact`` is *also* dropped, then B's completion hook fires. With a
    bare unattributed flag, B's hook would be absorbed as A's stale ack and
    B's boundary lost.

    Binding the ack to a ``seq`` and making absorption one-shot fixes it: the
    window is consumed exactly once (the trailing hook for A), and any
    *further* standalone completion — B's — falls through to a fresh persist
    instead of being swallowed.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    await forwarder._note_precompact(
        bridge_dir, claude_session_id="claude-1", transcript_path=None
    )

    persist = _persist_mock()
    with patch(
        "omnigent.harnesses.claude_native.forwarder._persist_native_compaction_item", persist
    ):
        # Compaction A persists via the transcript path → arms the ack for A's seq.
        await forwarder._handle_compact_summary_item(
            AsyncMock(),
            session_id="conv_p21",
            bridge_dir=bridge_dir,
            item=_compact_summary_item(),
            retry_tracker=forwarder._PostRetryTracker(),
        )
    armed = forwarder._read_compaction_state(bridge_dir)
    assert armed.expect_completion_ack is True
    assert armed.expect_completion_ack_seq == 1  # bound to A's seq, not a bare bool
    assert armed.persisted_seqs == (1,)

    # A's own trailing completion hook arrives late and is absorbed (one-shot).
    absorbed = await forwarder._claim_standalone_completion(bridge_dir)
    assert absorbed is None
    after_absorb = forwarder._read_compaction_state(bridge_dir)
    assert after_absorb.expect_completion_ack is False
    assert after_absorb.expect_completion_ack_seq == 0  # window closed

    # Compaction B: its PreCompact was dropped too, so B arrives as a
    # standalone completion hook with NO pending token and NO armed ack. It
    # must persist a fresh boundary, not be swallowed as A's stale ack.
    b_seq = await forwarder._claim_standalone_completion(bridge_dir)
    assert b_seq == 2, "B's boundary must be persisted, not lost to a stale ack"
    final = forwarder._read_compaction_state(bridge_dir)
    assert final.pending is not None
    assert final.pending.seq == 2


@pytest.mark.asyncio
async def test_completion_ack_armed_for_unpersisted_seq_biases_to_persist(
    tmp_path: Path,
) -> None:
    """
    P2-1: an ack armed for a seq that is NOT persisted persists (bias-to-safe).

    If durable state is somehow armed (corrupt/partial write, or a legacy
    ``compaction_forwarder.json`` from before ``expect_completion_ack_seq``
    existed so the seq reads back as ``0``) the standalone path cannot prove
    the arriving hook is a duplicate. A lost boundary reloads the full
    pre-compaction history on resume — far worse than an at-most-once
    duplicate — so the path biases to persisting a fresh boundary rather than
    silently absorbing the hook.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    # Legacy/corrupt shape: flag armed but the seq it points at is not in
    # persisted_seqs (here it reads back as 0, mimicking an old state file).
    forwarder._write_compaction_state(
        bridge_dir,
        forwarder.CompactionForwardState(
            pending=None,
            last_seq=1,
            persisted_seqs=(),
            expect_completion_ack=True,
            expect_completion_ack_seq=0,
        ),
    )
    seq = await forwarder._claim_standalone_completion(bridge_dir)
    assert seq == 2, "bias-to-safe: persist rather than absorb an unprovable ack"
    state = forwarder._read_compaction_state(bridge_dir)
    assert state.pending is not None
    assert state.pending.seq == 2


@pytest.mark.asyncio
async def test_standalone_hook_persist_failure_holds_cursor_for_retry(
    tmp_path: Path,
) -> None:
    """
    P2-2: a standalone-completion persist failure holds the hook cursor.

    A ``SessionStart source=compact`` with no pending token and no transcript
    summary is a hook-only standalone compaction. If its boundary POST fails
    transiently the forwarder must NOT advance past the hook (losing the
    boundary, since no transcript summary will ever retry it) — it holds the
    hook cursor and retries next poll, mirroring the transcript path. The
    pending token minted by ``_claim_standalone_completion`` makes the retry
    idempotent: the re-seen hook re-consumes the same seq. A later successful
    POST persists exactly one boundary.
    """
    bridge_dir = tmp_path / "bridge"
    # A lone compact SessionStart (no preceding PreCompact) — standalone.
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "source": "compact",
            "session_id": "claude-standalone",
        },
    )
    start_state = forwarder.HookForwardState(event_cursor=0, byte_offset=0)

    request = httpx.Request("POST", "http://test/items")
    response = httpx.Response(503, request=request)
    failing = AsyncMock(
        side_effect=httpx.HTTPStatusError("boom", request=request, response=response)
    )

    async def _run_once(state: forwarder.HookForwardState) -> forwarder.HookForwardState:
        # The best-effort spinner status post is orthogonal to the durable
        # persist under test; stub it so the client mock stays quiet.
        with patch(
            "omnigent.harnesses.claude_native.forwarder._post_external_compaction_status",
            AsyncMock(return_value=None),
        ):
            return await forwarder._forward_available_status_events(
                client=AsyncMock(),
                session_id="conv_p22",
                bridge_dir=bridge_dir,
                state=state,
                retry_tracker=forwarder._PostRetryTracker(),
                dedupe=forwarder._ForwardDedupeState(),
                task_subjects={},
                task_statuses={},
                task_order=[],
            )

    # Poll 1: persist fails → cursor is held BEFORE the compaction hook record,
    # a pending token is minted, and no boundary is marked persisted.
    with patch(
        "omnigent.harnesses.claude_native.forwarder._persist_native_compaction_item", failing
    ):
        after_fail = await _run_once(start_state)
    assert failing.await_count == 1
    assert after_fail.event_cursor == start_state.event_cursor  # cursor held
    held = forwarder._read_compaction_state(bridge_dir)
    assert held.pending is not None  # token minted, awaiting a durable persist
    assert not held.persisted_seqs  # nothing marked persisted on failure
    minted_seq = held.pending.seq

    # Poll 2 (retry): the same hook record is re-seen; the persist succeeds and
    # re-consumes the SAME seq (idempotent), marking exactly one boundary.
    ok = _persist_mock()
    with patch("omnigent.harnesses.claude_native.forwarder._persist_native_compaction_item", ok):
        after_ok = await _run_once(after_fail)
    assert ok.await_count == 1
    persisted = forwarder._read_compaction_state(bridge_dir)
    assert persisted.persisted_seqs == (minted_seq,)  # exactly one boundary
    assert persisted.pending is None
    assert after_ok.event_cursor > after_fail.event_cursor  # cursor advanced
