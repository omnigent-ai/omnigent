"""Settlement tests for Claude-native forwarding."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

import omnigent.harnesses.claude_native.forwarder as forwarder
from omnigent.harnesses.claude_native.bridge import (
    ClaudeTranscriptItem,
    prepare_bridge_dir,
    record_hook_event,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata_prefix", [False, True])
async def test_partial_transcript_tail_does_not_promote_pending_settle(
    tmp_path: Path, metadata_prefix: bool
) -> None:
    """An incomplete final record is not quiescence and must not become a scheduled wake."""
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    prefix = (
        json.dumps(
            {
                "type": "assistant",
                "uuid": "before-tail",
                "message": {"role": "assistant", "content": "Entering the worktree"},
            }
        )
        + "\n"
    )
    tail = (
        json.dumps(
            {
                "type": "assistant",
                "uuid": "final-tail",
                "message": {"role": "assistant", "content": "Entered the worktree"},
            }
        )
        + "\n"
    )
    split_at = len(tail) // 2
    metadata = json.dumps({"type": "progress"}) + "\n" if metadata_prefix else ""
    transcript_path.write_text(prefix + metadata + tail[:split_at], encoding="utf-8")
    byte_offset = len(prefix.encode("utf-8"))
    state = forwarder.TranscriptForwardState(
        transcript_path=transcript_path,
        line_cursor=1,
        byte_offset=byte_offset,
        cursor_fingerprint=forwarder._jsonl_cursor_fingerprint(transcript_path, byte_offset),
        current_response_id="current-turn",
        seen_source_ids=("before-tail:0:message",),
        settled_response_id="older-turn",
        pending_settled_response_id="current-turn",
    )
    forwarder._write_forward_state(bridge_dir, state)
    posted_items: list[dict[str, Any]] = []

    def _handle_request(request: httpx.Request) -> httpx.Response:
        """Record conversation items without substituting transcript parsing or file reads."""
        payload = json.loads(request.content)
        if payload["type"] == "external_conversation_item":
            posted_items.append(payload["data"])
        return httpx.Response(202, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_handle_request), base_url="http://test"
    ) as client:
        dedupe = forwarder._ForwardDedupeState()

        async def _poll(
            current_state: forwarder.TranscriptForwardState,
        ) -> forwarder.TranscriptForwardState:
            """Forward one real transcript batch with the shared settle latch."""
            return await forwarder._forward_available_items(
                client=client,
                session_id="conv_partial_tail",
                bridge_dir=bridge_dir,
                agent_name="claude-native-ui",
                state=current_state,
                retry_tracker=forwarder._PostRetryTracker(),
                dedupe=dedupe,
            )

        waiting = await _poll(state)
        expected_offset = len((prefix + metadata).encode("utf-8"))
        assert waiting == replace(
            state,
            line_cursor=1 + int(metadata_prefix),
            byte_offset=expected_offset,
            cursor_fingerprint=forwarder._jsonl_cursor_fingerprint(
                transcript_path, expected_offset
            ),
        )
        assert dedupe.pending_settled_response_id == "current-turn"
        assert dedupe.settled_response_id == "older-turn"
        assert not posted_items
        with transcript_path.open("a", encoding="utf-8") as handle:
            handle.write(tail[split_at:])
        completed = await _poll(waiting)
        assert completed.byte_offset == transcript_path.stat().st_size
        assert completed.pending_settled_response_id == "current-turn"
        settled = await _poll(completed)
        assert settled.pending_settled_response_id is None
        assert settled.settled_response_id == "current-turn"
        assert forwarder._read_forward_state(bridge_dir) == settled

    assert len(posted_items) == 1
    assert posted_items[0]["item_type"] == "message"
    assert posted_items[0]["source_id"] == "final-tail:0:message"
    assert posted_items[0]["response_id"] == "current-turn"


@pytest.mark.asyncio
async def test_missing_transcript_persists_new_pending_settle(tmp_path: Path) -> None:
    """A Stop received during relocation stays durable without falsely settling its turn."""
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(json.dumps({"type": "progress"}) + "\n", encoding="utf-8")
    byte_offset = transcript_path.stat().st_size
    state = forwarder.TranscriptForwardState(
        transcript_path=transcript_path,
        line_cursor=1,
        byte_offset=byte_offset,
        cursor_fingerprint=forwarder._jsonl_cursor_fingerprint(transcript_path, byte_offset),
        current_response_id="current-turn",
        settled_response_id="older-turn",
    )
    forwarder._write_forward_state(bridge_dir, state)
    os.replace(transcript_path, tmp_path / "relocated.jsonl")
    dedupe = forwarder._ForwardDedupeState(pending_settled_response_id="current-turn")

    def _unexpected_request(request: httpx.Request) -> httpx.Response:
        """A missing transcript cannot produce conversation item posts."""
        raise AssertionError(f"Unexpected POST to {request.url}")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_unexpected_request), base_url="http://test"
    ) as client:
        waiting = await forwarder._forward_available_items(
            client=client,
            session_id="conv_missing_stop",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            state=state,
            retry_tracker=forwarder._PostRetryTracker(),
            dedupe=dedupe,
        )

    assert waiting == replace(state, pending_settled_response_id="current-turn")
    assert forwarder._read_forward_state(bridge_dir) == waiting
    assert dedupe.pending_settled_response_id == "current-turn"
    assert dedupe.settled_response_id == "older-turn"


def test_transcript_forward_state_persists_settled_response_id(tmp_path: Path) -> None:
    """
    The turn-settle latch survives a forwarder restart via the cursor file.

    A restart inside a scheduled-wake gap must still mark the wake; a
    pre-latch state file (no ``settled_response_id`` key) loads as None.
    """
    bridge_dir = prepare_bridge_dir("conv_x", bridge_id="b2", workspace=tmp_path)
    transcript = tmp_path / "session.jsonl"
    state = forwarder.TranscriptForwardState(
        transcript_path=transcript,
        line_cursor=3,
        byte_offset=64,
        current_response_id="resp_a",
        settled_response_id="resp_a",
        pending_settled_response_id="resp_b",
    )
    forwarder._write_forward_state(bridge_dir, state)
    loaded = forwarder._read_forward_state(bridge_dir)
    assert loaded is not None
    assert loaded.settled_response_id == "resp_a"
    assert loaded.pending_settled_response_id == "resp_b"

    raw = json.loads((bridge_dir / forwarder._FORWARDER_STATE_FILE).read_text("utf-8"))
    del raw["settled_response_id"]
    (bridge_dir / forwarder._FORWARDER_STATE_FILE).write_text(json.dumps(raw), "utf-8")
    legacy = forwarder._read_forward_state(bridge_dir)
    assert legacy is not None
    assert legacy.settled_response_id is None


def test_promote_pending_settle_waits_for_turn_quiescence(tmp_path: Path) -> None:
    """
    A pending settle activates only once its turn has no output in flight.

    The turn's final message can surface after its Stop edge — promoting
    while that batch still
    carries the turn's output would mis-read the tail as a scheduled
    wake and split the answer into a phantom new turn.
    """
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    dedupe = forwarder._ForwardDedupeState()
    dedupe.pending_settled_response_id = "resp_a"
    tail = ClaudeTranscriptItem(
        source_id="s1:0:message",
        item_type="message",
        data={"role": "assistant", "content": [{"type": "output_text", "text": "tail"}]},
        response_id="resp_a",
    )
    assert (
        forwarder._promote_pending_settle(
            dedupe, [tail], transcript_path=transcript_path, byte_offset=0
        )
        is False
    )
    assert dedupe.settled_response_id is None
    assert dedupe.pending_settled_response_id == "resp_a"

    # A late tool result also defers: it can surface EARLIER than the
    # assistant tail, and promoting on it would mis-mark that tail.
    late_result = ClaudeTranscriptItem(
        source_id="s2:0:function_call_output",
        item_type="function_call_output",
        data={"call_id": "c1", "output": "done"},
        response_id="resp_a",
    )
    assert (
        forwarder._promote_pending_settle(
            dedupe, [late_result], transcript_path=transcript_path, byte_offset=0
        )
        is False
    )
    assert dedupe.pending_settled_response_id == "resp_a"

    # Items for OTHER turns don't defer; a truly quiet batch promotes.
    other = ClaudeTranscriptItem(
        source_id="s3:0:message",
        item_type="message",
        data={"role": "assistant", "content": [{"type": "output_text", "text": "hi"}]},
        response_id="resp_b",
    )
    assert (
        forwarder._promote_pending_settle(
            dedupe, [other], transcript_path=transcript_path, byte_offset=0
        )
        is True
    )
    assert dedupe.settled_response_id == "resp_a"
    assert dedupe.pending_settled_response_id is None

    # Idempotent once promoted.
    assert (
        forwarder._promote_pending_settle(
            dedupe, [], transcript_path=transcript_path, byte_offset=0
        )
        is False
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("wake", "retry_mode"),
    [
        (wake, retry_mode)
        for wake in ("cron", "handback", "handback_before_settle")
        for retry_mode in ("none", "same_process", "restart", "status")
    ],
)
async def test_scheduled_wake_forwards_marker_under_a_new_turn_id(
    tmp_path: Path, retry_mode: str, wake: str
) -> None:
    """
    The full wake pipeline: settle → quiet-poll promote → running new turn.

    Poll 1 forwards a turn; its Stop edge records the pending settle
    (covered by the status-events test — recorded directly here). Poll 2
    is quiet and promotes the settle, persisting it. Poll 3 sees a turn
    Claude started on its own — a cron firing (no user entry, so the
    scheduled-wake marker leads) or a sub-agent hand-back, which can also
    land before the quiet poll. It must post ``running`` once, then the
    turn's items, all under a new response id. A failed ``running`` post
    holds the items until it is retried.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "uuid": "iter-one",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Iteration 1: all green."}],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    state = forwarder.TranscriptForwardState(
        transcript_path=transcript_path,
        line_cursor=0,
        byte_offset=0,
        cursor_fingerprint=forwarder._jsonl_cursor_fingerprint(transcript_path, 0),
    )
    requests: list[dict[str, Any]] = []
    failed_marker = False

    def _handle_request(request: httpx.Request) -> httpx.Response:
        nonlocal failed_marker
        """
        Accept every forwarder POST, recording its payload.

        :param request: Outbound HTTP request from the forwarder.
        :returns: HTTP 202 for the mock Omnigent endpoint.
        """
        payload = json.loads(request.content.decode("utf-8"))
        assert isinstance(payload, dict)
        requests.append(payload)
        failing = (
            payload["type"] == "external_session_status"
            if retry_mode == "status"
            else payload["type"] == "external_conversation_item"
            and payload["data"]["item_data"].get("role") == "user"
        )
        if retry_mode != "none" and not failed_marker and failing:
            failed_marker = True
            return httpx.Response(503)
        return httpx.Response(202, json={})

    transport = httpx.MockTransport(_handle_request)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        dedupe = forwarder._ForwardDedupeState()
        after_turn = await forwarder._forward_available_items(
            client=client,
            session_id="conv_abc",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            state=state,
            retry_tracker=forwarder._PostRetryTracker(),
            dedupe=dedupe,
        )
        turn_one_id = after_turn.current_response_id
        assert turn_one_id is not None

        # The turn ends: the Stop edge records the pending settle.
        await forwarder._record_pending_settle(bridge_dir, dedupe, turn_one_id)
        quiet = after_turn
        if wake != "handback_before_settle":
            quiet = await forwarder._forward_available_items(
                client=client,
                session_id="conv_abc",
                bridge_dir=bridge_dir,
                agent_name="claude-native-ui",
                state=after_turn,
                retry_tracker=forwarder._PostRetryTracker(),
                dedupe=dedupe,
            )
            assert dedupe.settled_response_id == turn_one_id
            assert quiet.settled_response_id == turn_one_id

        # A cron firing appends assistant output with NO user entry; a
        # hand-back appends the sub-agent's report first.
        with transcript_path.open("a", encoding="utf-8") as handle:
            if wake != "cron":
                handle.write(
                    json.dumps(
                        {
                            "type": "user",
                            "uuid": "handback",
                            "isMeta": True,
                            "origin": {
                                "kind": "peer",
                                "handback": True,
                                "senderTaskId": "agent-1",
                            },
                            "message": {"role": "user", "content": "<agent-message>done"},
                        }
                    )
                    + "\n"
                )
            handle.write(
                json.dumps(
                    {
                        "type": "assistant",
                        "uuid": "iter-two",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "Iteration 2: still green."}],
                        },
                    }
                )
                + "\n"
            )
        requests.clear()
        after_wake = await forwarder._forward_available_items(
            client=client,
            session_id="conv_abc",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            state=quiet,
            retry_tracker=forwarder._PostRetryTracker(),
            dedupe=dedupe,
        )
        if retry_mode != "none":
            persisted = forwarder._read_forward_state(bridge_dir)
            assert persisted is not None
            assert persisted.current_response_id == turn_one_id
            assert persisted.line_cursor == quiet.line_cursor
            assert after_wake.current_response_id == turn_one_id
            if retry_mode == "restart":
                dedupe = forwarder._ForwardDedupeState()
                after_wake = persisted
            await forwarder._forward_available_items(
                client=client,
                session_id="conv_abc",
                bridge_dir=bridge_dir,
                agent_name="claude-native-ui",
                state=after_wake,
                retry_tracker=forwarder._PostRetryTracker(),
                dedupe=dedupe,
            )

    # Claude's status file stays busy while a background agent lives, so the
    # woken turn's start is announced from the transcript, ahead of its items.
    status = requests[0]
    statuses = [r for r in requests if r["type"] == "external_session_status"]
    items = [r["data"] for r in requests if r["type"] == "external_conversation_item"]
    assert len(statuses) + len(items) == len(requests)
    if retry_mode == "status":
        assert statuses[0] == statuses[1]
        status = statuses[1]
    elif retry_mode != "none":
        assert items[0]["source_id"] == items[1]["source_id"]
        items = items[1:]
    wake_turn_id = items[0]["response_id"]
    assert wake_turn_id != turn_one_id
    assert status["type"] == "external_session_status"
    assert status["data"]["status"] == "running"
    assert status["data"]["response_id"] == wake_turn_id
    assert [item["item_data"]["role"] for item in items] == ["user", "assistant"]
    if wake == "cron":
        assert items[0]["item_data"]["content"] == [
            {"type": "input_text", "text": "[System: scheduled prompt fired]"}
        ]
    assert {item["response_id"] for item in items} == {wake_turn_id}


async def test_prompt_after_a_settled_turn_announces_its_edges(tmp_path: Path) -> None:
    """
    A prompt typed while a background agent keeps the status file busy is announced.

    An Esc fires no ``Stop``, so the announced turn is closed from its interrupt marker.
    """
    transcript_path = tmp_path / "session.jsonl"
    records = [
        {"type": "user", "uuid": "prompt", "message": {"role": "user", "content": "go"}},
        {
            "type": "user",
            "uuid": "interrupt",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "[Request interrupted by user]"}],
            },
        },
    ]
    transcript_path.write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )
    state = forwarder.TranscriptForwardState(
        transcript_path=transcript_path,
        line_cursor=0,
        byte_offset=0,
        cursor_fingerprint=forwarder._jsonl_cursor_fingerprint(transcript_path, 0),
    )
    statuses: list[str] = []

    def _handle_request(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        if payload["type"] == "external_session_status":
            statuses.append(payload["data"]["status"])
        return httpx.Response(202, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_handle_request), base_url="http://test"
    ) as client:
        await forwarder._forward_available_items(
            client=client,
            session_id="conv_abc",
            bridge_dir=tmp_path / "bridge",
            agent_name="claude-native-ui",
            state=state,
            retry_tracker=forwarder._PostRetryTracker(),
            dedupe=forwarder._ForwardDedupeState(settled_response_id="resp_prev_turn"),
        )

    assert statuses == ["running", "idle"]


@pytest.mark.parametrize(
    ("previous", "expected"),
    [("unstopped", "resp_t"), ("settled", None), ("interrupted", None)],
)
def test_a_late_stop_is_attributed_to_the_turn_that_ended(
    previous: str, expected: str | None
) -> None:
    """
    A turn opened before the previous turn's ``Stop`` arrived does not take that Stop.

    A prompt can open the next turn before the hook for the last one lands;
    stamping that Stop with the new turn settled it at once and its reply read
    as a scheduled wake.
    """

    def user(source: str, text: str, response_id: str, **extra: Any) -> ClaudeTranscriptItem:
        return ClaudeTranscriptItem(
            source_id=f"{source}:0:message",
            item_type="message",
            data={"role": "user", "content": [{"type": "input_text", "text": text}]},
            response_id=response_id,
            **extra,
        )

    items = [user("prompt", "next", "resp_p", opens_turn=True)]
    if previous == "interrupted":
        items.insert(0, user("esc", "[Request interrupted by user]", "resp_esc"))
    dedupe = forwarder._ForwardDedupeState(
        pending_settled_response_id="resp_t" if previous == "settled" else None
    )

    forwarder._track_unstopped_turn(items, set(), dedupe, previous_response_id="resp_t")

    assert dedupe.unstopped_response_id == expected


@pytest.mark.asyncio
async def test_a_stop_read_before_a_restart_still_settles_its_turn(tmp_path: Path) -> None:
    """
    A restart right after a ``Stop`` is consumed keeps that turn settled.

    The hook cursor is already past the ``Stop``, so its settle must be on disk;
    otherwise the next turn's ``Stop`` is credited to it and a cron wake after
    that next turn opens no new turn.
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
    state = forwarder.TranscriptForwardState(
        transcript_path=transcript_path,
        line_cursor=0,
        byte_offset=0,
        cursor_fingerprint=forwarder._jsonl_cursor_fingerprint(transcript_path, 0),
    )
    hook_state = forwarder.HookForwardState(event_cursor=0, byte_offset=0)
    dedupe = forwarder._ForwardDedupeState()
    statuses: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        if payload["type"] == "external_session_status":
            statuses.append(payload["data"])
        elif payload["type"] == "external_conversation_item":
            items.append(payload["data"])
        return httpx.Response(202, json={})

    def append(*records: dict[str, Any]) -> None:
        with transcript_path.open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")

    def turn(name: str) -> None:
        append(
            {"type": "user", "uuid": f"{name}-0", "message": {"role": "user", "content": name}},
            {
                "type": "assistant",
                "uuid": f"{name}-1",
                "message": {"role": "assistant", "content": "ok"},
            },
        )
        for event in ("UserPromptSubmit", "Stop"):
            record_hook_event(
                bridge_dir, {"hook_event_name": event, "session_id": "claude-session"}
            )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test"
    ) as client:

        async def poll() -> None:
            nonlocal state, hook_state
            state = await forwarder._forward_available_items(
                client=client,
                session_id="conv_abc",
                bridge_dir=bridge_dir,
                agent_name="claude-native-ui",
                state=state,
                retry_tracker=forwarder._PostRetryTracker(),
                dedupe=dedupe,
            )
            hook_state = await forwarder._forward_available_status_events(
                client=client,
                session_id="conv_abc",
                bridge_dir=bridge_dir,
                state=hook_state,
                retry_tracker=forwarder._PostRetryTracker(),
                dedupe=dedupe,
                task_subjects={},
                task_statuses={},
                task_order=[],
                response_id=dedupe.unstopped_response_id or state.current_response_id,
            )

        turn("first")
        await poll()
        first_id = state.current_response_id
        # Restart before any later transcript poll persists the settle.
        restored_state = forwarder._read_forward_state(bridge_dir)
        restored_hooks = forwarder._read_hook_state(bridge_dir)
        assert restored_state is not None and restored_hooks is not None
        state, hook_state, dedupe = restored_state, restored_hooks, forwarder._ForwardDedupeState()

        turn("second")
        await poll()
        second_id = state.current_response_id
        await poll()
        assert dedupe.settled_response_id == second_id
        items.clear()
        append(
            {
                "type": "assistant",
                "uuid": "cron-0",
                "message": {"role": "assistant", "content": "Iteration 2: still green."},
            }
        )
        await poll()

    assert first_id is not None and second_id not in {None, first_id}
    wake_id = items[0]["response_id"]
    assert wake_id != second_id
    edges = [(data["status"], data["response_id"]) for data in statuses]
    assert [edge for edge in edges if edge[0] == "idle"] == [
        ("idle", first_id),
        ("idle", second_id),
    ]
    assert edges[-1] == ("running", wake_id)
    assert items[0]["item_data"]["content"] == [
        {"type": "input_text", "text": "[System: scheduled prompt fired]"}
    ]
    assert {item["response_id"] for item in items} == {wake_id}


@pytest.mark.asyncio
@pytest.mark.parametrize("late", ["prompt", "handback", "shell"])
async def test_a_stop_ahead_of_its_transcript_leaves_the_last_turn_idle(
    tmp_path: Path, late: str
) -> None:
    """
    A ``Stop`` read before its turn's transcript lines still settles that turn.

    Claude flushes the transcript after running hooks, so a quick turn's
    ``Stop`` can be read first and stamped with no turn. The next prompt must
    not mistake that turn for one still awaiting its ``Stop``, or the last
    turn's ``Stop`` is credited to it and the last turn is re-opened as running.
    Once a turn has settled, the late lines must not announce the stopped turn
    (a prompt, a hand-back or a shell launch) as running either. A turn that a
    task notification wakes ends with its own ``Stop``, so the prompts after it
    are still announced, and a shell launched before an Esc is still counted.
    A hand-back that lands mid-turn opens no turn, so that turn's ``Stop``
    leaves the session idle. A late ``Stop`` that idles the earlier turn
    re-announces the newer one, retrying a failed post until it lands.
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
    state = forwarder.TranscriptForwardState(
        transcript_path=transcript_path,
        line_cursor=0,
        byte_offset=0,
        cursor_fingerprint=forwarder._jsonl_cursor_fingerprint(transcript_path, 0),
    )
    hook_state = forwarder.HookForwardState(event_cursor=0, byte_offset=0)
    dedupe = forwarder._ForwardDedupeState()
    posted: list[dict[str, Any]] = []
    running_failures = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal running_failures
        payload = json.loads(request.content.decode("utf-8"))
        if payload["type"] == "external_session_status":
            if running_failures and payload["data"]["status"] == "running":
                running_failures -= 1
                return httpx.Response(503, json={})
            posted.append(payload["data"])
        return httpx.Response(202, json={})

    def turn(
        name: str,
        *,
        kind: str = "prompt",
        transcript: bool = True,
        hooks: bool = True,
        esc: bool = False,
    ) -> None:
        records: list[dict[str, Any]] = [
            {"type": "user", "message": {"role": "user", "content": name}},
            {"type": "assistant", "message": {"role": "assistant", "content": "ok"}},
        ]
        if esc:
            records.append(
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "[Request interrupted by user]"}],
                    },
                }
            )
        if kind == "mid_turn_handback":
            records += [
                {
                    "type": "user",
                    "isMeta": True,
                    "origin": {"kind": "peer", "handback": True, "senderTaskId": "agent-1"},
                    "message": {"role": "user", "content": f"<agent-message>{name}"},
                },
                {"type": "assistant", "message": {"role": "assistant", "content": "noted"}},
            ]
        if kind == "notification":
            records[0].update(
                isMeta=True,
                message={
                    "role": "user",
                    "content": f"<task-notification><task-id>{name}</task-id>"
                    "<status>completed</status></task-notification>",
                },
            )
        if kind == "handback":
            records[0].update(
                isMeta=True,
                origin={"kind": "peer", "handback": True, "senderTaskId": "agent-1"},
                message={"role": "user", "content": f"<agent-message>{name}"},
            )
        if kind == "shell":
            records[1:1] = [
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu_bg",
                                "name": "Bash",
                                "input": {"command": "sleep 40", "run_in_background": True},
                            }
                        ],
                    },
                },
                {
                    "type": "user",
                    "toolUseResult": {"backgroundTaskId": "bg-1"},
                    "message": {
                        "role": "user",
                        "content": [
                            {"type": "tool_result", "tool_use_id": "toolu_bg", "content": "ok"}
                        ],
                    },
                },
            ]
        if transcript:
            with transcript_path.open("a", encoding="utf-8") as handle:
                for index, record in enumerate(records):
                    handle.write(json.dumps({**record, "uuid": f"{name}-{index}"}) + "\n")
        if hooks:
            events: list[dict[str, Any]] = [] if esc else [{"hook_event_name": "Stop"}]
            if kind in {"prompt", "shell", "mid_turn_handback"}:
                events.insert(0, {"hook_event_name": "UserPromptSubmit"})
            if kind == "shell" and not esc:
                events[-1]["background_tasks"] = [
                    {"id": "bg-1", "type": "shell", "status": "running", "command": "sleep 40"}
                ]
            for event in events:
                record_hook_event(bridge_dir, {**event, "session_id": "claude-session"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test"
    ) as client:

        async def poll() -> None:
            # One forwarder loop iteration: transcript items, then hook edges.
            nonlocal state, hook_state
            state = await forwarder._forward_available_items(
                client=client,
                session_id="conv_abc",
                bridge_dir=bridge_dir,
                agent_name="claude-native-ui",
                state=state,
                retry_tracker=forwarder._PostRetryTracker(),
                dedupe=dedupe,
            )
            hook_state = await forwarder._forward_available_status_events(
                client=client,
                session_id="conv_abc",
                bridge_dir=bridge_dir,
                state=hook_state,
                retry_tracker=forwarder._PostRetryTracker(),
                dedupe=dedupe,
                task_subjects={},
                task_statuses={},
                task_order=[],
                response_id=dedupe.unstopped_response_id or state.current_response_id,
            )

        turn("first", transcript=False)
        await poll()
        turn("first", hooks=False)
        await poll()
        for name in ("second", "third"):
            turn(name)
            await poll()
            await poll()
        assert dedupe.settled_response_id is not None
        assert [data["status"] for data in posted] == ["idle", "idle", "running", "idle"]

        # The next turn's only Stop is read before its lines; nothing else ends it.
        turn("fourth", kind=late, transcript=False)
        await poll()
        turn("fourth", kind=late, hooks=False)
        for _ in range(3):
            await poll()

        assert [data["status"] for data in posted] == ["idle", "idle", "running", "idle", "idle"]
        # The Stop's count already includes the shell; its late launch adds nothing.
        assert [data.get("background_task_count") for data in posted][-1] == (
            1 if late == "shell" else 0
        )

        posted.clear()
        turn("notified", kind="notification")
        await poll()
        await poll()
        turn("sixth")
        await poll()
        await poll()
        turn("seventh", kind="shell", esc=True)
        await poll()
        turn("eighth", kind="mid_turn_handback")
        for _ in range(3):
            await poll()

        assert [(data["status"], data.get("background_task_count")) for data in posted] == [
            ("running", None),
            ("idle", 0),
            ("running", None),
            ("idle", 0),
            ("running", None),
            ("running", 1),
            ("idle", None),
            ("running", None),
            ("idle", 0),
        ]
        assert posted[-1]["response_id"] == state.current_response_id

        # The next turn opens before the last one's Stop lands; that Stop idles
        # the last turn and the failed re-announce of the newer one is retried.
        posted.clear()
        turn("ninth", hooks=False)
        turn("tenth", hooks=False)
        await poll()
        record_hook_event(bridge_dir, {"hook_event_name": "Stop", "session_id": "claude-session"})
        running_failures = 1
        for _ in range(3):
            await poll()
        record_hook_event(bridge_dir, {"hook_event_name": "Stop", "session_id": "claude-session"})
        await poll()

    assert [data["status"] for data in posted] == ["running", "running", "idle", "running", "idle"]
    assert running_failures == 0
    assert posted[3]["response_id"] == posted[1]["response_id"]
    assert posted[-1]["response_id"] == state.current_response_id
