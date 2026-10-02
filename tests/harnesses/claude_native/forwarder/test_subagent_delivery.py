"""Subagent delivery tests for Claude-native forwarding."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

import omnigent.harnesses.claude_native.forwarder as forwarder
from omnigent.harnesses.claude_native.bridge import (
    ClaudeTranscriptItem,
    TranscriptReadResult,
    TranscriptRecordItems,
    record_hook_event,
)
from tests.harnesses.claude_native.forwarder._support import (
    _get_recorded_request,
    _seed_subagent_on_disk,
    _start_recording_server_with_responses,
    _subagent_drop_row,
)


async def test_subagent_watcher_forwards_transcript_items_to_child_session(
    tmp_path: Path,
) -> None:
    """
    After registering a sub-agent, the forwarder tails its
    ``.jsonl`` and POSTs an array of ``external_conversation_item`` events to
    the Omnigent child session id (not the parent's).
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="b6d8fff",
        agent_type="Explore",
        description="Trace data flow",
        tool_use_id="toolu_abc",
        # Real sub-agent transcripts carry ``isSidechain: true`` on
        # every record (that's how Claude marks them as belonging to
        # a child instead of the main thread). The parser's default
        # behavior strips sidechain records, so without this flag
        # the watcher silently posts zero items — pin the real shape
        # here so a regression to that behavior fails this test.
        transcript_records=[
            {
                "isSidechain": True,
                "type": "user",
                "uuid": "sa-user-1",
                "message": {"role": "user", "content": "go"},
            },
            {
                "isSidechain": True,
                "type": "assistant",
                "uuid": "sa-assistant-1",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "looking now"}],
                },
            },
        ],
    )
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "claude-session",
            "transcript_path": str(transcript_path),
        },
    )

    def response_for(body: object) -> object:
        """Mint a known child id for the start event.

        :param body: Decoded request body.
        :returns: Response payload.
        """
        if isinstance(body, list):
            return [
                {"queued": False, "item_id": f"item-{index}"} for index, _event in enumerate(body)
            ]
        if isinstance(body, dict) and body.get("type") == "external_subagent_start":
            return {"queued": False, "child_session_id": "conv_child_beta"}
        return {}

    server, _thread, base_url = _start_recording_server_with_responses(response_for)
    task = asyncio.create_task(
        forwarder.forward_claude_transcript_to_session(
            base_url=base_url,
            headers={},
            session_id="conv_parent",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            start_at_end=False,
            poll_interval_s=0.01,
        )
    )
    try:
        # We need the start event plus one event array addressed to the child.
        child_path = "/v1/sessions/conv_child_beta/events"
        batch: list[dict[str, Any]] | None = None
        for _ in range(40):
            req = await _get_recorded_request(server)
            if req["path"] == child_path and isinstance(req["body"], list):
                batch = req["body"]
                break
        assert batch is not None
        assert len(batch) == 2
        assert all(event["type"] == "external_conversation_item" for event in batch)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.shutdown()
        server.server_close()


async def test_subagent_watcher_retries_failed_batch_from_checkpoint(
    tmp_path: Path,
) -> None:
    """
    A rejected child batch leaves its byte cursor behind and retries in order.

    The server deduplicates source ids, so an ambiguous response can safely
    retry the entire batch even if some entries were already applied. The local
    cursor advances only after the acknowledgement arrives.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    subagent_jsonl = _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="retry1",
        agent_type="Explore",
        description="retry item flow",
        tool_use_id="toolu_retry",
        transcript_records=[
            {
                "isSidechain": True,
                "type": "user",
                "uuid": "sa-user-retry",
                "message": {"role": "user", "content": "go"},
            },
            {
                "isSidechain": True,
                "type": "assistant",
                "uuid": "sa-assistant-retry",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "done"}],
                },
            },
        ],
    )
    state = forwarder.SubagentForwardState(
        subagents={
            "retry1": forwarder.SubagentEntry(
                subagent_id="retry1",
                child_conversation_id="conv_child_retry",
            )
        }
    )
    posted_items: list[str] = []
    batch_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Fail the first batch and acknowledge its retry.

        :param request: Request issued by the forwarder.
        :returns: Canned Omnigent response.
        """
        nonlocal batch_attempts
        body = json.loads(request.content.decode("utf-8"))
        if not isinstance(body, list):
            return httpx.Response(202, json={})
        batch_attempts += 1
        for event in body:
            row = event["data"]
            item_data = row["item_data"]
            posted_items.append(f"{item_data['role']}:{item_data['content'][0]['text']}")
        if batch_attempts == 1:
            return httpx.Response(503, json={"error": "try again"})
        return httpx.Response(
            202,
            json=[{"queued": False, "item_id": f"item-{index}"} for index, _ in enumerate(body)],
        )

    item_retry_tracker = forwarder._PostRetryTracker(base_delay_s=0.0)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://ap",
    ) as client:
        first = await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=state,
            agent_name="claude-native-ui",
            start_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            item_retry_tracker=item_retry_tracker,
            status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
        )
        second = await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=first,
            agent_name="claude-native-ui",
            start_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            item_retry_tracker=item_retry_tracker,
            status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
        )

    assert posted_items == ["user:go", "assistant:done", "user:go", "assistant:done"]
    child_state = second.subagents["retry1"]
    assert child_state.byte_offset == subagent_jsonl.stat().st_size
    assert set(child_state.seen_source_ids) == {
        "sa-user-retry:0:message",
        "sa-assistant-retry:0:message",
    }


def test_subagent_batches_obey_count_and_exact_byte_limits() -> None:
    """Batching counts the complete UTF-8 JSON body and truncates one huge item."""
    assert forwarder.MAX_SUBAGENT_EVENT_BATCH_BYTES == 5 * 1024 * 1024

    def pending(index: int, text: str) -> forwarder._PendingSubagentItem:
        return forwarder._PendingSubagentItem(
            item=ClaudeTranscriptItem(
                source_id=f"source-{index}",
                item_type="function_call_output",
                data={"call_id": f"toolu_{index}", "output": text},
                response_id="resp_batch",
            ),
            checkpoint_after=index + 1,
        )

    tiny_batches = forwarder._partition_subagent_batches(
        [pending(index, "ok") for index in range(205)]
    )
    assert [len(batch) for batch in tiny_batches] == [100, 100, 5]

    large_batches = forwarder._partition_subagent_batches(
        [pending(1000, "€" * 1_000_000), pending(1001, "€" * 1_000_000)]
    )
    assert [len(batch) for batch in large_batches] == [1, 1]

    oversized = forwarder._partition_subagent_batches([pending(2000, "€" * 2_000_000)])
    assert len(oversized) == 1
    truncated_output = oversized[0][0].item.data["output"]
    assert isinstance(truncated_output, str)
    assert "content truncated by omnigent" in truncated_output
    for batch in [*tiny_batches, *large_batches, *oversized]:
        assert (
            len(forwarder._encoded_subagent_batch(batch))
            <= forwarder.MAX_SUBAGENT_EVENT_BATCH_BYTES
        )


def test_oversized_subagent_item_does_not_truncate_identifiers() -> None:
    """Batch fitting never rewrites schema-significant identifier fields."""
    name = "n" * forwarder.MAX_SUBAGENT_EVENT_BATCH_BYTES
    entry = forwarder._PendingSubagentItem(
        item=ClaudeTranscriptItem(
            source_id="oversized-name",
            item_type="function_call",
            data={"agent": "claude", "name": name, "arguments": "{}", "call_id": "call-1"},
            response_id="resp-name",
        )
    )

    fitted = forwarder._fit_subagent_item(entry)

    assert fitted.drop_reason is not None
    assert fitted.item.data["name"] == name


@pytest.mark.parametrize(
    ("field_name", "kind"),
    [("input", "input"), ("stdout", "output"), ("stderr", "output")],
)
def test_oversized_subagent_terminal_text_is_truncated(
    monkeypatch: pytest.MonkeyPatch,
    field_name: str,
    kind: str,
) -> None:
    """Large terminal commands and output are shrunk instead of dropped."""
    monkeypatch.setattr(forwarder, "MAX_SUBAGENT_EVENT_BATCH_BYTES", 1024)
    entry = forwarder._PendingSubagentItem(
        item=ClaudeTranscriptItem(
            source_id=f"oversized-terminal-{field_name}",
            item_type="terminal_command",
            data={"kind": kind, field_name: "x" * 2048},
            response_id="resp-terminal",
        )
    )

    fitted = forwarder._fit_subagent_item(entry)

    assert fitted.drop_reason is None
    assert fitted.item.data["kind"] == kind
    terminal_text = fitted.item.data[field_name]
    assert isinstance(terminal_text, str)
    assert "content truncated by omnigent" in terminal_text
    assert len(forwarder._encoded_subagent_batch([fitted])) <= 1024


def test_subagent_batch_partitioning_encodes_items_linearly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Byte accounting never re-encodes the growing batch prefix."""

    entries = [
        forwarder._PendingSubagentItem(
            item=ClaudeTranscriptItem(
                source_id=f"linear-{index}",
                item_type="message",
                data={"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
                response_id="resp_linear",
            )
        )
        for index in range(20)
    ]
    encoded_item_count = 0
    original_encode = forwarder._encoded_subagent_batch

    def record_encode(items: list[forwarder._PendingSubagentItem]) -> bytes:
        nonlocal encoded_item_count
        encoded_item_count += len(items)
        return original_encode(items)

    monkeypatch.setattr(forwarder, "_encoded_subagent_batch", record_encode)

    batches = forwarder._partition_subagent_batches(entries)

    assert batches == [entries]
    assert encoded_item_count == 2 * len(entries)


@pytest.mark.asyncio
async def test_subagent_batch_partitioning_runs_off_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Child-history JSON sizing does not block the live forwarding loop."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    subagents_dir = tmp_path / "subagents"
    subagents_dir.mkdir()
    (subagents_dir / "agent-worker.jsonl").write_text("", encoding="utf-8")
    entry = forwarder.SubagentEntry(
        subagent_id="worker",
        child_conversation_id="conv_child_worker",
    )
    checkpoint = forwarder._SubagentStateCheckpoint(
        bridge_dir,
        forwarder.SubagentForwardState(subagents={"worker": entry}),
    )
    event_loop_thread = threading.current_thread()
    partition_threads: list[threading.Thread] = []
    original_partition = forwarder._partition_subagent_batches

    def record_partition(
        items: list[forwarder._PendingSubagentItem],
    ) -> list[list[forwarder._PendingSubagentItem]]:
        partition_threads.append(threading.current_thread())
        return original_partition(items)

    def reject_request(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected request: {request.url}")

    monkeypatch.setattr(forwarder, "_partition_subagent_batches", record_partition)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(reject_request),
        base_url="http://ap",
    ) as client:
        await forwarder._forward_one_subagent(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            subagents_dir=subagents_dir,
            entry=entry,
            agent_name="claude-native-ui",
            checkpoint=checkpoint,
            item_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            batch_capability=forwarder._SessionEventBatchCapability(),
            status_capability=forwarder._SubagentStatusCapability(),
        )

    assert partition_threads
    assert all(thread is not event_loop_thread for thread in partition_threads)


@pytest.mark.asyncio
async def test_untruncatable_subagent_item_is_dead_lettered_and_checkpointed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One impossible item cannot livelock every later child-history poll."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    subagents_dir = tmp_path / "subagents"
    subagents_dir.mkdir()
    (subagents_dir / "agent-oversized.jsonl").write_text("{}\n", encoding="utf-8")
    item = ClaudeTranscriptItem(
        source_id="oversized-untruncatable",
        item_type="message",
        data={"x" * forwarder.MAX_SUBAGENT_EVENT_BATCH_BYTES: 1},
        response_id="resp_oversized",
    )
    read_result = TranscriptReadResult(
        line_cursor=1,
        byte_offset=3,
        current_response_id=None,
        items=[item],
        record_items=(TranscriptRecordItems(next_byte_offset=3, items=(item,)),),
    )
    monkeypatch.setattr(
        forwarder,
        "read_transcript_items_from_offset",
        lambda *args, **kwargs: read_result,
    )
    entry = forwarder.SubagentEntry(
        subagent_id="oversized",
        child_conversation_id="conv_child_oversized",
    )
    checkpoint = forwarder._SubagentStateCheckpoint(
        bridge_dir,
        forwarder.SubagentForwardState(subagents={"oversized": entry}),
    )

    def reject_request(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected request: {request.url}")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(reject_request),
        base_url="http://ap",
    ) as client:
        await forwarder._forward_one_subagent(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            subagents_dir=subagents_dir,
            entry=entry,
            agent_name="claude-native-ui",
            checkpoint=checkpoint,
            item_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            batch_capability=forwarder._SessionEventBatchCapability(),
            status_capability=forwarder._SubagentStatusCapability(),
        )

    updated = checkpoint.state.subagents["oversized"]
    assert updated.byte_offset == 3
    assert updated.seen_source_ids == (item.source_id,)
    dead_letter = json.loads(
        (bridge_dir / "dead_letter.jsonl").read_text(encoding="utf-8").strip()
    )
    assert dead_letter["payload"]["source_id"] == item.source_id
    assert "no truncatable text" in dead_letter["reason"]
    assert dead_letter["http_status"] == 413

    row = _subagent_drop_row(caplog)
    assert row["session_id"] == "conv_child_oversized"
    assert row["attributes"]["parent_session_id"] == "conv_parent"
    assert row["attributes"]["drop_reason"] == "oversized_item"
    assert row["attributes"]["item_count"] == "1"
    assert row["attributes"]["http_status"] == "413"
    assert row["attributes"]["response_id"] == "resp_oversized"
    assert "exception_type" not in row["attributes"]
    assert "x" * 100 not in json.dumps(row["attributes"])


@pytest.mark.asyncio
async def test_subagent_batches_fall_back_once_for_older_server(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A server that rejects event arrays receives individual events thereafter."""

    def pending(index: int) -> forwarder._PendingSubagentItem:
        return forwarder._PendingSubagentItem(
            item=ClaudeTranscriptItem(
                source_id=f"fallback-{index}",
                item_type="message",
                data={
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": str(index)}],
                },
                response_id="resp_fallback",
            )
        )

    bodies: list[object] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        bodies.append(body)
        if isinstance(body, list):
            return httpx.Response(
                422,
                json={
                    "detail": [
                        {
                            "type": "model_attributes_type",
                            "loc": ["body"],
                            "msg": "Input should be a valid dictionary",
                        }
                    ]
                },
            )
        return httpx.Response(202, json={"queued": False, "item_id": "item_fallback"})

    capability = forwarder._SessionEventBatchCapability()
    caplog.set_level(logging.INFO)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://ap",
    ) as client:
        await forwarder._post_external_conversation_items(
            client,
            session_id="conv_child_one",
            items=[pending(1), pending(2)],
            batch_capability=capability,
        )
        await forwarder._post_external_conversation_items(
            client,
            session_id="conv_child_two",
            items=[pending(3), pending(4)],
            batch_capability=capability,
        )

    assert capability.supported is False
    assert len([body for body in bodies if isinstance(body, list)]) == 1
    individual_source_ids = {
        body["data"]["source_id"] for body in bodies if isinstance(body, dict)
    }
    assert individual_source_ids == {"fallback-1", "fallback-2", "fallback-3", "fallback-4"}
    assert "does not accept session event arrays" in caplog.text


@pytest.mark.asyncio
async def test_subagent_batch_failure_resumes_at_first_unsent_record(tmp_path: Path) -> None:
    """A failed second batch keeps the durable cursor after record 100."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    records = [
        {
            "isSidechain": True,
            "type": "assistant",
            "uuid": f"sa-{index}",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": f"item {index}"}],
            },
        }
        for index in range(150)
    ]
    subagent_jsonl = _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="checkpoint",
        agent_type="Explore",
        description="large history",
        tool_use_id="toolu_checkpoint",
        transcript_records=records,
    )
    state = forwarder.SubagentForwardState(
        subagents={
            "checkpoint": forwarder.SubagentEntry(
                subagent_id="checkpoint",
                child_conversation_id="conv_child_checkpoint",
            )
        }
    )
    attempts: list[list[str]] = []
    failed_second_batch = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal failed_second_batch
        body = json.loads(request.content.decode("utf-8"))
        if not isinstance(body, list):
            return httpx.Response(202, json={})
        source_ids = [event["data"]["source_id"] for event in body]
        attempts.append(source_ids)
        if source_ids[0].startswith("sa-100:") and not failed_second_batch:
            failed_second_batch = True
            return httpx.Response(503, json={"error": "retry"})
        return httpx.Response(
            202,
            json=[{"queued": False, "item_id": f"item-{index}"} for index, _ in enumerate(body)],
        )

    tracker = forwarder._PostRetryTracker(base_delay_s=0.0)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://ap"
    ) as client:
        first = await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=state,
            agent_name="claude-native-ui",
            start_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            item_retry_tracker=tracker,
            status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
        )
        with subagent_jsonl.open("rb") as handle:
            expected_offset = sum(len(handle.readline()) for _ in range(100))
        assert first.subagents["checkpoint"].byte_offset == expected_offset
        persisted = forwarder._read_subagent_forward_state(bridge_dir)
        assert persisted.subagents["checkpoint"].byte_offset == expected_offset

        second = await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=first,
            agent_name="claude-native-ui",
            start_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            item_retry_tracker=tracker,
            status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
        )

    assert second.subagents["checkpoint"].byte_offset == subagent_jsonl.stat().st_size
    attempted_ids = [source_id for batch in attempts for source_id in batch]
    assert all(attempted_ids.count(f"sa-{index}:0:message") == 1 for index in range(100))
    assert all(attempted_ids.count(f"sa-{index}:0:message") == 2 for index in range(100, 150))


@pytest.mark.asyncio
async def test_permanent_batch_failure_redrives_items_individually(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A poison event cannot discard valid siblings from a failed batch."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    subagents_dir = tmp_path / "subagents"
    subagents_dir.mkdir()
    (subagents_dir / "agent-redrive.jsonl").write_text("{}\n", encoding="utf-8")
    items = tuple(
        ClaudeTranscriptItem(
            source_id=source_id,
            item_type="message",
            data={"role": "assistant", "content": [{"type": "text", "text": source_id}]},
            response_id="resp_redrive",
        )
        for source_id in ("before-poison", "poison", "after-poison")
    )
    read_result = TranscriptReadResult(
        line_cursor=3,
        byte_offset=30,
        current_response_id=None,
        items=list(items),
        record_items=tuple(
            TranscriptRecordItems(next_byte_offset=(index + 1) * 10, items=(item,))
            for index, item in enumerate(items)
        ),
    )
    monkeypatch.setattr(
        forwarder,
        "read_transcript_items_from_offset",
        lambda *args, **kwargs: read_result,
    )
    entry = forwarder.SubagentEntry(
        subagent_id="redrive",
        child_conversation_id="conv_child_redrive",
    )
    checkpoint = forwarder._SubagentStateCheckpoint(
        bridge_dir,
        forwarder.SubagentForwardState(subagents={"redrive": entry}),
    )
    request_bodies: list[object] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        request_bodies.append(body)
        if isinstance(body, list):
            return httpx.Response(400, json={"error": "poison in batch"})
        if body["type"] == "external_conversation_item":
            if body["data"]["source_id"] == "poison":
                return httpx.Response(400, json={"error": "poison"})
            return httpx.Response(202, json={"queued": False, "item_id": "item-ok"})
        return httpx.Response(202, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://ap",
    ) as client:
        await forwarder._forward_one_subagent(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            subagents_dir=subagents_dir,
            entry=entry,
            agent_name="claude-native-ui",
            checkpoint=checkpoint,
            item_retry_tracker=forwarder._PostRetryTracker(
                base_delay_s=0.0,
                max_permanent_attempts=1,
            ),
            status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            batch_capability=forwarder._SessionEventBatchCapability(),
            status_capability=forwarder._SubagentStatusCapability(),
        )

    individual_source_ids = [
        body["data"]["source_id"]
        for body in request_bodies
        if isinstance(body, dict) and body.get("type") == "external_conversation_item"
    ]
    assert individual_source_ids == ["before-poison", "poison", "after-poison"]
    updated = checkpoint.state.subagents["redrive"]
    assert updated.byte_offset == 30
    assert updated.seen_source_ids == tuple(item.source_id for item in items)
    dead_letters = [
        json.loads(line)
        for line in (bridge_dir / "dead_letter.jsonl").read_text("utf-8").splitlines()
    ]
    assert [record["payload"]["source_id"] for record in dead_letters] == ["poison"]


@pytest.mark.asyncio
async def test_individual_redrive_honors_not_confirmed_retry_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A fallback item is retried before a not-confirmed 503 is dead-lettered."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    subagents_dir = tmp_path / "subagents"
    subagents_dir.mkdir()
    (subagents_dir / "agent-retry.jsonl").write_text("{}\n", encoding="utf-8")
    item = ClaudeTranscriptItem(
        source_id="retry-not-confirmed",
        item_type="message",
        data={"role": "assistant", "content": [{"type": "text", "text": "hello"}]},
        response_id="resp-retry",
    )
    read_result = TranscriptReadResult(
        line_cursor=1,
        byte_offset=10,
        current_response_id=None,
        items=[item],
        record_items=(TranscriptRecordItems(next_byte_offset=10, items=(item,)),),
    )
    monkeypatch.setattr(
        forwarder,
        "read_transcript_items_from_offset",
        lambda *args, **kwargs: read_result,
    )
    entry = forwarder.SubagentEntry(
        subagent_id="retry",
        child_conversation_id="conv_child_retry",
    )
    checkpoint = forwarder._SubagentStateCheckpoint(
        bridge_dir,
        forwarder.SubagentForwardState(subagents={"retry": entry}),
    )
    batch_attempts = 0
    individual_attempts = 0
    forward_successes = 0

    def note_forward_success() -> None:
        nonlocal forward_successes
        forward_successes += 1

    monkeypatch.setattr(forwarder, "_note_forward_success", note_forward_success)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal batch_attempts, individual_attempts
        body = json.loads(request.content.decode("utf-8"))
        if isinstance(body, list):
            batch_attempts += 1
        else:
            individual_attempts += 1
        return httpx.Response(
            503,
            json={"error": "subagent_delivery_not_confirmed"},
        )

    retry_tracker = forwarder._PostRetryTracker(
        base_delay_s=0.0,
        max_permanent_attempts=1,
        max_not_confirmed_attempts=2,
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://ap"
    ) as client:
        for _ in range(3):
            await forwarder._forward_one_subagent(
                client=client,
                parent_session_id="conv_parent",
                bridge_dir=bridge_dir,
                subagents_dir=subagents_dir,
                entry=checkpoint.state.subagents["retry"],
                agent_name="claude-native-ui",
                checkpoint=checkpoint,
                item_retry_tracker=retry_tracker,
                status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
                batch_capability=forwarder._SessionEventBatchCapability(),
                status_capability=forwarder._SubagentStatusCapability(),
            )

    assert batch_attempts == 2
    assert individual_attempts == 2
    assert forward_successes == 0
    assert checkpoint.state.subagents["retry"].byte_offset == 10
    dead_letters = [
        json.loads(line)
        for line in (bridge_dir / "dead_letter.jsonl").read_text("utf-8").splitlines()
    ]
    assert [record["payload"]["source_id"] for record in dead_letters] == [item.source_id]
    assert dead_letters[0]["reason"] == "delivery not confirmed after retries"

    row = _subagent_drop_row(caplog)
    assert row["session_id"] == "conv_child_retry"
    assert row["attributes"]["drop_reason"] == "delivery_not_confirmed"
    assert row["attributes"]["http_status"] == "503"
    assert row["attributes"]["attempts"] == "2"
    assert row["attributes"]["exception_type"] == "HTTPStatusError"


@pytest.mark.asyncio
async def test_subagent_batch_backoff_survives_new_tail_items(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Appending later items cannot reset backoff for the failing head item."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    subagents_dir = tmp_path / "subagents"
    subagents_dir.mkdir()
    (subagents_dir / "agent-backoff.jsonl").write_text("{}\n", encoding="utf-8")

    def transcript_item(source_id: str) -> ClaudeTranscriptItem:
        return ClaudeTranscriptItem(
            source_id=source_id,
            item_type="message",
            data={"role": "assistant", "content": [{"type": "text", "text": source_id}]},
            response_id="resp-backoff",
        )

    first = transcript_item("first")
    second = transcript_item("second")
    current_result = TranscriptReadResult(
        line_cursor=1,
        byte_offset=10,
        current_response_id=None,
        items=[first],
        record_items=(TranscriptRecordItems(next_byte_offset=10, items=(first,)),),
    )
    monkeypatch.setattr(
        forwarder,
        "read_transcript_items_from_offset",
        lambda *args, **kwargs: current_result,
    )
    entry = forwarder.SubagentEntry(
        subagent_id="backoff",
        child_conversation_id="conv_child_backoff",
    )
    checkpoint = forwarder._SubagentStateCheckpoint(
        bridge_dir,
        forwarder.SubagentForwardState(subagents={"backoff": entry}),
    )
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(502, text="unavailable")

    retry_tracker = forwarder._PostRetryTracker(base_delay_s=60.0)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://ap"
    ) as client:
        await forwarder._forward_one_subagent(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            subagents_dir=subagents_dir,
            entry=entry,
            agent_name="claude-native-ui",
            checkpoint=checkpoint,
            item_retry_tracker=retry_tracker,
            status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            batch_capability=forwarder._SessionEventBatchCapability(),
            status_capability=forwarder._SubagentStatusCapability(),
        )
        current_result = TranscriptReadResult(
            line_cursor=2,
            byte_offset=20,
            current_response_id=None,
            items=[first, second],
            record_items=(
                TranscriptRecordItems(next_byte_offset=10, items=(first,)),
                TranscriptRecordItems(next_byte_offset=20, items=(second,)),
            ),
        )
        await forwarder._forward_one_subagent(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            subagents_dir=subagents_dir,
            entry=entry,
            agent_name="claude-native-ui",
            checkpoint=checkpoint,
            item_retry_tracker=retry_tracker,
            status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            batch_capability=forwarder._SessionEventBatchCapability(),
            status_capability=forwarder._SubagentStatusCapability(),
        )

    assert requests == 1


@pytest.mark.asyncio
async def test_subagent_cleanup_swallows_finished_worker_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Rotation cleanup cannot re-raise an already-finished worker error."""

    async def fail() -> forwarder.SubagentForwardState:
        raise RuntimeError("worker failed")

    task = asyncio.create_task(fail())
    await asyncio.sleep(0)

    await forwarder._cancel_subagent_forward_task(task)

    assert "worker failed during cleanup" in caplog.text


@pytest.mark.asyncio
async def test_subagent_history_drains_eight_children_concurrently(tmp_path: Path) -> None:
    """Independent child conversations are concurrent while each stays ordered."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    entries: dict[str, forwarder.SubagentEntry] = {}
    for index in range(16):
        subagent_id = f"parallel-{index}"
        _seed_subagent_on_disk(
            transcript_path=transcript_path,
            subagent_id=subagent_id,
            agent_type="Explore",
            description="parallel backlog",
            tool_use_id=f"toolu_parallel_{index}",
            transcript_records=[
                {
                    "isSidechain": True,
                    "type": "assistant",
                    "uuid": f"parallel-message-{index}",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": str(index)}],
                    },
                }
            ],
        )
        entries[subagent_id] = forwarder.SubagentEntry(
            subagent_id=subagent_id,
            child_conversation_id=f"conv_child_{index}",
        )

    active = 0
    maximum_active = 0
    release = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, maximum_active
        body = json.loads(request.content.decode("utf-8"))
        if not isinstance(body, list):
            return httpx.Response(202, json={})
        active += 1
        maximum_active = max(maximum_active, active)
        if active == 8:
            release.set()
        try:
            await release.wait()
        finally:
            active -= 1
        return httpx.Response(
            202,
            json=[{"queued": False, "item_id": f"item-{index}"} for index, _ in enumerate(body)],
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://ap"
    ) as client:
        await asyncio.wait_for(
            forwarder._forward_available_subagents(
                client=client,
                parent_session_id="conv_parent",
                bridge_dir=bridge_dir,
                transcript_path=transcript_path,
                state=forwarder.SubagentForwardState(subagents=entries),
                agent_name="claude-native-ui",
                start_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
                item_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
                status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            ),
            timeout=3.0,
        )
    assert maximum_active == 8


@pytest.mark.asyncio
async def test_concurrent_subagent_502s_recover_without_phantom_completion(
    tmp_path: Path,
) -> None:
    """A failed fan-out retries every child before any child can finish idle."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    old_activity = time.time() - forwarder._SUBAGENT_IDLE_THRESHOLD_S - 60
    entries: dict[str, forwarder.SubagentEntry] = {}
    for index in range(5):
        subagent_id = f"recover-{index}"
        _seed_subagent_on_disk(
            transcript_path=transcript_path,
            subagent_id=subagent_id,
            agent_type="Explore",
            description="concurrent retry",
            tool_use_id=f"toolu_recover_{index}",
            transcript_records=[
                {
                    "isSidechain": True,
                    "type": "assistant",
                    "uuid": f"recover-message-{index}",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": str(index)}],
                    },
                }
            ],
        )
        entries[subagent_id] = forwarder.SubagentEntry(
            subagent_id=subagent_id,
            child_conversation_id=f"conv_recover_{index}",
            last_activity_ts=old_activity,
            last_status="running",
        )

    attempts: dict[str, int] = {}
    statuses: list[tuple[str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        child_id = request.url.path.split("/")[-2]
        if isinstance(body, list):
            attempts[child_id] = attempts.get(child_id, 0) + 1
            if attempts[child_id] == 1:
                return httpx.Response(502, text="bad gateway")
            return httpx.Response(202, json=[{"item_id": f"item-{child_id}"}])
        if body.get("type") in {"external_session_status", "subagent.status"}:
            statuses.append((child_id, body))
        return httpx.Response(202, json={})

    tracker = forwarder._PostRetryTracker(
        base_delay_s=0.0,
        max_transient_attempts=3,
    )
    state = forwarder.SubagentForwardState(subagents=entries)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://ap"
    ) as client:
        state = await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=state,
            agent_name="claude-native-ui",
            start_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            item_retry_tracker=tracker,
            status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
        )
        assert statuses == []
        state = await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=state,
            agent_name="claude-native-ui",
            start_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            item_retry_tracker=tracker,
            status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
        )
        quiet_entries = {
            subagent_id: replace(entry, last_activity_ts=old_activity)
            for subagent_id, entry in state.subagents.items()
        }
        state = await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=forwarder.SubagentForwardState(subagents=quiet_entries),
            agent_name="claude-native-ui",
            start_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            item_retry_tracker=tracker,
            status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
        )

    assert set(attempts.values()) == {2}
    assert sorted(child_id for child_id, _ in statuses) == [f"conv_recover_{i}" for i in range(5)]
    assert [body for _, body in statuses] == [
        {"type": "subagent.status", "data": {"idle": True}}
    ] * 5
    assert all(entry.delivery_error is None for entry in state.subagents.values())


@pytest.mark.asyncio
async def test_subagent_item_drop_writes_dead_letter(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    A permanently-rejected sub-agent transcript item is dead-lettered (#1120).

    Drives the real ``_forward_available_subagents`` drop path: the
    ``external_subagent_start`` POST succeeds, the child item POST is rejected
    with a permanent 400 (and the item tracker exhausts on the first failure),
    so the dropped item is appended to ``{bridge_dir}/dead_letter.jsonl`` instead
    of being silently lost.

    :param tmp_path: Pytest temp dir for the bridge dir and transcript.
    """
    forwarder._reset_forward_health()
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="dl1",
        agent_type="Explore",
        description="dead-letter item flow",
        tool_use_id="toolu_dl",
        transcript_records=[
            {
                "isSidechain": True,
                "type": "assistant",
                "uuid": "sa-assistant-dl",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "lost"}],
                },
            },
        ],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        """Accept the start POST; permanently reject the child item POST.

        :param request: Request issued by the forwarder.
        :returns: Canned Omnigent response.
        """
        body = json.loads(request.content.decode("utf-8"))
        if isinstance(body, dict) and body.get("type") == "external_subagent_start":
            return httpx.Response(200, json={"child_session_id": "conv_child_dl"})
        if isinstance(body, list) or body.get("type") == "external_conversation_item":
            return httpx.Response(400, json={"error": "nope"})
        return httpx.Response(202, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://ap",
    ) as client:
        await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=forwarder.SubagentForwardState(subagents={}),
            agent_name="claude-native-ui",
            start_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            item_retry_tracker=forwarder._PostRetryTracker(
                base_delay_s=0.0, max_permanent_attempts=1
            ),
            status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
        )

    forwarder._reset_forward_health()
    dl_path = bridge_dir / "dead_letter.jsonl"
    lines = dl_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["session_id"] == "conv_child_dl"
    assert record["event_type"] == "external_conversation_item"
    assert record["payload"]["item_data"]["content"][0]["text"] == "lost"

    row = _subagent_drop_row(caplog)
    assert row["session_id"] == "conv_child_dl"
    assert row["attributes"]["parent_session_id"] == "conv_parent"
    assert row["attributes"]["drop_reason"] == "permanent_http_failure"
    assert row["attributes"]["http_status"] == "400"
    assert row["attributes"]["attempts"] == "1"
    assert row["attributes"]["exception_type"] == "HTTPStatusError"
    assert "lost" not in json.dumps(row["attributes"])


@pytest.mark.asyncio
async def test_subagent_start_drop_writes_dead_letter(tmp_path: Path) -> None:
    """
    A permanently-rejected sub-agent START is dead-lettered (#1120).

    :param tmp_path: Pytest temp dir for the bridge dir and transcript.
    """
    forwarder._reset_forward_health()
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="dlstart1",
        agent_type="Explore",
        description="dead-letter start flow",
        tool_use_id="toolu_dlstart",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        """Permanently reject the sub-agent start POST.

        :param request: Request issued by the forwarder.
        :returns: Canned Omnigent response.
        """
        return httpx.Response(400, json={"error": "nope"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://ap",
    ) as client:
        await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=forwarder.SubagentForwardState(subagents={}),
            agent_name="claude-native-ui",
            start_retry_tracker=forwarder._PostRetryTracker(
                base_delay_s=0.0, max_permanent_attempts=1
            ),
            item_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
        )

    forwarder._reset_forward_health()
    dl_path = bridge_dir / "dead_letter.jsonl"
    lines = dl_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["session_id"] == "conv_parent"
    assert record["event_type"] == "external_subagent_start"
    assert record["payload"]["subagent_id"] == "dlstart1"
    assert record["payload"]["agent_type"] == "Explore"


@pytest.mark.asyncio
async def test_timed_out_batch_is_split_not_dropped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A batch whose POST never got a response is re-driven item by item.

    A read timeout on a 100-item batch is usually the batch's own size against
    the flat post timeout, so retrying the same payload cannot clear it. The
    server never rejected the items, so they must be split rather than
    dead-lettered.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    subagents_dir = tmp_path / "subagents"
    subagents_dir.mkdir()
    (subagents_dir / "agent-split.jsonl").write_text("{}\n", encoding="utf-8")
    items = [
        ClaudeTranscriptItem(
            source_id=f"item-{index}",
            item_type="message",
            data={"role": "assistant", "content": [{"type": "text", "text": f"m{index}"}]},
            response_id="resp-split",
        )
        for index in range(3)
    ]
    read_result = TranscriptReadResult(
        line_cursor=1,
        byte_offset=30,
        current_response_id=None,
        items=items,
        record_items=(TranscriptRecordItems(next_byte_offset=30, items=tuple(items)),),
    )
    monkeypatch.setattr(
        forwarder,
        "read_transcript_items_from_offset",
        lambda *args, **kwargs: read_result,
    )
    entry = forwarder.SubagentEntry(
        subagent_id="split",
        child_conversation_id="conv_child_split",
    )
    checkpoint = forwarder._SubagentStateCheckpoint(
        bridge_dir,
        forwarder.SubagentForwardState(subagents={"split": entry}),
    )
    batch_attempts = 0
    individual_source_ids: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal batch_attempts
        body = json.loads(request.content.decode("utf-8"))
        if isinstance(body, list):
            batch_attempts += 1
            raise httpx.ReadTimeout("batch too large for the post budget", request=request)
        if isinstance(body, dict) and body.get("type") == "external_conversation_item":
            individual_source_ids.append(body["data"]["source_id"])
        return httpx.Response(204)

    retry_tracker = forwarder._PostRetryTracker(
        base_delay_s=0.0,
        max_transient_attempts=2,
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://ap"
    ) as client:
        for _ in range(3):
            await forwarder._forward_one_subagent(
                client=client,
                parent_session_id="conv_parent",
                bridge_dir=bridge_dir,
                subagents_dir=subagents_dir,
                entry=checkpoint.state.subagents["split"],
                agent_name="claude-native-ui",
                checkpoint=checkpoint,
                item_retry_tracker=retry_tracker,
                status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
                batch_capability=forwarder._SessionEventBatchCapability(),
                status_capability=forwarder._SubagentStatusCapability(),
            )

    assert batch_attempts == 2
    assert individual_source_ids == [item.source_id for item in items]
    assert not (bridge_dir / "dead_letter.jsonl").exists()
    updated = checkpoint.state.subagents["split"]
    assert updated.byte_offset == 30
    assert updated.seen_source_ids == tuple(item.source_id for item in items)
