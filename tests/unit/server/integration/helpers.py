"""
Shared SSE-collection helpers for server integration tests.

The integration tests routinely connect to ``/v1/responses`` with
``stream=True`` and need to assert on the *sequence* of SSE events.
``CapturedEvent`` formalizes the ``(event_type, parsed_data)`` shape
the suite has been using ad-hoc, and ``collect_sse_events()`` runs
the full event stream into a list — including the synthetic
``"done"`` marker for the trailing ``[DONE]`` line.

Required by the concurrency tests. Lives here rather than
``conftest.py`` because these are plain helpers, not pytest
fixtures.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx
from httpx_sse import aconnect_sse


@dataclass
class CapturedEvent:
    """
    One SSE event captured from a streaming response.

    :param event: The SSE ``event:`` field, e.g.
        ``"response.created"``, ``"response.output_text.delta"``,
        ``"response.completed"``, or the synthetic ``"done"`` marker
        for the trailing ``[DONE]`` line.
    :param data: The parsed ``data:`` payload. A dict for normal
        events, the literal string ``"[DONE]"`` for the terminal
        marker.
    """

    event: str
    # dict[str, Any] | str — most events are JSON dicts, the [DONE]
    # marker is a bare string.
    data: dict[str, Any] | str


async def collect_sse_events(
    client: httpx.AsyncClient,
    *,
    method: str,
    url: str,
    json_body: dict[str, Any],
) -> list[CapturedEvent]:
    """
    Connect to an SSE endpoint and collect every event into a list.

    Used by tests that assert on event ordering, content, or counts.
    The trailing ``[DONE]`` line is captured as a ``CapturedEvent``
    with ``event="done"`` and ``data="[DONE]"`` so tests can
    distinguish a clean stream close from a mid-stream disconnect.

    :param client: The HTTP client to use, typically the test
        ``httpx.AsyncClient``.
    :param method: HTTP method, e.g. ``"POST"``.
    :param url: Target URL, e.g. ``"/v1/responses"``.
    :param json_body: JSON body for the request, e.g.
        ``{"model": "test-agent", "input": "Hi", "stream": True}``.
    :returns: List of :class:`CapturedEvent` in arrival order. An
        empty list means the server closed the stream without
        emitting any events (likely an error before the SSE
        handshake).
    """
    events: list[CapturedEvent] = []
    async with aconnect_sse(
        client,
        method,
        url,
        json=json_body,
    ) as event_source:
        async for sse in event_source.aiter_sse():
            if sse.data == "[DONE]":
                events.append(CapturedEvent(event="done", data="[DONE]"))
            else:
                events.append(CapturedEvent(event=sse.event, data=json.loads(sse.data)))
    return events
