"""Terminal tunnel attempts have a final row even when no retry follows."""

import asyncio
import logging
import threading

import pytest
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus
from websockets.http11 import Response

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


@pytest.mark.asyncio
async def test_shutdown_after_retryable_error_does_not_log_or_wait_for_retry(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    shutdown = asyncio.Event()

    async def once(*args: object, **kwargs: object):
        shutdown.set()
        raise OSError("connection dropped during shutdown")

    monkeypatch.setattr(serve, "_serve_tunnel_once", once)
    with caplog.at_level(logging.INFO, logger=serve.__name__):
        await serve.serve_tunnel(
            _noop_app,
            server_url="http://synthetic",
            runner_version="test",
            runner_id="runner-test",
            shutdown_event=shutdown,
        )

    attempts = [
        r.attributes
        for r in caplog.records
        if getattr(r, "event_name", None) == "runner_tunnel_attempt_finished"
    ]
    assert len(attempts) == 1
    assert attempts[0]["outcome"] == "shutdown_after_error"
    assert attempts[0]["source_error_type"] == "OSError"
    assert attempts[0].get("terminal_error_type") is None
    assert not any(
        getattr(r, "event_name", None) == "runner_tunnel_disconnected" for r in caplog.records
    )


@pytest.mark.asyncio
async def test_retry_does_not_inherit_callers_handled_exception(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    shutdown = asyncio.Event()
    calls = 0

    async def once(*args: object, **kwargs: object):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("retryable connection error")
        shutdown.set()
        return serve._CloseDetails(1000, "shutdown", 1000, 1000)

    monkeypatch.setattr(serve, "_serve_tunnel_once", once)
    monkeypatch.setattr(serve, "_INITIAL_RECONNECT_DELAY_S", 0)
    with caplog.at_level(logging.INFO, logger=serve.__name__):
        try:
            raise RuntimeError("unrelated caller exception")
        except RuntimeError:
            await serve.serve_tunnel(
                _noop_app,
                server_url="http://synthetic",
                runner_version="test",
                runner_id="runner-test",
                shutdown_event=shutdown,
            )

    attempts = [
        r.attributes
        for r in caplog.records
        if getattr(r, "event_name", None) == "runner_tunnel_attempt_finished"
    ]
    retries = [
        r for r in caplog.records if getattr(r, "event_name", None) == "runner_tunnel_disconnected"
    ]
    assert calls == 2
    assert len(attempts) == 1
    assert attempts[0]["outcome"] == "graceful_shutdown"
    assert len(retries) == 1


@pytest.mark.asyncio
async def test_cancellation_during_auth_retry_is_a_local_shutdown(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    started = threading.Event()
    release = threading.Event()

    class Provider:
        def __call__(self) -> str:
            return "synthetic-token"

        def invalidate(self) -> bool:
            started.set()
            assert release.wait(5)
            return True

    async def once(*args: object, **kwargs: object) -> None:
        raise InvalidStatus(Response(401, "Unauthorized", Headers(), b""))

    monkeypatch.setattr(serve, "_serve_tunnel_once", once)
    with caplog.at_level(logging.INFO, logger=serve.__name__):
        task = asyncio.create_task(
            serve.serve_tunnel(
                _noop_app,
                server_url="http://synthetic",
                runner_version="test",
                runner_id="runner-test",
                auth_token_factory=Provider(),
            )
        )
        try:
            assert await asyncio.to_thread(started.wait, 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    attempts = [
        r.attributes
        for r in caplog.records
        if getattr(r, "event_name", None) == "runner_tunnel_attempt_finished"
    ]
    assert len(attempts) == 1
    assert attempts[0]["outcome"] == "cancelled"
    assert attempts[0]["terminal_error_type"] == "CancelledError"
    assert attempts[0]["source_error_type"] == "InvalidStatus"
    assert attempts[0]["disconnect_reason"] == "local_shutdown"
