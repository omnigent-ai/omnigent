"""Runner relay must treat an HTTP-error stream response as a transport loss.

The server's runner SSE relay accepted an HTTP-error response on
``GET /v1/sessions/{id}/stream`` as though it were an open event stream:
``_relay_runner_stream_once`` iterated ``resp.aiter_text()`` without checking
``resp.status_code``. A JSON error body carries no SSE frames, so the parse
loop finished and the function returned normally instead of raising
``_RelayTransportLost``. The supervisor then skipped its bounded recovery
loop, the readiness heartbeat never arrived, and ``_ensure_runner_relay_ready``
failed the session with ``RUNNER_UNAVAILABLE`` instead of riding out the
transient error and reconnecting.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import httpx
import pytest

# Generous ceiling for awaiting relay tasks on a shared xdist runner.
_TASK_TIMEOUT_S = 10.0

# Readiness wait kept short so the pre-fix (no-recovery) path fails fast; the
# post-fix path reconnects within the 0.5s retry interval, well inside this.
_READY_TIMEOUT_S = 2.5

_HEARTBEAT_SSE = b'data: {"type": "session.heartbeat"}\n\ndata: [DONE]\n\n'

# Statuses the relay previously accepted as clean, empty streams.
_ERROR_STATUSES = [503, 401, 403, 404, 429, 500]


def _error_then_heartbeat_client(
    status_code: int,
) -> tuple[httpx.AsyncClient, list[int]]:
    """Build a runner client whose first stream request errors, then recovers.

    :param status_code: HTTP error status returned on the first
        ``GET /v1/sessions/{id}/stream`` request.
    :returns: The client and a list that records each request's served status,
        so a caller can assert the relay retried past the error.
    """
    served: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if not served:
            served.append(status_code)
            return httpx.Response(
                status_code,
                json={"error": "runner_unavailable", "detail": "temporary"},
            )
        served.append(200)
        return httpx.Response(
            200,
            content=_HEARTBEAT_SSE,
            headers={"content-type": "text/event-stream"},
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://runner",
    )
    return client, served


@pytest.mark.parametrize("status_code", _ERROR_STATUSES)
async def test_runner_relay_recovers_from_http_error_response(
    status_code: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient HTTP-error stream response must enter the recovery loop.

    Drives the real ``_ensure_runner_relay_ready`` entry the message-send path
    uses, backed by a real ``httpx.AsyncClient`` whose mock transport serves
    the error first and a ``session.heartbeat`` on the retry.

    Pre-fix: only the error request is made, readiness stays unset, and the
    call raises ``RUNNER_UNAVAILABLE``. Post-fix: the error is treated as a
    transport loss, the relay reconnects, and the heartbeat establishes
    readiness.
    """
    from omnigent.server.routes import sessions as sessions_module
    from omnigent.server.routes._sessions import orchestration

    monkeypatch.setattr(orchestration, "_RUNNER_RELAY_READY_TIMEOUT_S", _READY_TIMEOUT_S)

    session_id = f"relayhttp{status_code}000000000000000000000"
    client, served = _error_then_heartbeat_client(status_code)
    sessions_module._runner_relay_tasks.clear()
    try:
        handle: Any = await sessions_module._ensure_runner_relay_ready(
            session_id,
            "runner_http_error",
            client,
            conversation_store=None,
        )

        assert handle is not None
        assert handle.ready.is_set(), (
            f"readiness never set: the HTTP {status_code} response was accepted "
            "as a clean stream and the recovery loop was skipped"
        )
        assert served == [status_code, 200], (
            f"expected the relay to retry past HTTP {status_code} and reconnect, "
            f"but the requests it served were {served}"
        )
    finally:
        handle = sessions_module._runner_relay_tasks.get(session_id)
        if handle is not None:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(handle.task, timeout=_TASK_TIMEOUT_S)
        sessions_module._runner_relay_tasks.clear()
        await client.aclose()
