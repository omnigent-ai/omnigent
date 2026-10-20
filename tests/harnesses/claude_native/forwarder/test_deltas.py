"""Deltas tests for Claude-native forwarding."""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

import omnigent.harnesses.claude_native.forwarder as forwarder
from omnigent.harnesses.claude_native.bridge import (
    ClaudeMessageDelta,
    MessageDeltaReadResult,
    prepare_bridge_dir,
    record_hook_event,
)
from tests.harnesses.claude_native.forwarder._support import (
    _get_recorded_request,
    _start_recording_server,
)


@dataclass
class _CapturedDeltaPost:
    """
    One ``POST /events`` body captured during a delta-forwarding test.

    :param url_path: Request URL path, e.g. ``"/v1/sessions/conv_x/events"``.
    :param body: Parsed JSON request body.
    """

    url_path: str
    body: dict[str, Any] | list[dict[str, Any]]


def _write_deltas_file(bridge_dir: Path, records: list[dict[str, Any]]) -> None:
    """
    Append delta records to ``message_deltas.jsonl`` as the hook would.

    :param bridge_dir: Bridge directory.
    :param records: Delta dicts to serialize one-per-line, e.g.
        ``[{"message_id": "m1", "index": 0, "final": True, "delta": "hi"}]``.
    :returns: None.
    """
    bridge_dir.mkdir(parents=True, exist_ok=True)
    with (bridge_dir / "message_deltas.jsonl").open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def _delta_capture_client(
    captured: list[_CapturedDeltaPost],
    status_code: int = 202,
) -> httpx.AsyncClient:
    """
    Build an AsyncClient whose ``/events`` POSTs are captured.

    :param captured: List appended to with each observed POST body.
    :param status_code: HTTP status the stub returns, e.g. ``202`` for
        success or ``500`` to exercise the best-effort drop path.
    :returns: An ``httpx.AsyncClient`` bound to the capturing transport.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(
            _CapturedDeltaPost(url_path=request.url.path, body=json.loads(request.content))
        )
        return httpx.Response(status_code, json={"queued": False})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://ap")


async def test_forward_available_deltas_batches_and_advances_offset(tmp_path: Path) -> None:
    """
    Chunks available in one poll are POSTed in one ordered batch.

    Proves the forwarder turns deltas-file lines into the exact event
    shape the Omnigent route expects (delta + message_id + index + final) and
    advances+persists the byte offset so the next poll resumes after
    them. Fails if a field is dropped (UI can't scope/order the buffer)
    or the offset doesn't persist (chunks re-POST on restart).
    """
    bridge_dir = prepare_bridge_dir("conv_x", bridge_id="b1", workspace=tmp_path)
    _write_deltas_file(
        bridge_dir,
        [
            {"message_id": "m1", "index": 0, "final": False, "delta": "Hello "},
            {"message_id": "m1", "index": 1, "final": True, "delta": "world"},
        ],
    )
    captured: list[_CapturedDeltaPost] = []
    seen: dict[tuple[str, int], None] = {}
    async with _delta_capture_client(captured) as client:
        new_state = await forwarder._forward_available_deltas(
            client=client,
            session_id="conv_x",
            bridge_dir=bridge_dir,
            state=forwarder.DeltaForwardState(),
            seen_keys=seen,
        )

    assert [c.url_path for c in captured] == ["/v1/sessions/conv_x/events"]
    # Full event shape proves every field survived hook → file → POST.
    assert [c.body for c in captured] == [
        [
            {
                "type": "external_output_text_delta",
                "data": {"delta": "Hello ", "message_id": "m1", "index": 0, "final": False},
            },
            {
                "type": "external_output_text_delta",
                "data": {"delta": "world", "message_id": "m1", "index": 1, "final": True},
            },
        ]
    ]
    # Offset advanced to EOF and was persisted, so a reload resumes past
    # the two chunks instead of re-POSTing them.
    assert new_state.byte_offset == os.path.getsize(bridge_dir / "message_deltas.jsonl")
    assert forwarder._read_delta_forward_state(bridge_dir).byte_offset == new_state.byte_offset


async def test_forward_available_deltas_dedupes_by_message_id_and_index(tmp_path: Path) -> None:
    """
    A repeated ``(message_id, index)`` is POSTed at most once.

    The byte offset prevents re-reads on the happy path, but a file
    truncation/rewind can replay records; the in-memory seen-ring must
    still suppress the duplicate. Fails if the dedupe key is wrong (or
    absent), which would double-render a chunk in the live preview.
    """
    bridge_dir = prepare_bridge_dir("conv_x", bridge_id="b1", workspace=tmp_path)
    _write_deltas_file(
        bridge_dir,
        [
            {"message_id": "m1", "index": 0, "final": False, "delta": "dup"},
            {"message_id": "m1", "index": 0, "final": False, "delta": "dup"},
            {"message_id": "m1", "index": 1, "final": True, "delta": "next"},
        ],
    )
    captured: list[_CapturedDeltaPost] = []
    seen: dict[tuple[str, int], None] = {}
    async with _delta_capture_client(captured) as client:
        await forwarder._forward_available_deltas(
            client=client,
            session_id="conv_x",
            bridge_dir=bridge_dir,
            state=forwarder.DeltaForwardState(),
            seen_keys=seen,
        )
    # The duplicate (m1, 0) is collapsed: only the first (m1,0) and the
    # distinct (m1,1) are POSTed — 1 batch, not 3 requests.
    assert len(captured) == 1
    assert isinstance(captured[0].body, list)
    assert [(e["data"]["message_id"], e["data"]["index"]) for e in captured[0].body] == [
        ("m1", 0),
        ("m1", 1),
    ]


async def test_forward_available_deltas_falls_back_for_old_servers(tmp_path: Path) -> None:
    bridge_dir = prepare_bridge_dir("conv_x", bridge_id="b1", workspace=tmp_path)
    _write_deltas_file(
        bridge_dir,
        [{"message_id": "m1", "index": i, "final": i == 2, "delta": str(i)} for i in range(3)],
    )
    captured: list[dict[str, Any] | list[dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.append(body)
        return httpx.Response(422 if isinstance(body, list) else 202)

    capability = forwarder._SessionEventBatchCapability()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://ap"
    ) as client:
        await forwarder._forward_available_deltas(
            client=client,
            session_id="conv_x",
            bridge_dir=bridge_dir,
            state=forwarder.DeltaForwardState(),
            seen_keys={},
            batch_capability=capability,
        )
    assert isinstance(captured[0], list)
    assert [event["data"]["index"] for event in captured[1:]] == [0, 1, 2]
    assert capability.supported is False


async def test_legacy_delta_failure_does_not_skip_later_chunks(tmp_path: Path) -> None:
    bridge_dir = prepare_bridge_dir("conv_x", bridge_id="b1", workspace=tmp_path)
    _write_deltas_file(
        bridge_dir,
        [{"message_id": "m1", "index": i, "final": i == 2, "delta": str(i)} for i in range(3)],
    )
    posted: list[dict[str, Any] | list[dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        posted.append(body)
        if isinstance(body, list):
            return httpx.Response(422)
        return httpx.Response(500 if body["data"]["index"] == 1 else 202)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://ap"
    ) as client:
        await forwarder._forward_available_deltas(
            client=client,
            session_id="conv_x",
            bridge_dir=bridge_dir,
            state=forwarder.DeltaForwardState(),
            seen_keys={},
        )
    assert isinstance(posted[0], list)
    assert [body["data"]["index"] for body in posted[1:]] == [0, 1, 2]


async def test_forward_available_deltas_bounds_batch_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cap = 220
    monkeypatch.setattr(forwarder, "_MAX_DELTA_BATCH_BYTES", cap)
    bridge_dir = prepare_bridge_dir("conv_x", bridge_id="b1", workspace=tmp_path)
    _write_deltas_file(
        bridge_dir,
        [{"message_id": "m1", "index": i, "final": i == 3, "delta": "🚀" * 15} for i in range(4)],
    )
    captured: list[_CapturedDeltaPost] = []
    async with _delta_capture_client(captured) as client:
        await forwarder._forward_available_deltas(
            client=client,
            session_id="conv_x",
            bridge_dir=bridge_dir,
            state=forwarder.DeltaForwardState(),
            seen_keys={},
        )
    assert len(captured) > 1
    assert all(
        len(forwarder.encode_session_event_batch(c.body)) <= cap
        for c in captured
        if isinstance(c.body, list)
    )
    assert [
        event["data"]["index"]
        for captured_post in captured
        for event in (
            captured_post.body if isinstance(captured_post.body, list) else [captured_post.body]
        )
    ] == list(range(4))


async def test_oversized_single_delta_does_not_block_next(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(forwarder, "_MAX_DELTA_BATCH_BYTES", 200)
    bridge_dir = prepare_bridge_dir("conv_x", bridge_id="b1", workspace=tmp_path)
    _write_deltas_file(
        bridge_dir,
        [
            {"message_id": "m1", "index": 0, "final": False, "delta": "🚀" * 100},
            {"message_id": "m1", "index": 1, "final": True, "delta": "done"},
        ],
    )
    captured: list[_CapturedDeltaPost] = []
    async with _delta_capture_client(captured) as client:
        await forwarder._forward_available_deltas(
            client=client,
            session_id="conv_x",
            bridge_dir=bridge_dir,
            state=forwarder.DeltaForwardState(),
            seen_keys={},
        )
    assert [c.body["data"]["index"] for c in captured] == [0, 1]
    assert all(isinstance(c.body, dict) for c in captured)


async def test_failed_delta_batch_still_sends_next_batch(tmp_path: Path) -> None:
    bridge_dir = prepare_bridge_dir("conv_x", bridge_id="b1", workspace=tmp_path)
    _write_deltas_file(
        bridge_dir,
        [{"message_id": "m1", "index": i, "final": i == 39, "delta": str(i)} for i in range(40)],
    )
    posted: list[list[dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert isinstance(body, list)
        posted.append(body)
        return httpx.Response(500 if len(posted) == 1 else 202)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://ap"
    ) as client:
        state = await forwarder._forward_available_deltas(
            client=client,
            session_id="conv_x",
            bridge_dir=bridge_dir,
            state=forwarder.DeltaForwardState(),
            seen_keys={},
        )
    assert [len(batch) for batch in posted] == [32, 8]
    assert state.byte_offset == os.path.getsize(bridge_dir / "message_deltas.jsonl")


async def test_forward_available_deltas_bounds_batch_count(tmp_path: Path) -> None:
    bridge_dir = prepare_bridge_dir("conv_x", bridge_id="b1", workspace=tmp_path)
    _write_deltas_file(
        bridge_dir,
        [{"message_id": "m1", "index": i, "final": i == 39, "delta": str(i)} for i in range(40)],
    )
    captured: list[_CapturedDeltaPost] = []
    async with _delta_capture_client(captured) as client:
        await forwarder._forward_available_deltas(
            client=client,
            session_id="conv_x",
            bridge_dir=bridge_dir,
            state=forwarder.DeltaForwardState(),
            seen_keys={},
        )
    assert [len(c.body) for c in captured] == [32, 8]
    assert isinstance(captured[0].body, list)
    assert isinstance(captured[1].body, list)
    assert [e["data"]["index"] for c in captured for e in c.body] == list(range(40))


async def test_forward_available_deltas_drops_on_http_error(tmp_path: Path) -> None:
    """
    A failed delta POST is swallowed and the offset still advances.

    Deltas are an ephemeral preview; the authoritative final message
    arrives via ``external_conversation_item`` regardless, so a transient
    Omnigent blip must not raise or wedge the tail. Fails if the error
    propagates (would crash the forwarder loop) or the offset stalls
    (would re-POST the failed chunk forever).
    """
    bridge_dir = prepare_bridge_dir("conv_x", bridge_id="b1", workspace=tmp_path)
    _write_deltas_file(
        bridge_dir, [{"message_id": "m1", "index": 0, "final": True, "delta": "boom"}]
    )
    captured: list[_CapturedDeltaPost] = []
    seen: dict[tuple[str, int], None] = {}
    async with _delta_capture_client(captured, status_code=500) as client:
        new_state = await forwarder._forward_available_deltas(
            client=client,
            session_id="conv_x",
            bridge_dir=bridge_dir,
            state=forwarder.DeltaForwardState(),
            seen_keys=seen,
        )
    # The POST was attempted (and 500'd) but no exception escaped, and
    # the offset moved past the chunk so it won't be retried endlessly.
    assert len(captured) == 1
    assert new_state.byte_offset == os.path.getsize(bridge_dir / "message_deltas.jsonl")


def test_delta_forward_state_round_trips(tmp_path: Path) -> None:
    """
    The delta cursor persists and reloads its byte offset.

    Fails if the on-disk shape changes without the reader keeping up —
    a forwarder restart would then re-stream the whole deltas file.
    """
    bridge_dir = prepare_bridge_dir("conv_x", bridge_id="b1", workspace=tmp_path)
    # A fresh read with no state file starts at offset 0.
    assert forwarder._read_delta_forward_state(bridge_dir).byte_offset == 0
    forwarder._write_delta_forward_state(bridge_dir, forwarder.DeltaForwardState(byte_offset=512))
    assert forwarder._read_delta_forward_state(bridge_dir).byte_offset == 512


async def test_post_external_output_text_delta_sends_expected_payload(tmp_path: Path) -> None:
    """
    The single-delta POST helper sends the canonical event body.

    Guards the wire contract between the forwarder and the AP
    ``/events`` route in isolation from the file-tailing logic.
    """
    captured: list[_CapturedDeltaPost] = []
    async with _delta_capture_client(captured) as client:
        await forwarder._post_external_output_text_delta(
            client,
            session_id="conv_y",
            delta=ClaudeMessageDelta(message_id="m9", index=4, final=True, delta="tok"),
        )
    assert captured == [
        _CapturedDeltaPost(
            url_path="/v1/sessions/conv_y/events",
            body={
                "type": "external_output_text_delta",
                "data": {"delta": "tok", "message_id": "m9", "index": 4, "final": True},
            },
        )
    ]


def _posted_event_types(request: dict[str, Any]) -> list[str]:
    """
    Return the event types in one recorded ``/events`` POST, batch or single.

    :param request: Request recorded by ``_start_recording_server``.
    :returns: Event types in body order, e.g. ``["external_output_text_delta"]``.
    """
    body = request["body"]
    events = body if isinstance(body, list) else [body]
    return [event.get("type") for event in events]


async def _event_types_through_first_delta(server: Any) -> list[str]:
    """
    Collect posted event types up to and including the first streamed delta.

    :param server: Recording server from ``_start_recording_server``.
    :returns: Event types in arrival order.
    """
    types: list[str] = []
    while "external_output_text_delta" not in types:
        types.extend(_posted_event_types(await _get_recorded_request(server)))
    return types


def _user_prompt_line(text: str) -> str:
    """
    Serialize one Claude transcript user-prompt record.

    :param text: Prompt text, e.g. ``"what next?"``.
    :returns: JSONL line including the trailing newline.
    """
    record = {"type": "user", "uuid": f"user-{text}", "message": {"role": "user", "content": text}}
    return json.dumps(record) + "\n"


def _start_turn_files(bridge_dir: Path, transcript_path: Path, *, submit: bool) -> None:
    """
    Write a Claude session start, an optional prompt submit, and a reply's first chunk.

    :param bridge_dir: Bridge directory.
    :param transcript_path: Claude transcript path recorded on the hooks.
    :param submit: Whether to record a ``UserPromptSubmit`` hook.
    :returns: None.
    """
    transcript_path.touch()
    hook = {"session_id": "claude-session", "transcript_path": str(transcript_path)}
    record_hook_event(bridge_dir, {"hook_event_name": "SessionStart", **hook})
    if submit:
        record_hook_event(bridge_dir, {"hook_event_name": "UserPromptSubmit", **hook})
    _write_deltas_file(
        bridge_dir, [{"message_id": "m1", "index": 0, "final": False, "delta": "Start "}]
    )


async def _stop_forwarder(task: asyncio.Task[None], server: Any, thread: Any) -> None:
    """
    Cancel the forwarder loop and stop its recording server.

    :param task: Running ``forward_claude_transcript_to_session`` task.
    :param server: Recording server.
    :param thread: Server thread.
    :returns: None.
    """
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    server.shutdown()
    server.server_close()
    thread.join(timeout=5.0)


def _forwarder_task(base_url: str, bridge_dir: Path) -> asyncio.Task[None]:
    """
    Start the forwarder loop against a recording server.

    :param base_url: Recording server URL.
    :param bridge_dir: Bridge directory to tail.
    :returns: The running loop task.
    """
    return asyncio.create_task(
        forwarder.forward_claude_transcript_to_session(
            base_url=base_url,
            headers={},
            session_id="conv_order",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            start_at_end=False,
            poll_interval_s=0.01,
        )
    )


@pytest.mark.asyncio
async def test_prompt_reaches_session_before_reply_chunk_read_in_same_poll(
    tmp_path: Path,
) -> None:
    """A prompt and its reply's first chunk visible together post prompt-first."""
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    _start_turn_files(bridge_dir, transcript_path, submit=False)
    transcript_path.write_text(_user_prompt_line("what next?"), encoding="utf-8")
    server, thread, base_url = _start_recording_server()
    task = _forwarder_task(base_url, bridge_dir)
    try:
        types = await _event_types_through_first_delta(server)
    finally:
        await _stop_forwarder(task, server, thread)

    assert "external_conversation_item" in types
    assert types.index("external_conversation_item") < types.index("external_output_text_delta")


@pytest.mark.asyncio
async def test_reply_chunk_waits_for_a_submitted_prompt_recorded_after_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude can stream a reply before recording its prompt; the chunk waits."""
    monkeypatch.setattr(forwarder, "_PROMPT_RECORD_WAIT_S", 30.0)
    chunk_reads = 0
    read_deltas = forwarder._read_available_deltas

    async def counting_read(
        bridge_dir: Path, state: forwarder.DeltaForwardState
    ) -> MessageDeltaReadResult:
        nonlocal chunk_reads
        result = await read_deltas(bridge_dir, state)
        chunk_reads += bool(result.deltas)
        return result

    monkeypatch.setattr(forwarder, "_read_available_deltas", counting_read)
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    _start_turn_files(bridge_dir, transcript_path, submit=True)
    server, thread, base_url = _start_recording_server()
    task = _forwarder_task(base_url, bridge_dir)
    try:
        # A second read of the same chunk means the first poll finished without posting it.
        deadline = time.monotonic() + 10.0
        while chunk_reads < 2 and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        held = [request["body"] for request in list(server.requests.queue)]
        with transcript_path.open("a", encoding="utf-8") as handle:
            handle.write(_user_prompt_line("what next?"))
        types = await _event_types_through_first_delta(server)
    finally:
        await _stop_forwarder(task, server, thread)

    assert chunk_reads >= 2
    assert all(
        "external_output_text_delta" not in _posted_event_types({"body": body}) for body in held
    )
    assert "external_conversation_item" in types
    assert types.index("external_conversation_item") < types.index("external_output_text_delta")


@pytest.mark.asyncio
async def test_reply_chunk_posts_after_bounded_wait_without_prompt_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A submit that never produces a prompt item delays the preview only briefly."""
    monkeypatch.setattr(forwarder, "_PROMPT_RECORD_WAIT_S", 0.2)
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    _start_turn_files(bridge_dir, transcript_path, submit=True)
    server, thread, base_url = _start_recording_server()
    started = time.monotonic()
    task = _forwarder_task(base_url, bridge_dir)
    try:
        types = await _event_types_through_first_delta(server)
        waited_s = time.monotonic() - started
    finally:
        await _stop_forwarder(task, server, thread)

    assert "external_conversation_item" not in types
    assert waited_s >= 0.2


@pytest.mark.asyncio
async def test_subagent_prompt_submit_does_not_hold_the_reply_chunk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a main-session submit holds a preview; a subagent's prompt does not."""
    monkeypatch.setattr(forwarder, "_PROMPT_RECORD_WAIT_S", 30.0)
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    _start_turn_files(bridge_dir, transcript_path, submit=False)
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "claude-session",
            "transcript_path": str(transcript_path),
            "agent_id": "agent-1",
        },
    )
    server, thread, base_url = _start_recording_server()
    started = time.monotonic()
    task = _forwarder_task(base_url, bridge_dir)
    try:
        types = await _event_types_through_first_delta(server)
        waited_s = time.monotonic() - started
    finally:
        await _stop_forwarder(task, server, thread)

    assert "external_conversation_item" not in types
    assert waited_s < 10.0


def _chunk(message_id: str, index: int) -> ClaudeMessageDelta:
    """
    Build one streamed chunk.

    :param message_id: Claude message id, e.g. ``"m1"``.
    :param index: Chunk index within the message.
    :returns: The chunk.
    """
    return ClaudeMessageDelta(message_id=message_id, index=index, final=False, delta="x")


def test_prompt_order_gate_holds_only_new_messages_until_prompt_forwarded() -> None:
    """The gate holds a new message, never one already streaming, until release."""
    gate = forwarder._PromptOrderGate()
    streaming = {("m1", 0): None}
    assert not gate.holds([_chunk("m2", 0)], streaming)

    gate.note_submit(time.time())
    assert gate.holds([_chunk("m2", 0)], streaming)
    assert gate.holds([_chunk("m1", 1), _chunk("m2", 0)], streaming)
    assert not gate.holds([_chunk("m1", 1)], streaming)

    gate.note_prompt_forwarded()
    assert not gate.holds([_chunk("m2", 0)], streaming)


def test_prompt_order_gate_ignores_submits_already_forwarded_or_unknown() -> None:
    """A re-read or untimestamped submit does not restart the wait."""
    gate = forwarder._PromptOrderGate()
    submitted_at = time.time()
    gate.note_prompt_forwarded()
    gate.note_submit(submitted_at - 1.0)
    gate.note_submit(None)
    assert not gate.holds([_chunk("m2", 0)], {})


def test_prompt_order_gate_releases_after_wait_bound() -> None:
    """The wait expires so a prompt that never appears cannot stall previews."""
    gate = forwarder._PromptOrderGate()
    submitted_at = time.time()
    gate.note_submit(submitted_at)
    gate.noticed_at -= forwarder._PROMPT_RECORD_WAIT_S
    assert not gate.holds([_chunk("m2", 0)], {})
    assert gate.submitted_at is None

    gate.note_submit(submitted_at)
    assert not gate.holds([_chunk("m3", 0)], {})


@pytest.mark.parametrize(
    ("content", "skip_user_messages"),
    [
        ("skipped prompt", True),
        (
            "<command-name>/model</command-name>\n<command-message>model</command-message>\n"
            "<command-args>example-model</command-args>",
            False,
        ),
        ("<bash-input>pwd</bash-input>", False),
    ],
    ids=["skipped_user_message", "slash_command", "terminal_command"],
)
@pytest.mark.asyncio
async def test_forwarding_a_prompt_item_releases_the_gate_without_waiting(
    tmp_path: Path, content: str, skip_user_messages: bool
) -> None:
    """Each prompt-like item a submit can produce releases a held preview."""
    transcript_path = tmp_path / "session.jsonl"
    record = {"type": "user", "uuid": "prompt-1", "message": {"role": "user", "content": content}}
    transcript_path.write_text(json.dumps(record) + "\n")
    gate = forwarder._PromptOrderGate()
    gate.note_submit(time.time())
    assert gate.holds([_chunk("m2", 0)], {})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(202, json={})),
        base_url="http://test",
    ) as client:
        await forwarder._forward_available_items(
            client=client,
            session_id="conv_x",
            bridge_dir=tmp_path / "bridge",
            agent_name="test-agent",
            state=forwarder.TranscriptForwardState(transcript_path, 0),
            retry_tracker=forwarder._PostRetryTracker(),
            dedupe=forwarder._ForwardDedupeState(),
            skip_user_messages=skip_user_messages,
            prompt_gate=gate,
        )

    assert gate.submitted_at is None
    assert not gate.holds([_chunk("m2", 0)], {})
