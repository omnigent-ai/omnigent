"""Unit tests for ``_snapshot_terminal_events`` — the stream snapshot's terminal replay.

The session-stream snapshot asks the runner for its terminals every time a
viewer opens or reconnects a stream. That request must be marked passive so it
does not reset the runner's idle watchdog; otherwise a browser tab left open
keeps an idle runner alive indefinitely.
"""

from __future__ import annotations

import httpx
import pytest

from omnigent.runner.transports.ws_tunnel.frames import PASSIVE_REQUEST_HEADER
from omnigent.server.routes.sessions.routes_events import _snapshot_terminal_events

pytestmark = pytest.mark.asyncio

_SESSION_ID = "conv_abc123"


async def test_terminal_fetch_is_marked_passive() -> None:
    """The snapshot's runner request carries the passive header.

    :returns: None.
    """
    seen: list[httpx.Request] = []

    def _runner(request: httpx.Request) -> httpx.Response:
        """Record the request and return two terminals.

        :param request: Request the snapshot sent to the runner.
        :returns: A terminals page.
        """
        seen.append(request)
        return httpx.Response(200, json={"data": [{"id": "term_1"}, {"id": "term_2"}]})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_runner), base_url="http://runner"
    ) as runner_client:
        events = await _snapshot_terminal_events(runner_client, _SESSION_ID)

    assert len(seen) == 1
    assert seen[0].url.path == f"/v1/sessions/{_SESSION_ID}/resources/terminals"
    assert dict(seen[0].url.params) == {"order": "asc", "limit": "1000"}
    assert seen[0].headers.get(PASSIVE_REQUEST_HEADER) == "1"
    assert events == [
        {"type": "session.resource.created", "resource": {"id": "term_1"}},
        {"type": "session.resource.created", "resource": {"id": "term_2"}},
    ]


@pytest.mark.parametrize("failure", ["status", "exception"])
async def test_terminal_fetch_failure_yields_no_events(failure: str) -> None:
    """A failing runner contributes no snapshot events instead of raising.

    :param failure: ``"status"`` for a non-200 reply, ``"exception"`` for a
        transport error.
    :returns: None.
    """

    def _runner(request: httpx.Request) -> httpx.Response:
        """Fail the request the way the case asks.

        :param request: Request the snapshot sent to the runner.
        :returns: A 503 response when ``failure`` is ``"status"``.
        :raises httpx.ConnectError: When ``failure`` is ``"exception"``.
        """
        if failure == "exception":
            raise httpx.ConnectError("runner unreachable", request=request)
        return httpx.Response(503)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_runner), base_url="http://runner"
    ) as runner_client:
        events = await _snapshot_terminal_events(runner_client, _SESSION_ID)

    assert events == []
