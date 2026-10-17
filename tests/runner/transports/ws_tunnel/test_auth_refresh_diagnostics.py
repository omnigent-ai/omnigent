"""Token refresh observations preserve cache, exceptions and credential privacy."""

import asyncio
import json
import logging
from pathlib import Path

import pytest
from fastapi import FastAPI
from websockets.asyncio.server import ServerConnection
from websockets.asyncio.server import serve as websocket_server

from omnigent.runner.transports.ws_tunnel import serve
from omnigent.runner.transports.ws_tunnel.frames import (
    HelloFrame,
    RequestFrame,
    ResponseBodyFrame,
    ResponseEndFrame,
    ResponseHeadFrame,
    decode_frame,
    encode_frame,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["token", "none", "fallback", "raised"])
async def test_refresh_outcomes_preserve_behavior(
    mode: str, caplog: pytest.LogCaptureFixture
) -> None:
    cached = "synthetic-cached-credential"
    fresh = "synthetic-new-credential"

    def factory() -> str | None:
        if mode == "fallback":
            raise OSError("synthetic failure")
        if mode == "raised":
            raise RuntimeError("synthetic failure")
        return fresh if mode == "token" else None

    with caplog.at_level(logging.DEBUG, logger=serve.__name__):
        if mode == "raised":
            with pytest.raises(RuntimeError):
                await serve._refresh_auth_token(cached, factory)
        else:
            assert await serve._refresh_auth_token(cached, factory) == (
                fresh if mode == "token" else cached
            )
    records = [
        r
        for r in caplog.records
        if getattr(r, "event_name", "").startswith("runner_tunnel_token_refresh_")
    ]
    assert len(records) == 2
    first, last = [r.attributes for r in records]
    assert first["token_refresh_id"] == last["token_refresh_id"]
    assert last["credential_scope"] == "runner_tunnel"
    assert last["cached_token_present"] is True
    assert (
        last["outcome"]
        == {
            "token": "token_returned",
            "none": "no_token_fallback",
            "fallback": "error_fallback",
            "raised": "raised",
        }[mode]
    )
    assert last["elapsed_ms"] >= 0
    assert cached not in json.dumps(first) + json.dumps(last)
    assert fresh not in json.dumps(first) + json.dumps(last)


@pytest.mark.asyncio
async def test_broken_diagnostic_sink_does_not_change_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("synthetic sink error")

    monkeypatch.setattr(serve._logger, "debug", fail)
    monkeypatch.setattr(serve._logger, "log", fail)
    assert await serve._refresh_auth_token("cached", lambda: "fresh") == "fresh"


@pytest.mark.asyncio
@pytest.mark.parametrize("outer_error", [RuntimeError, asyncio.CancelledError])
@pytest.mark.parametrize("mode", ["token", "none", "fallback"])
async def test_refresh_does_not_inherit_callers_handled_exception(
    outer_error: type[BaseException], mode: str, caplog: pytest.LogCaptureFixture
) -> None:
    def factory() -> str | None:
        if mode == "fallback":
            raise OSError("local refresh failure")
        return "fresh" if mode == "token" else None

    with caplog.at_level(logging.DEBUG, logger=serve.__name__):
        try:
            raise outer_error("unrelated caller exception")
        except outer_error:
            result = await serve._refresh_auth_token("cached", factory)

    assert result == ("fresh" if mode == "token" else "cached")
    finished = next(
        r.attributes
        for r in caplog.records
        if getattr(r, "event_name", "") == "runner_tunnel_token_refresh_finished"
    )
    assert (
        finished["outcome"]
        == {
            "token": "token_returned",
            "none": "no_token_fallback",
            "fallback": "error_fallback",
        }[mode]
    )
    assert finished.get("error_type") == ("OSError" if mode == "fallback" else None)


@pytest.mark.asyncio
async def test_refresh_records_and_propagates_its_own_cancellation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def factory() -> str | None:
        raise asyncio.CancelledError("local cancellation")

    with caplog.at_level(logging.DEBUG, logger=serve.__name__):
        with pytest.raises(asyncio.CancelledError, match="local cancellation"):
            await serve._refresh_auth_token("cached", factory)
    finished = next(
        r.attributes
        for r in caplog.records
        if getattr(r, "event_name", "") == "runner_tunnel_token_refresh_finished"
    )
    assert finished["outcome"] == "cancelled"
    assert finished["error_type"] == "CancelledError"


@pytest.mark.asyncio
@pytest.mark.parametrize("outer_error", [RuntimeError, asyncio.CancelledError])
async def test_refresh_inside_error_handler_serves_a_real_tunnel_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    outer_error: type[BaseException],
) -> None:
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("OMNIGENT_RUNNER_CONNECT_MARKER", raising=False)
    shutdown = asyncio.Event()
    reply: list[object] = []
    app = FastAPI()

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    async def peer(ws: ServerConnection) -> None:
        assert isinstance(decode_frame(await ws.recv()), HelloFrame)
        await ws.send(encode_frame(RequestFrame(id="proof", method="GET", path="/health")))
        while True:
            frame = decode_frame(await ws.recv())
            reply.append(frame)
            if isinstance(frame, ResponseEndFrame):
                break
        shutdown.set()
        await ws.wait_closed()

    with caplog.at_level(logging.DEBUG, logger=serve.__name__):
        async with websocket_server(peer, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            try:
                raise outer_error("unrelated caller exception")
            except outer_error:
                await asyncio.wait_for(
                    serve.serve_tunnel(
                        app,
                        server_url=f"http://127.0.0.1:{port}",
                        runner_id="runner-refresh-proof",
                        runner_version="test",
                        auth_token_factory=lambda: "synthetic-token",
                        shutdown_event=shutdown,
                    ),
                    timeout=5,
                )

    assert any(isinstance(f, ResponseHeadFrame) and f.status == 200 for f in reply)
    body = "".join(f.body for f in reply if isinstance(f, ResponseBodyFrame))
    assert json.loads(body) == {"status": "ok"}
    finished = next(
        r.attributes
        for r in caplog.records
        if getattr(r, "event_name", "") == "runner_tunnel_token_refresh_finished"
    )
    assert finished["outcome"] == "token_returned"
    assert finished.get("error_type") is None
    attempts = [
        r.attributes
        for r in caplog.records
        if getattr(r, "event_name", "") == "runner_tunnel_attempt_finished"
    ]
    assert len(attempts) == 1
    assert attempts[0]["outcome"] == "graceful_shutdown"
    assert attempts[0].get("terminal_error_type") is None
