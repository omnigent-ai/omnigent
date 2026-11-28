"""Severity contract for the session-change runner forward.

A transport-level miss on ``POST /v1/sessions/<id>/events`` (runner asleep,
tunnel still reconnecting) is recovered: the persisted AP-side value stays
authoritative and the runner re-reads it. It therefore must not out-rank the
non-2xx branch, which a live runner's rejection already reports at WARNING.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest

from omnigent.server.routes._sessions.helpers import (
    _forward_session_change_to_runner_impl,
    _RunnerForwardResult,
)


class _RefusingClient:
    """Stand-in runner client whose POST always fails at the transport layer."""

    def __init__(self, error: httpx.HTTPError) -> None:
        self.error = error

    async def post(self, *args: Any, **kwargs: Any) -> httpx.Response:
        raise self.error


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadError])
@pytest.mark.parametrize("uncertain", [False, True])
async def test_transport_miss_warns_and_falls_back(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error: type[httpx.HTTPError],
    uncertain: bool,
) -> None:
    """Connection failure precedes dispatch; a lost reply retains caller uncertainty."""
    helpers = "omnigent.server.routes._sessions.helpers"
    monkeypatch.setattr(
        f"{helpers}._get_runner_client",
        lambda *a, **k: _async_return(_RefusingClient(error("synthetic transport failure"))),
    )
    transport_error_result = (
        _RunnerForwardResult(status_code=503, body="unconfirmed") if uncertain else None
    )

    logger_name = "omnigent.server.routes.sessions"
    with caplog.at_level(logging.WARNING, logger=logger_name):
        result = await _forward_session_change_to_runner_impl(
            "conv_abc123",
            None,
            {"type": "model_change" if uncertain else "goal_get"},
            transport_error_result=transport_error_result,
        )

    assert result is (transport_error_result if error is httpx.ReadError else None)
    records = [r for r in caplog.records if r.name == logger_name]
    assert len(records) == 1, f"expected one record, got {records}"
    assert records[0].levelno == logging.WARNING
    assert "did not reach the runner" in records[0].getMessage()


def _async_return(value: Any) -> Any:
    """Wrap *value* in an awaitable, for patching an async helper."""

    async def _coro() -> Any:
        return value

    return _coro()
