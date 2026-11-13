"""Deltas tests for Claude-native forwarding."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

import omnigent.harnesses.claude_native.forwarder as forwarder
from omnigent.harnesses.claude_native.bridge import (
    ClaudeMessageDelta,
    prepare_bridge_dir,
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
