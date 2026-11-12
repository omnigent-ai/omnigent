"""Terminal tunnel attempts have a final row even when no retry follows."""

import asyncio
import logging

import pytest

from omnigent.runner.transports.ws_tunnel import serve
from tests.runner.transports.ws_tunnel.test_serve import _Closed, _noop_app


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["fatal", "cancelled", "graceful_shutdown"])
async def test_terminal_outcome_has_one_correlated_row(
    mode: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    shutdown = asyncio.Event()

    async def once(*args: object, **kwargs: object):
        if mode == "fatal":
            raise _Closed(4002)
        if mode == "cancelled":
            raise asyncio.CancelledError()
        kwargs["on_connected"]()
        shutdown.set()
        return serve._CloseDetails(1000, "shutdown", 1000, 1000)

    monkeypatch.setattr(serve, "_serve_tunnel_once", once)
    with caplog.at_level(logging.INFO, logger=serve.__name__):
        if mode != "graceful_shutdown":
            with pytest.raises(RuntimeError if mode == "fatal" else asyncio.CancelledError):
                await serve.serve_tunnel(
                    _noop_app,
                    server_url="http://synthetic",
                    runner_version="0.18.0",
                    runner_id="runner-test",
                    shutdown_event=shutdown,
                )
        else:
            await serve.serve_tunnel(
                _noop_app,
                server_url="http://synthetic",
                runner_version="0.18.0",
                runner_id="runner-test",
                shutdown_event=shutdown,
            )
    rows = [
        r
        for r in caplog.records
        if getattr(r, "event_name", None) == "runner_tunnel_attempt_finished"
    ]
    assert len(rows) == 1
    attrs = rows[0].attributes
    assert attrs["runner_id"] == "runner-test"
    assert attrs["connection_id"]
    assert attrs["outcome"] == mode
    assert attrs["connected"] is (mode == "graceful_shutdown")
    assert not any(
        getattr(r, "event_name", None) == "runner_tunnel_disconnected" for r in caplog.records
    )
