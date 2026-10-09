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
)


class _RefusingClient:
    """Stand-in runner client whose POST always fails at the transport layer."""

    async def post(self, *args: Any, **kwargs: Any) -> httpx.Response:
        raise httpx.ConnectError("connection refused")


@pytest.mark.asyncio
async def test_transport_miss_warns_and_falls_back(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A refused forward returns None at WARNING, not ERROR.

    :param monkeypatch: Fixture used to inject the refusing runner client.
    :param caplog: Pytest log capture fixture.
    :returns: None.
    """
    helpers = "omnigent.server.routes._sessions.helpers"
    monkeypatch.setattr(
        f"{helpers}._get_runner_client",
        lambda *a, **k: _async_return(_RefusingClient()),
    )

    logger_name = "omnigent.server.routes.sessions"
    with caplog.at_level(logging.WARNING, logger=logger_name):
        result = await _forward_session_change_to_runner_impl(
            "conv_abc123",
            None,
            {"type": "goal_get"},
        )

    assert result is None, "a transport miss must fall back, not raise"
    records = [r for r in caplog.records if r.name == logger_name]
    assert len(records) == 1, f"expected one record, got {records}"
    assert records[0].levelno == logging.WARNING
    assert "did not reach the runner" in records[0].getMessage()


def _async_return(value: Any) -> Any:
    """Wrap *value* in an awaitable, for patching an async helper."""

    async def _coro() -> Any:
        return value

    return _coro()
