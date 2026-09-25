"""HTTP failures must enter relay recovery rather than look like clean streams."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import Mock

import httpx
import pytest

from omnigent.server.routes._sessions import orchestration
from omnigent.stores.conversation_store import ConversationStore


@pytest.mark.asyncio
async def test_relay_recovers_after_http_503(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    attempts = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, json={"detail": "private upstream response"})
        return httpx.Response(
            200,
            text='data: {"type":"session.heartbeat"}\n\ndata: [DONE]\n\n',
        )

    monkeypatch.setattr(orchestration, "_RELAY_RETRY_INTERVAL_S", 0.0)
    ready = asyncio.Event()
    async with httpx.AsyncClient(
        base_url="http://runner", transport=httpx.MockTransport(handle)
    ) as client:
        with caplog.at_level(logging.INFO, logger="omnigent.server.routes.sessions"):
            await asyncio.wait_for(
                orchestration._relay_runner_stream(
                    "conv_http_retry", client, Mock(spec=ConversationStore), ready
                ),
                timeout=10,
            )
    assert attempts == 2
    assert ready.is_set()
    connected = [
        r for r in caplog.records if getattr(r, "event_name", None) == "runner_stream_connected"
    ]
    assert len(connected) == 1
    rejected = [
        r
        for r in caplog.records
        if getattr(r, "event_name", None) == "runner_stream_http_rejected"
    ]
    assert len(rejected) == 1
    assert rejected[0].attributes["http_status"] == 503
    assert "private upstream response" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 403, 404, 429, 500, 503])
async def test_rejected_http_stream_does_not_report_connected(
    status_code: int, caplog: pytest.LogCaptureFixture
) -> None:
    ready = asyncio.Event()
    async with httpx.AsyncClient(
        base_url="http://runner",
        transport=httpx.MockTransport(lambda _: httpx.Response(status_code, text="upstream")),
    ) as client:
        with caplog.at_level(logging.INFO, logger="omnigent.server.routes.sessions"):
            with pytest.raises(orchestration._RelayTransportLost) as caught:
                await orchestration._relay_runner_stream_once(
                    "conv_http_error", client, Mock(spec=ConversationStore), ready
                )
    assert isinstance(caught.value.__cause__, httpx.HTTPStatusError)
    assert caught.value.__cause__.response.status_code == status_code
    assert not ready.is_set()
    assert not any(
        getattr(r, "event_name", None) in {"runner_stream_connected", "runner_stream_ready"}
        for r in caplog.records
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("heartbeat_count", [0, 2])
async def test_ready_diagnostic_requires_a_heartbeat_and_emits_once(
    heartbeat_count: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async with httpx.AsyncClient(
        base_url="http://runner",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                text=(
                    'data: {"type":"session.heartbeat"}\n\n' * heartbeat_count + "data: [DONE]\n\n"
                ),
            )
        ),
    ) as client:
        with caplog.at_level(logging.INFO, logger="omnigent.server.routes.sessions"):
            await orchestration._relay_runner_stream_once(
                "conv_http_ready", client, Mock(spec=ConversationStore)
            )
    records = [
        r for r in caplog.records if getattr(r, "event_name", None) == "runner_stream_ready"
    ]
    assert len(records) == (1 if heartbeat_count else 0)
    if records:
        assert records[0].session_id == "conv_http_ready"
