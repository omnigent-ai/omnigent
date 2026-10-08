"""Deltas tests for Codex forwarder."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from omnigent.harnesses.codex_native import forwarder as fwd
from tests.harnesses.codex_native.forwarder._support import (
    _RecordingClient,
)


@pytest.mark.asyncio
async def test_reasoning_delta_opens_block_then_continues() -> None:
    """
    Codex reasoning deltas mirror as external_output_reasoning_delta (#1254).

    The first delta of a reasoning item opens the block (``started=True``
    → ``response.reasoning.started``); subsequent deltas for the same item
    continue it (``started=False``). Reasoning was previously dropped — only
    the effort *level* synced, never the thinking text.
    """
    client = _RecordingClient()
    state = fwd._CodexForwarderState()
    coalescer = fwd._OutputTextDeltaCoalescer(
        client,
        "conv_x",
        flush_interval_seconds=60.0,
        flush_char_threshold=1000,
    )

    await fwd._handle_reasoning_delta(
        {"turnId": "turn_1", "itemId": "item_r", "delta": "Let me "},
        coalescer,
        state,
    )
    await fwd._handle_reasoning_delta(
        {"turnId": "turn_1", "itemId": "item_r", "delta": "think."},
        coalescer,
        state,
    )
    await coalescer.flush()
    await coalescer.close()

    assert client.posts == [
        (
            "/v1/sessions/conv_x/events",
            {
                "type": "external_output_reasoning_delta",
                "data": {"delta": "Let me think.", "started": True},
            },
        ),
    ]


@pytest.mark.asyncio
async def test_reasoning_delta_new_item_reopens_block() -> None:
    """
    A reasoning delta for a new item id opens a fresh block.

    Multi-step turns (reason → tool → reason) emit a second reasoning item;
    its first delta must re-open the block so the web UI starts a new
    "thinking" section rather than appending to the prior one.
    """
    client = _RecordingClient()
    state = fwd._CodexForwarderState()
    coalescer = fwd._OutputTextDeltaCoalescer(
        client,
        "conv_x",
        flush_interval_seconds=60.0,
        flush_char_threshold=1000,
    )

    await fwd._handle_reasoning_delta({"itemId": "item_a", "delta": "first"}, coalescer, state)
    await fwd._handle_reasoning_delta({"itemId": "item_b", "delta": "second"}, coalescer, state)
    await coalescer.flush()
    await coalescer.close()

    started_flags = [post[1]["data"]["started"] for post in client.posts]
    assert started_flags == [True, True]


@pytest.mark.asyncio
async def test_reasoning_delta_skips_empty_non_opening_delta() -> None:
    """
    An empty delta that does not open a block is dropped (no noise post).

    The block-opening delta is always posted (even empty, to emit
    ``response.reasoning.started``); a later empty continuation carries
    nothing to render and must not POST.
    """
    client = _RecordingClient()
    state = fwd._CodexForwarderState()
    coalescer = fwd._OutputTextDeltaCoalescer(
        client,
        "conv_x",
        flush_interval_seconds=60.0,
        flush_char_threshold=1000,
    )

    # Opening delta (empty) still posts to open the block.
    await fwd._handle_reasoning_delta({"itemId": "item_r", "delta": ""}, coalescer, state)
    # Empty continuation for the same item is dropped.
    await fwd._handle_reasoning_delta({"itemId": "item_r", "delta": ""}, coalescer, state)
    await coalescer.flush()
    await coalescer.close()

    assert len(client.posts) == 1
    assert client.posts[0][1]["data"] == {"delta": "", "started": True}


def _coalescer(client: object) -> fwd._OutputTextDeltaCoalescer:
    """Build a delta coalescer wired to ``client``."""
    return fwd._OutputTextDeltaCoalescer(
        client=client,
        session_id="conv_idle",
        flush_interval_seconds=0.05,
        flush_char_threshold=64,
    )


@pytest.mark.asyncio
async def test_delta_coalescer_close_returns_when_its_worker_is_cancelled() -> None:
    """``close()`` must not park on a marker its cancelled worker can never resolve.

    ``asyncio.run`` cancels every task before resuming any, so when the forwarder
    resumes first its ``finally`` calls ``close()`` while the worker is cancelled but
    not yet ``done()``. The marker is queued with nobody left to complete it.
    """
    coalescer = _coalescer(_RecordingClient())
    coalescer._ensure_worker()
    await asyncio.sleep(0)
    worker = coalescer._worker_task
    assert worker is not None

    worker.cancel()
    # Deliberately NOT awaited: this reproduces the teardown ordering where the
    # worker is cancelled but has not run yet, which no `done()` check can catch.
    assert not worker.done()

    loop = asyncio.get_running_loop()
    started = loop.time()
    await asyncio.wait_for(coalescer.close(), timeout=fwd._DELTA_MARKER_TIMEOUT_SECONDS + 5.0)

    assert coalescer._worker_task is None
    assert loop.time() - started < fwd._DELTA_MARKER_TIMEOUT_SECONDS


@pytest.mark.asyncio
async def test_delta_coalescer_close_gives_up_on_a_wedged_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker still alive but stuck mid-post must not hold ``close()`` forever.

    This is the one case the bound exists for: the worker owns the marker, so it is
    off the queue, and the worker is running, so racing it does not help either.
    """

    class _HangingClient:
        """Client whose post never returns, wedging the worker inside a flush."""

        def __init__(self) -> None:
            self.entered = asyncio.Event()

        async def post(self, *args: object, **kwargs: object) -> None:
            self.entered.set()
            await asyncio.sleep(3600)

    monkeypatch.setattr(fwd, "_DELTA_MARKER_TIMEOUT_SECONDS", 0.1)
    client = _HangingClient()
    coalescer = _coalescer(client)
    await coalescer.append("x", message_id="m1")
    await asyncio.wait_for(client.entered.wait(), timeout=5.0)
    worker = coalescer._worker_task
    assert worker is not None

    try:
        await asyncio.wait_for(coalescer.close(), timeout=fwd._DELTA_MARKER_TIMEOUT_SECONDS + 5.0)

        # Once the close deadline expires, cancel and reap the worker so it
        # cannot outlive the HTTP client that owns its in-flight POST.
        assert coalescer._worker_task is None
        assert worker.cancelled()
    finally:
        if not worker.done():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)


@pytest.mark.asyncio
async def test_delta_coalescer_worker_survives_an_already_settled_marker() -> None:
    """Resolving a marker whose future is already settled must not kill the worker.

    ``set_result`` on a settled future raises ``InvalidStateError`` inside the
    worker, so every later delta would otherwise be dropped without a word.
    """
    client = _RecordingClient()
    coalescer = _coalescer(client)
    coalescer._ensure_worker()
    await asyncio.sleep(0)

    settled: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    settled.cancel()
    coalescer._queue.put_nowait(fwd._DeltaFlushBarrier(done=settled))
    await asyncio.sleep(0.1)

    assert coalescer._worker_task is not None
    assert not coalescer._worker_task.done()

    posts_before = len(client.posts)
    await coalescer.append("still here", message_id="m1")
    await asyncio.wait_for(coalescer.flush(), timeout=5.0)
    assert len(client.posts) > posts_before


@pytest.mark.asyncio
async def test_delta_coalescer_survives_a_cancelled_flush_caller() -> None:
    """A cancelled ``flush()`` caller must not kill the worker.

    The caller's cancellation can settle its own future. Resolving it again raises
    ``InvalidStateError`` inside the worker, so every later delta would otherwise
    be silently dropped.
    """
    client = _RecordingClient()
    coalescer = _coalescer(client)
    coalescer._ensure_worker()
    await asyncio.sleep(0)

    pending = asyncio.ensure_future(coalescer.flush())
    await asyncio.sleep(0)
    pending.cancel()
    await asyncio.gather(pending, return_exceptions=True)
    await asyncio.sleep(0.1)

    assert coalescer._worker_task is not None
    assert not coalescer._worker_task.done()

    posts_before = len(client.posts)
    await coalescer.append("still here", message_id="m1")
    await asyncio.wait_for(coalescer.flush(), timeout=5.0)
    assert len(client.posts) > posts_before


@pytest.mark.asyncio
async def test_delta_coalescer_restarts_an_unexpectedly_stopped_worker() -> None:
    """A dead worker drops its stale queue before later streaming restarts."""
    client = _RecordingClient()
    coalescer = _coalescer(client)
    coalescer._ensure_worker()
    worker = coalescer._worker_task
    assert worker is not None

    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)
    stale = fwd._DeltaChunk(message_id="old", tool_call_id=None, delta="stale")
    coalescer._queue.put_nowait(stale)
    coalescer._queued_chars = len(stale.delta)
    await coalescer.flush()
    await coalescer.append("recovered", message_id="m1")
    await coalescer.flush()
    await coalescer.close()

    assert [post[1]["data"]["delta"] for post in client.posts] == ["recovered"]


@pytest.mark.asyncio
async def test_delta_coalescer_batches_newline_heavy_tool_output() -> None:
    """Line-oriented command output is batched instead of POSTed per line."""
    client = _RecordingClient()
    coalescer = fwd._OutputTextDeltaCoalescer(
        client,
        "conv_x",
        flush_interval_seconds=60.0,
        flush_char_threshold=100_000,
    )
    chunks = [f"line {index}\n" for index in range(200)]

    for chunk in chunks:
        await coalescer.append_tool_output(chunk, call_id="call_1")
    await coalescer.flush()
    await coalescer.close()

    assert client.posts == [
        (
            "/v1/sessions/conv_x/events",
            {
                "type": "external_tool_output_delta",
                "data": {"call_id": "call_1", "delta": "".join(chunks)},
            },
        )
    ]


@pytest.mark.asyncio
async def test_delta_coalescer_preserves_mixed_stream_order() -> None:
    """Assistant and reasoning batches retain their Codex arrival order."""
    client = _RecordingClient()
    coalescer = fwd._OutputTextDeltaCoalescer(
        client,
        "conv_x",
        flush_interval_seconds=60.0,
        flush_char_threshold=100_000,
    )

    await coalescer.append("answer one", message_id="message_1")
    await coalescer.append_reasoning("think ", started=True)
    await coalescer.append_reasoning("more", started=False)
    await coalescer.append("answer two", message_id="message_2")
    await coalescer.flush()
    await coalescer.close()

    assert [post[1]["type"] for post in client.posts] == [
        "external_output_text_delta",
        "external_output_reasoning_delta",
        "external_output_text_delta",
    ]
    assert [post[1]["data"]["delta"] for post in client.posts] == [
        "answer one",
        "think more",
        "answer two",
    ]


@pytest.mark.asyncio
async def test_delta_coalescer_does_not_drop_healthy_burst_at_queue_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ready worker gets a chance to drain before the queue sheds output."""
    monkeypatch.setattr(fwd, "_DELTA_QUEUE_CHAR_LIMIT", 10)
    client = _RecordingClient()
    coalescer = fwd._OutputTextDeltaCoalescer(
        client,
        "conv_x",
        flush_interval_seconds=60.0,
        flush_char_threshold=100,
    )

    await coalescer.append("first!", message_id="m1")
    await coalescer.append("second", message_id="m1")
    await coalescer.flush()
    await coalescer.close()

    assert [post[1]["data"]["delta"] for post in client.posts] == ["first!second"]


@pytest.mark.asyncio
async def test_delta_coalescer_overflow_drops_backlog_before_durable_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow relay sheds queued previews but preserves the completion boundary."""

    class _SlowFirstClient(_RecordingClient):
        """Hold the first delta POST while the queue crosses its byte budget."""

        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()
            self.calls = 0

        async def post(
            self,
            url: str,
            *,
            json: dict,
            timeout: float | None = None,
        ) -> httpx.Response:
            self.calls += 1
            if self.calls == 1:
                self.entered.set()
                await self.release.wait()
            return await super().post(url, json=json, timeout=timeout)

    monkeypatch.setattr(fwd, "_DELTA_QUEUE_CHAR_LIMIT", 19)
    client = _SlowFirstClient()
    coalescer = fwd._OutputTextDeltaCoalescer(
        client,
        "conv_x",
        flush_interval_seconds=60.0,
        flush_char_threshold=1,
    )

    await coalescer.append("stale", message_id="old")
    await asyncio.wait_for(client.entered.wait(), timeout=5.0)
    await coalescer.append("queued stale", message_id="old")
    await coalescer.append("overflow", message_id="old")
    completed = asyncio.create_task(
        fwd._handle_completed_event(
            client,  # type: ignore[arg-type]
            session_id="conv_x",
            params={
                "threadId": "thread_1",
                "turnId": "turn_1",
                "item": {"id": "item_1", "type": "agentMessage", "text": "final answer"},
            },
            delta_coalescer=coalescer,
            forwarder_state=None,
        )
    )
    await asyncio.sleep(0)
    assert not completed.done()
    client.release.set()
    await asyncio.wait_for(completed, timeout=5.0)

    await coalescer.append("fresh", message_id="new")
    await coalescer.flush()
    await coalescer.close()

    assert client.calls == 3
    assert client.posts == [
        (
            "/v1/sessions/conv_x/events",
            {
                "type": "external_output_text_delta",
                "data": {
                    "delta": "stale",
                    "message_id": "old",
                    "index": 0,
                    "final": False,
                },
            },
        ),
        (
            "/v1/sessions/conv_x/events",
            {
                "type": "external_conversation_item",
                "data": {
                    "item_type": "message",
                    "item_data": {
                        "role": "assistant",
                        "agent": "codex-native-ui",
                        "content": [{"type": "output_text", "text": "final answer"}],
                    },
                    "response_id": "codex_turn_1",
                    "message_id": "codex:thread_1:turn_1:agentMessage:item_1",
                    "source_id": "thread_1:turn_1:item_1",
                },
            },
        ),
        (
            "/v1/sessions/conv_x/events",
            {
                "type": "external_output_text_delta",
                "data": {
                    "delta": "fresh",
                    "message_id": "new",
                    "index": 0,
                    "final": False,
                },
            },
        ),
    ]


@pytest.mark.asyncio
async def test_delta_coalescer_sheds_small_slow_backlog_before_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sub-limit backlog cannot continue posting after the durable item."""

    class _SlowDeltaClient(_RecordingClient):
        async def post(
            self,
            url: str,
            *,
            json: dict,
            timeout: float | None = None,
        ) -> httpx.Response:
            if json["type"] == "external_output_text_delta":
                await asyncio.sleep(0.04)
            return await super().post(url, json=json, timeout=timeout)

    monkeypatch.setattr(fwd, "_DELTA_FLUSH_GRACE_SECONDS", 0.02)
    monkeypatch.setattr(fwd, "_DELTA_MARKER_TIMEOUT_SECONDS", 0.1)
    client = _SlowDeltaClient()
    coalescer = fwd._OutputTextDeltaCoalescer(
        client,
        "conv_x",
        flush_interval_seconds=60.0,
        flush_char_threshold=5,
    )

    await coalescer.append("first", message_id="old")
    await asyncio.sleep(0)
    await coalescer.append("second", message_id="old")
    await fwd._handle_completed_event(
        client,  # type: ignore[arg-type]
        session_id="conv_x",
        params={
            "threadId": "thread_1",
            "turnId": "turn_1",
            "item": {"id": "item_1", "type": "agentMessage", "text": "final answer"},
        },
        delta_coalescer=coalescer,
        forwarder_state=None,
    )
    await asyncio.sleep(0.05)
    await coalescer.close()

    assert [post[1]["type"] for post in client.posts] == [
        "external_output_text_delta",
        "external_conversation_item",
    ]


@pytest.mark.asyncio
async def test_transient_delta_post_uses_one_short_attempt() -> None:
    """Lossy previews fail fast instead of retrying behind durable events."""

    class _FailingClient:
        def __init__(self) -> None:
            self.timeouts: list[float | None] = []

        async def post(
            self,
            url: str,
            *,
            json: dict,
            timeout: float | None = None,
        ) -> httpx.Response:
            self.timeouts.append(timeout)
            return httpx.Response(503, request=httpx.Request("POST", url))

    fwd._reset_forward_health()
    try:
        client = _FailingClient()

        await fwd._post_output_text_delta(
            client,  # type: ignore[arg-type]
            "conv_x",
            "preview",
            message_id="m1",
            index=0,
            final=False,
        )

        assert client.timeouts == [fwd._DELTA_POST_TIMEOUT_SECONDS]
    finally:
        fwd._reset_forward_health()
