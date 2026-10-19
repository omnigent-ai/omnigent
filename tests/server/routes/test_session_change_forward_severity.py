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
    _surface_model_change_forward_failure,
)
from tests.debug_log_helpers import capture_debug_rows


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


@pytest.mark.asyncio
@pytest.mark.parametrize("context_kind", ["legacy", "malformed", "structured"])
async def test_rejected_control_relays_runner_failure_context(
    monkeypatch: pytest.MonkeyPatch, context_kind: str
) -> None:
    from omnigent.server.routes._sessions import helpers

    context: object = {
        "failure_id": "err_runner_request",
        "failure_source": "runner_request",
        "runner_request_exception_type": "RuntimeError",
        "runner_request_cause_type": "FileNotFoundError",
        "runner_request_os_errno": 2,
        "runner_request_origin": "omnigent.runner.native_controls.apply:42",
        "origin": "forged",
        "session_id": "foreign-session",
        "exception_message": "private-runner-data",
    }
    if context_kind == "malformed":
        context = "not an object"
    payload: dict[str, object] = {"error": "model_control_failed", "detail": "See runner log"}
    if context_kind != "legacy":
        payload["failure_context"] = context
    published: list[object] = []
    monkeypatch.setattr(
        helpers, "_publish_error_event", lambda _sid, event: published.append(event)
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(500, json=payload)),
        base_url="http://runner",
    ) as runner:
        monkeypatch.setattr(helpers, "_get_runner_client", lambda *args: _async_return(runner))
        with capture_debug_rows("server") as rows:
            result = await _forward_session_change_to_runner_impl(
                "child", None, {"type": "model_change", "model": "requested-model"}
            )
            assert result is not None and result.status_code == 500
            assert _surface_model_change_forward_failure("child", "requested-model", result)

    assert len(published) == 1
    warnings = [row for row in rows if row["level"] == "WARNING"]
    assert len(warnings) == 2
    assert all(row["session_id"] == "child" for row in warnings)
    for row in warnings:
        attributes = row["attributes"]
        if context_kind == "structured":
            assert attributes["failure_id"] == "err_runner_request"
            assert attributes["runner_request_exception_type"] == "RuntimeError"
            assert attributes["runner_request_os_errno"] == "2"
            assert attributes.get("origin") != "forged"
            assert "private-runner-data" not in str(attributes)
        else:
            assert "failure_id" not in attributes
