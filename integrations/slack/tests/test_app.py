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

    with caplog.at_level(logging.ERROR, logger="test-app-error"):
        try:
            raise RuntimeError("boom")
        except RuntimeError as exc:
            await error_handler.func(error=exc, body={"type": "event_callback"})

    assert any("Unhandled Slack listener error" in r.message for r in caplog.records)
    # The exception traceback is attached (logger.exception), not just the message.
    assert any(r.exc_info for r in caplog.records)
