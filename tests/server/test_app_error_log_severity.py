"""Log severity of the app-level ``OmnigentError`` handler.

A runner going offline (host reboot, idle-reap, tunnel drop) is a normal
operational state: the transient 503 codes (``runner_unavailable``,
``runner_capability_mismatch``) must be logged at INFO without a stack
trace, while genuine 500-class errors keep ERROR + ``exc_info`` so real
failures stay diagnosable.
"""

from __future__ import annotations

import logging

import httpx
import pytest
from fastapi import FastAPI

from omnigent.errors import ErrorCode, OmnigentError

_LOGGER_NAME = "omnigent.server.app"


@pytest.fixture
def erroring_client(app: FastAPI) -> httpx.AsyncClient:
    """HTTP client against the real app plus routes that raise each error.

    Registers one route per error code under test so the request travels
    through the app's real ``OmnigentError`` exception handler (the code
    under test), not a re-implementation.

    :param app: The FastAPI app from the shared server fixture.
    :returns: An ASGI-transport client bound to the app.
    """

    @app.get("/test/raise/{code}")
    async def _raise(code: str) -> None:
        raise OmnigentError(f"synthetic {code} for log-severity test", code=code)

    # The app ends with a catch-all SPA fallback route when the built web UI
    # is present; a route appended after it would never match. Move the test
    # route ahead of it so the request reaches the raising handler.
    app.router.routes.insert(0, app.router.routes.pop())

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://server")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code",
    [ErrorCode.RUNNER_UNAVAILABLE, ErrorCode.RUNNER_CAPABILITY_MISMATCH],
)
async def test_transient_runner_503_logs_info_without_traceback(
    erroring_client: httpx.AsyncClient,
    caplog: pytest.LogCaptureFixture,
    code: str,
) -> None:
    """An expected offline-runner 503 logs at INFO with no ``exc_info``.

    :param erroring_client: Client whose app raises the parametrized code.
    :param caplog: Pytest log capture.
    :param code: The transient runner error code under test.
    """
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        async with erroring_client as client:
            resp = await client.get(f"/test/raise/{code}")

    # The response contract is unchanged: still a 503 with the code.
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == code

    records = [r for r in caplog.records if r.name == _LOGGER_NAME]
    assert records, "expected the handler to log the runner-unavailable 503"
    assert all(r.levelno < logging.ERROR for r in records), (
        f"transient runner 503 ({code}) logged at ERROR: "
        f"{[r.getMessage() for r in records if r.levelno >= logging.ERROR]}"
    )
    assert all(r.exc_info is None for r in records), (
        f"transient runner 503 ({code}) logged with a stack trace"
    )
    assert not any("Internal error" in r.getMessage() for r in records)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code",
    [ErrorCode.INTERNAL_ERROR, ErrorCode.HARNESS_PROTOCOL_VIOLATION],
)
async def test_genuine_500_still_logs_error_with_traceback(
    erroring_client: httpx.AsyncClient,
    caplog: pytest.LogCaptureFixture,
    code: str,
) -> None:
    """A genuine 500-class error keeps ERROR severity and the traceback.

    :param erroring_client: Client whose app raises the parametrized code.
    :param caplog: Pytest log capture.
    :param code: The genuine internal error code under test.
    """
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        async with erroring_client as client:
            resp = await client.get(f"/test/raise/{code}")

    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == code

    error_records = [
        r for r in caplog.records if r.name == _LOGGER_NAME and r.levelno >= logging.ERROR
    ]
    assert error_records, f"genuine 500 ({code}) must still log at ERROR"
    assert any(r.exc_info is not None for r in error_records), (
        f"genuine 500 ({code}) must keep its traceback (exc_info)"
    )
