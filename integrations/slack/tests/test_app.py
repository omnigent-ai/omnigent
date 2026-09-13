from __future__ import annotations

import logging

import pytest
from omnigent_slack.app import _register_error_handler
from slack_bolt.async_app import AsyncApp


@pytest.mark.asyncio
async def test_error_handler_logs_with_traceback(caplog: pytest.LogCaptureFixture) -> None:
    """The registered global error handler logs the failure with a traceback."""
    app = AsyncApp(token="xoxb-dummy", signing_secret="x")
    logger = logging.getLogger("test-app-error")
    _register_error_handler(app, logger)

    # Grab the handler Bolt stored and invoke it as Bolt would on a listener
    # error. Bolt injects only the args the handler declares by name (ours
    # takes error + body), so call the stored func with those.
    error_handler = app._async_middleware_error_handler
    assert error_handler is not None

    def raise_error() -> RuntimeError:
        try:
            raise RuntimeError("boom")
        except RuntimeError as exc:
            return exc

    error = raise_error()
    with caplog.at_level(logging.ERROR, logger="test-app-error"):
        await error_handler.func(error=error, body={"type": "event_callback"})

    record = caplog.records[-1]
    assert "Unhandled Slack listener error: boom" in record.message
    assert record.exc_info == (RuntimeError, error, error.__traceback__)


@pytest.mark.asyncio
async def test_error_handler_logs_exception_without_active_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An exception passed outside an ``except`` block still logs usefully."""
    app = AsyncApp(token="xoxb-dummy", signing_secret="x")
    logger = logging.getLogger("test-app-error-no-traceback")
    _register_error_handler(app, logger)
    error_handler = app._async_middleware_error_handler
    assert error_handler is not None

    error = ValueError("created outside an except block")
    with caplog.at_level(logging.ERROR, logger="test-app-error-no-traceback"):
        await error_handler.func(error=error, body={"type": "event_callback"})

    record = caplog.records[-1]
    assert "created outside an except block" in record.message
    assert record.exc_info == (ValueError, error, None)
    assert "NoneType: None" not in caplog.text
