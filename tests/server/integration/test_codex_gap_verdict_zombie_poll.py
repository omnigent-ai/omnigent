"""A gap-landing verdict must survive to the re-park even
when the previous chunk's disconnect was never detected (zombie waiter).

The codex-native harness long-polls ``POST /hooks/codex-elicitation-request``
in chunks, re-POSTing the SAME JSON-RPC envelope, so the elicitation id
(``codex_elicitation_id``) is stable across re-parks. A proxy (e.g. Databricks
Apps) can sever a chunk client-side while holding the backend connection open:
the harness abandons the chunk, but the server never observes a disconnect and
the chunk's waiter stays parked as a zombie.

``test_codex_hook_gap_verdict_returned_on_repost`` covers the DETECTED-sever
gap (the waiter unwound, the resolve tombstones, the re-park consumes) and
passes. This module covers the UNDETECTED sever: ``_resolve_elicitation`` finds
the zombie future still registered under the stable id, sets the verdict on it
— written to a connection nobody reads — and writes NO tombstone (it only
tombstones when no future is registered). The next chunk re-parks the same id,
finds nothing, and re-publishes the gate as a fresh pending elicitation: the
operator's answer is lost and the gate re-asks. The resolve endpoint returns
2xx throughout, so the resolving client cannot detect the loss and retry.

Fails on the unfixed build; passes once a verdict that lands on an abandoned
waiter is still delivered to a re-park of the same stable elicitation id.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import httpx
import pytest

from omnigent.runtime import pending_elicitations, session_stream
from omnigent.server.routes import sessions as sessions_route
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio

# The harness re-POSTs this SAME envelope every chunk (stable JSON-RPC id), so
# codex_elicitation_id(session, method, 12) is identical across re-parks.
_CODEX_GATE: dict[str, Any] = {
    "id": 12,
    "method": "mcpServer/elicitation/request",
    "params": {
        "threadId": "thread_gapverdict",
        "turnId": "turn_gapverdict",
        "serverName": "files",
        "mode": "form",
        "message": "Overwrite the file?",
        "requestedSchema": {"type": "object", "properties": {"ok": {"type": "string"}}},
    },
}

# How long the re-park (chunk 2) may wait for its verdict. On the unfixed
# build it parks for the hook's full timeout, so a short budget converts
# "verdict lost" into a deterministic failure.
_REPARK_VERDICT_BUDGET_S = 3.0


async def _create_session(client: httpx.AsyncClient, agent_id: str) -> str:
    """Create a minimal session and return its id."""
    resp = await client.post("/v1/sessions", json={"agent_id": agent_id})
    assert resp.status_code == 201, f"create failed: {resp.status_code} {resp.text}"
    return resp.json()["id"]


async def _drain_until_elicitation(
    session_id: str,
    *,
    timeout_s: float = 5.0,
) -> dict[str, Any]:
    """Return the next ``response.elicitation_request`` event on the stream."""
    async with asyncio.timeout(timeout_s):
        async for event in session_stream.subscribe(session_id):
            if event.get("type") == "response.elicitation_request":
                return event
    raise AssertionError("subscribe loop ended without an elicitation event")


async def test_gap_verdict_survives_an_undetected_severed_poll(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A verdict landing while a zombie waiter is parked reaches the re-park.

    Journey: chunk 1 parks the gate → the harness abandons it but the server
    never detects the sever (proxy holds the backend connection) → the
    operator resolves the card via the resolve URL (the web ApprovalCard
    path) → chunk 2 re-parks the same stable envelope and MUST receive the
    verdict instead of re-publishing the gate as a fresh pending card.
    """

    async def _never_disconnects(_request: Any) -> None:
        """Model the proxy-held backend connection: no disconnect, ever."""
        await asyncio.sleep(3600)

    monkeypatch.setattr(sessions_route, "_poll_request_disconnect", _never_disconnects)
    monkeypatch.setattr(sessions_route, "_HARNESS_ELICITATION_REPARK_GRACE_S", 0.25)
    pending_elicitations.reset_for_tests()
    agent = await create_test_agent(client, "test-codex-zombie-gap")
    session_id = await _create_session(client, agent["id"])

    # Chunk 1: the harness's long-poll. It is abandoned client-side (this
    # test never acts on its result) but its waiter stays parked
    # server-side — the undetected-sever state.
    drain_task = asyncio.create_task(_drain_until_elicitation(session_id))
    await asyncio.sleep(0.05)
    chunk1_task = asyncio.create_task(
        client.post(
            f"/v1/sessions/{session_id}/hooks/codex-elicitation-request",
            json=_CODEX_GATE,
        )
    )
    event = await drain_task
    elicitation_id = event["elicitation_id"]

    # The operator answers the surfaced card during the gap, via the same
    # resolve URL the web ApprovalCard posts to. The endpoint acknowledges
    # with 2xx — the client has no way to see the verdict went nowhere.
    verdict = await client.post(
        f"/v1/sessions/{session_id}/elicitations/{elicitation_id}/resolve",
        json={"action": "accept", "content": {"ok": "go"}},
    )
    assert verdict.status_code == 202, verdict.text

    # Chunk 2: the harness re-invokes with the same stable envelope. The
    # verdict must be delivered here; parking again (until the client budget
    # below expires) means the operator's answer was lost and the gate
    # re-asked.
    repark_event_task = asyncio.create_task(
        _drain_until_elicitation(session_id, timeout_s=_REPARK_VERDICT_BUDGET_S)
    )
    try:
        chunk2 = await asyncio.wait_for(
            client.post(
                f"/v1/sessions/{session_id}/hooks/codex-elicitation-request",
                json=_CODEX_GATE,
            ),
            timeout=_REPARK_VERDICT_BUDGET_S,
        )
    except TimeoutError:
        republished = None
        with contextlib.suppress(TimeoutError, AssertionError):
            republished = await repark_event_task
        raise AssertionError(
            "gap-landing verdict LOST: the re-park parked instead of returning "
            "the operator's verdict; the gate re-asked as a fresh pending "
            f"elicitation: {republished is not None} (id={elicitation_id})"
        ) from None
    finally:
        if not repark_event_task.done():
            repark_event_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await repark_event_task
        # The zombie chunk's response (if any) is written to a connection the
        # harness abandoned; reap the task so it doesn't outlive the loop.
        if not chunk1_task.done():
            chunk1_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, httpx.HTTPError):
            await chunk1_task
        # Drain any deferred clears so they don't outlive the test's loop.
        for task in set(sessions_route._deferred_elicitation_clear_tasks):
            with contextlib.suppress(Exception):
                await asyncio.wait_for(task, timeout=5.0)
        pending_elicitations.reset_for_tests()

    assert chunk2.status_code == 200, chunk2.text
    assert chunk2.json() == {"action": "accept", "content": {"ok": "go"}, "_meta": None}, (
        f"re-park returned {chunk2.text!r} instead of the gap-landing verdict"
    )
