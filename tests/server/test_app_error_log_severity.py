"""Check log severity for expected 503s and genuine server errors."""

from __future__ import annotations

import logging

import httpx
import pytest
from fastapi import FastAPI

from omnigent.errors import ErrorCode, OmnigentError

_LOGGER_NAME = "omnigent.server.app"


@pytest.fixture
def erroring_client(app: FastAPI) -> httpx.AsyncClient:
    """Route each synthetic error through the app's real handler."""

    @app.get("/test/raise/{code}")
    async def _raise(code: str) -> None:
        raise OmnigentError(f"synthetic {code} for log-severity test", code=code)

    # The SPA catch-all can shadow appended routes.
    app.router.routes.insert(0, app.router.routes.pop())

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://server")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code",
    [ErrorCode.RUNNER_UNAVAILABLE, ErrorCode.RUNNER_CAPABILITY_MISMATCH],
)
async def test_transient_runner_503_logs_below_error_without_traceback(
    erroring_client: httpx.AsyncClient,
    caplog: pytest.LogCaptureFixture,
    code: str,
) -> None:
    """Log transient runner 503s below ERROR without a traceback."""
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        async with erroring_client as client:
            resp = await client.get(f"/test/raise/{code}")

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
    """Keep ERROR severity and tracebacks for genuine server failures."""
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
