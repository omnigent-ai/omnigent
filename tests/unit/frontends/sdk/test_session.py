"""Unit tests for Session.query / Session.query(stream=True).

These tests exercise the convenience wrappers by mocking
``Session.send()`` directly. The real event → block folding
path is covered separately in ``test_stream.py``; here we only
verify the wrapping behavior (collect → str, stream → chunks).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from omnigent_client._events import (
    MessageDone,
    ResponseCompleted,
    ResponseCreated,
    ResponseInProgress,
    StreamEvent,
    TextDelta,
)
from omnigent_client._session import Session
from omnigent_client._types import Response


def _make_response(
    response_id: str = "resp_1",
    status: str = "completed",
    model: str = "test-agent",
) -> Response:
    """Minimal Response for synthesizing SSE events in tests."""
    return Response(id=response_id, status=status, model=model)


class _ScriptedSession(Session):
    """A Session subclass whose ``send()`` replays a fixed event list.

    ``Session.query()`` dispatches to ``self._collect_query`` /
    ``self._stream_query``, which in turn use ``BlockStream`` to fold
    events from ``self.send(...)``. We subclass Session so those
    helpers are actually present; we override ``__init__`` to avoid
    needing a real client, and override ``send`` to replay a script.

    :param events: Pre-baked events to yield on every ``send()`` call.
    """

    def __init__(self, events: list[StreamEvent]) -> None:
        # Deliberately skip Session.__init__ — we don't need a client
        # for these tests, and faking one would be more work than it's
        # worth. query() only reads self._collect_query / self._stream_query
        # (inherited) and the overridden self.send (below).
        self._events = events

    async def send(  # type: ignore[override]
        self,
        input: Any,
        *,
        files: Any = None,
        instructions: Any = None,
    ) -> AsyncIterator[StreamEvent]:
        for event in self._events:
            yield event


# ── query() — non-streaming returns final text ──────────────────────────


@pytest.mark.asyncio()
async def test_query_returns_final_text_simple() -> None:
    """A single text response → query() returns QueryResult with joined text."""
    session = _ScriptedSession(
        events=[
            ResponseCreated(response=_make_response()),
            ResponseInProgress(response=_make_response(status="in_progress")),
            TextDelta(delta="Hello "),
            TextDelta(delta="world"),
            MessageDone(content=[]),
            ResponseCompleted(response=_make_response()),
        ]
    )

    # Session.query dispatches through BlockStream → TextDone, which
    # query() collects into result.text. A wrong/empty value here
    # means the wrapper dropped the text.
    result = await session.query("hi")

    assert result.text == "Hello world"
    # No FileBlock events in the script → files list must be empty,
    # not None (we guarantee a list for type stability).
    assert result.files == []


@pytest.mark.asyncio()
async def test_query_empty_response_returns_empty_result() -> None:
    """No text events → result.text is ''. Must not raise or return None."""
    session = _ScriptedSession(
        events=[
            ResponseCreated(response=_make_response()),
            ResponseInProgress(response=_make_response(status="in_progress")),
            # No TextDelta / MessageDone — e.g. a cancelled turn.
            ResponseCompleted(response=_make_response()),
        ]
    )
    result = await session.query("hi")

    # Empty string is the contract, not None — keeps the return type
    # stable (`QueryResult`) regardless of whether the agent produced text.
    assert result.text == ""
    assert result.files == []


# ── query(stream=True) — yields text chunks as they arrive ──────────────


@pytest.mark.asyncio()
async def test_query_stream_yields_text_chunks() -> None:
    """stream=True → QueryStream yielding str chunks, in order."""
    session = _ScriptedSession(
        events=[
            ResponseCreated(response=_make_response()),
            ResponseInProgress(response=_make_response(status="in_progress")),
            TextDelta(delta="Hello "),
            TextDelta(delta="world"),
            MessageDone(content=[]),
            ResponseCompleted(response=_make_response()),
        ]
    )

    stream = await session.query("hi", stream=True)
    chunks = [c async for c in stream]

    # Concatenating all yielded chunks must equal the full text.
    # A wrong result here means either TextChunk blocks weren't
    # passed through, or their ``text`` field was not extracted.
    assert "".join(chunks) == "Hello world"

    # At least one chunk. Non-empty proof the stream actually yielded
    # (a broken wrapper could return an empty iterator and still
    # satisfy the join-equals-expected check above when that expected
    # value is also "").
    assert len(chunks) >= 1

    # No FileBlock events in the script → files property is empty
    # after exhaustion.
    assert stream.files == []


@pytest.mark.asyncio()
async def test_query_stream_rejects_double_iteration() -> None:
    """QueryStream is single-use — a second ``async for`` raises."""
    session = _ScriptedSession(
        events=[
            ResponseCreated(response=_make_response()),
            ResponseCompleted(response=_make_response()),
        ]
    )
    stream = await session.query("hi", stream=True)
    # First iteration is allowed and yields nothing meaningful here.
    _ = [c async for c in stream]

    # Second iteration must raise; single-use is the documented contract.
    # Without this check, callers could silently replay a spent stream
    # and get an empty result, masking bugs.
    with pytest.raises(RuntimeError, match="single-use"):
        async for _ in stream:
            pass
