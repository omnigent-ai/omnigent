"""Regression tests for Codex reasoning-stream paragraph boundaries.

Two user-visible failure modes on a Codex-harness session's reasoning stream:

1. **Streaming hangs (tail not flushed)**: the last words of a reasoning
   paragraph (which Codex ends without a trailing newline) sit in the
   client's flush buffer and only appear "a long while later", glued to the
   start of the next paragraph.

2. **Paragraphs are not split (clobbered)**: consecutive reasoning items
   render concatenated with no separator — the report's
   ``"…folder names.I have the runner…"`` instead of
   ``"…folder names.\\n\\nI have the runner…"``.

Both stem from the same missing boundary: Codex's app-server emits each
reasoning paragraph as a distinct item (``item/reasoning/textDelta`` with a
new ``itemId``), but ``omnigent/inner/codex_executor.py``'s handler ignores
``itemId`` and emits every delta as a bare ``reasoning_text`` chunk — it
never emits a ``ReasoningChunk(event_type="reasoning_started")`` marker
when a new reasoning item begins. Downstream (the workflow's
``_event_to_sse_dict`` → ``response.reasoning.started`` → the SDK/SPA
block-stream folder) already handles that marker correctly: it flushes the
previous paragraph's buffered tail and inserts a ``\\n\\n`` separator.

These tests drive the REAL Codex executor session (with the app-server
subprocess boundary stubbed, exactly like ``tests/inner/test_codex_executor``)
with the two-paragraph reasoning stream from the bug report, then fold the
executor's output through the REAL ``ExecutorAdapter._translate_event``
mapping, the REAL client SSE parser, and the REAL client ``BlockStream``.
They assert on the user-visible rendered reasoning text, so they fail for
this bug specifically and pass once the executor emits the per-item boundary.

Journey (user-observable)
-------------------------
1. Run a codex agent with a simple custom harness.
2. Ask a reasoning-heavy prompt ("Can you explain the arch of
   https://github.com/omnigent-ai/omnigent"), then follow up with
   "can you dive into the runner flow a little more?".
3. Watch the streaming reasoning display:
   - (bug 1) the final words of the first reasoning paragraph are withheld
     and only appear much later;
   - (bug 2) the next paragraph is clobbered onto the end of the first with
     no space/newline ("…folder names.I have the runner…").
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock

from omnigent_client._blocks import ReasoningChunk as ReasoningChunkBlock
from omnigent_client._events import (
    MessageDone,
    ResponseCompleted,
    ResponseCreated,
)
from omnigent_client._sse import _parse_event
from omnigent_client._stream import BlockStream
from omnigent_client._types import Response

from omnigent.inner.codex_executor import _CodexAppServerSession
from omnigent.inner.executor import Executor, TurnComplete
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter

# The exact reasoning text from the bug report's screenshot.
_PARA_1 = (
    "I'm drilling into runner now: first the module boundaries, then the "
    "control path for policies, tool dispatch, approvals, and transports so "
    "I can describe the actual flow instead of just the folder names."
)
_PARA_2 = "I have the runner's main HTTP app now."


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()


class _FakePipe:
    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        return None

    async def read(self, n: int) -> bytes:
        return b""


class _FakeProcess:
    def __init__(self) -> None:
        self.stdin = _FakePipe()
        self.stdout = _FakePipe()
        self.stderr = _FakePipe()
        self.returncode: int | None = None
        self.pid = 12345

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode or 0


async def _codex_turn_events() -> list[Any]:
    """Drive the real Codex app-server session through the reported turn.

    Replays the app-server notification stream a real Codex run produces for
    the report's journey: two distinct reasoning items (one per thought
    paragraph, each with its own ``itemId``, ending without a trailing
    newline) followed by the final answer.

    :returns: The executor events ``run_turn`` yielded.
    """
    session = _CodexAppServerSession(
        codex_path="/bin/echo",
        cwd="/tmp/workspace",
        env={},
        tool_executor=None,
    )
    session.start = AsyncMock()
    session._proc = _FakeProcess()
    session.thread_id = "thread-1"
    session._request = AsyncMock(return_value={"result": {"turn": {"id": "turn-1"}}})

    async def _inject() -> None:
        # Deliver after turn/start, as the live app-server does — queueing
        # before run_turn would hit the pre-turn stale-event drain instead.
        await asyncio.sleep(0.01)
        # Paragraph 1: its own reasoning item; ends mid-sentence, no newline.
        session._events.put_nowait(
            {
                "method": "item/reasoning/textDelta",
                "params": {"turnId": "turn-1", "itemId": "rs-1", "delta": _PARA_1},
            }
        )
        # Paragraph 2: a NEW reasoning item (new itemId) — the boundary the
        # executor must surface as a reasoning_started marker.
        session._events.put_nowait(
            {
                "method": "item/reasoning/textDelta",
                "params": {"turnId": "turn-1", "itemId": "rs-2", "delta": _PARA_2},
            }
        )
        session._events.put_nowait(
            {
                "method": "item/completed",
                "params": {
                    "turnId": "turn-1",
                    "item": {
                        "id": "msg-1",
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": "Here is the runner flow.",
                    },
                },
            }
        )

    inject_task = asyncio.create_task(_inject())
    events = [
        event
        async for event in session.run_turn(
            messages=[
                {"role": "user", "content": "can you dive into the runner flow a little more?"}
            ],
            tools=[],
            system_prompt="",
            model="gpt-5.4-mini",
            cwd=".",
            sandbox="workspace-write",
        )
    ]
    await inject_task
    return events


class _RecordingCtx:
    """Minimal TurnContext stand-in that records the adapter's SSE emits."""

    def __init__(self) -> None:
        self.response_id = "resp_1"
        self.emitted: list[Any] = []
        self.provider_usage: dict[str, Any] | None = None

    def emit(self, event: Any) -> None:
        self.emitted.append(event)


def _to_client_events(executor_events: list[Any]) -> list[Any]:
    """Map executor events onto client SSE events through the REAL pipeline.

    Each executor event goes through the real
    ``ExecutorAdapter._translate_event`` (producing the typed server SSE
    events), then each server event's wire form is parsed by the real client
    SSE parser — so a regression in either mapping fails these tests too.

    :param executor_events: Events yielded by ``run_turn``.
    :returns: The equivalent ``omnigent_client`` event stream.
    """
    adapter = ExecutorAdapter(executor_factory=Executor)
    ctx = _RecordingCtx()
    final_text = ""
    for event in executor_events:
        if isinstance(event, TurnComplete):
            final_text = event.response
        adapter._translate_event(event, ctx)  # deliberate white-box drive of the real mapping
    response = Response(id="resp_1", status="completed", model="codex-test")
    out: list[Any] = [ResponseCreated(response=response)]
    for server_event in ctx.emitted:
        payload = server_event.model_dump(mode="json", exclude_none=True)
        client_event = _parse_event(server_event.type, payload)
        if client_event is not None:
            out.append(client_event)
    out.append(MessageDone(content=[{"type": "output_text", "text": final_text}]))
    out.append(ResponseCompleted(response=response))
    return out


class _ReplaySession:
    """Fake client session replaying a fixed event list."""

    def __init__(self, events: list[Any]) -> None:
        self._events = events

    async def send(
        self,
        input: Any,
        *,
        files: Any = None,
    ) -> AsyncIterator[Any]:
        for event in self._events:
            yield event


async def _rendered_reasoning_chunks() -> list[str]:
    """Run the full pipeline and return the rendered reasoning chunk texts.

    Real Codex executor parsing → the documented SSE mapping → the real
    client ``BlockStream`` folder — the same path the TUI/SPA reasoning
    display consumes.

    :returns: ``ReasoningChunk`` block texts in emission order.
    """
    executor_events = await _codex_turn_events()
    client_events = _to_client_events(executor_events)
    stream = BlockStream()
    chunks: list[str] = []
    async for block in stream.stream(_ReplaySession(client_events), "follow-up"):
        if isinstance(block, ReasoningChunkBlock):
            chunks.append(block.text)
    return chunks


def test_codex_reasoning_paragraphs_are_split() -> None:
    """Consecutive Codex reasoning paragraphs must render with a separator.

    Reproduces the paragraph-clobbering bug: on unfixed code the executor drops the
    per-item boundary, so the rendered reasoning reads
    ``"…folder names.I have the runner…"`` — the second paragraph clobbered
    onto the first with zero space/newline.
    """
    chunks = _run(_rendered_reasoning_chunks())
    rendered = "".join(chunks)
    assert _PARA_1 in rendered, f"first reasoning paragraph missing: {rendered!r}"
    assert _PARA_2 in rendered, f"second reasoning paragraph missing: {rendered!r}"
    assert "folder names.I have" not in rendered, (
        f"reasoning paragraphs clobbered together with no separator : {rendered!r}"
    )
    assert "folder names.\n\nI have" in rendered, (
        "expected a blank-line separator between consecutive reasoning "
        f"paragraphs, got: {rendered!r}"
    )


def test_codex_reasoning_tail_flushed_at_paragraph_boundary() -> None:
    """The end of a reasoning paragraph must flush before the next begins.

    Reproduces the withheld-tail bug: the paragraph's last words (``"names."``)
    stay buffered until the *next* paragraph's text forces a flush, so the
    user sees the sentence truncated and then, much later, glued to the new
    paragraph. The chunk that renders the tail of paragraph 1 must not also
    carry paragraph 2's opening words.
    """
    chunks = _run(_rendered_reasoning_chunks())
    tail_chunks = [chunk for chunk in chunks if "names." in chunk]
    assert tail_chunks, f"paragraph-1 tail never rendered: {chunks!r}"
    for chunk in tail_chunks:
        assert "I have" not in chunk, (
            "paragraph-1 tail was withheld until paragraph 2 streamed and "
            f"rendered glued to it: {chunk!r}"
        )
