from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from omnigent_slack import app as app_module
from omnigent_slack.app import _register_error_handler
from slack_bolt.async_app import AsyncApp


class _StartupReached(Exception):
    """Raised in place of connecting to Slack, to stop ``run()`` after wiring."""


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


@pytest.mark.asyncio
async def test_run_passes_the_operator_setup_defaults_to_the_setup_flow(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``run()`` hands the configured defaults to SetupFlow.

    Without this the two env vars parse fine and do nothing. The socket handler
    is replaced so startup stops just after wiring, before any Slack call.
    """
    for key in ("OMNIGENT_SLACK_DEFAULT_AGENT_ID", "OMNIGENT_SLACK_DEFAULT_HOST_TYPE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OMNIGENT_SLACK_BOT_TOKEN", "xoxb-dummy")
    monkeypatch.setenv("OMNIGENT_SLACK_APP_TOKEN", "xapp-dummy")
    monkeypatch.setenv("OMNIGENT_SERVER_URL", "https://omnigent.example.com")
    monkeypatch.setenv("OMNIGENT_SLACK_DATABASE_PATH", str(tmp_path / "bot.sqlite3"))
    monkeypatch.setenv("OMNIGENT_SLACK_DEFAULT_AGENT_ID", "ag_standard")
    monkeypatch.setenv("OMNIGENT_SLACK_DEFAULT_HOST_TYPE", "managed")

    captured: dict[str, Any] = {}
    real_setup_flow = app_module.SetupFlow

    def _record(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return real_setup_flow(**kwargs)

    class _StopHandler:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def start_async(self) -> None:
            raise _StartupReached

    monkeypatch.setattr(app_module, "SetupFlow", _record)
    monkeypatch.setattr(app_module, "AsyncSocketModeHandler", _StopHandler)

    with pytest.raises(_StartupReached):
        await app_module.run()

    assert captured["default_agent_id"] == "ag_standard"
    assert captured["default_host_type"] == "managed"
