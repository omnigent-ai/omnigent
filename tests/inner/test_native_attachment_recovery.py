"""Transient attachment reads recover without losing the referenced bytes."""

import asyncio
import base64
import json
import logging
from collections import Counter

import httpx
import pytest

from omnigent.debug_logging import record_to_row
from omnigent.inner import native_attachments


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["metadata", "content"])
@pytest.mark.parametrize("failure", ["timeout", 502, 503, 504])
async def test_transient_attachment_read_recovers(
    stage: str, failure: str | int, caplog: pytest.LogCaptureFixture
) -> None:
    calls: Counter[str] = Counter()

    def handle(request: httpx.Request) -> httpx.Response:
        current = "content" if request.url.path.endswith("/content") else "metadata"
        calls[current] += 1
        if current == stage and calls[current] == 1:
            if failure == "timeout":
                raise httpx.ReadTimeout("synthetic timeout", request=request)
            return httpx.Response(int(failure))
        if current == "metadata":
            return httpx.Response(200, json={"content_type": "text/plain"})
        return httpx.Response(200, content=b"synthetic attachment")

    block = {"type": "input_file", "file_id": "file-example", "filename": "example.txt"}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="https://example.test"
    ) as client:
        with caplog.at_level(logging.INFO, logger=native_attachments.__name__):
            result = await native_attachments.resolve_file_id_block(
                block, session_id="session-example", client=client
            )

    assert result is not None
    rebuilt, notice = result
    assert base64.b64decode(str(rebuilt["file_data"]).split(",", 1)[1]) == b"synthetic attachment"
    assert rebuilt["filename"] == "example.txt"
    assert "file_id" not in rebuilt
    assert block["file_id"] == "file-example"
    assert notice is None
    assert calls[stage] == 2
    assert calls["metadata" if stage == "content" else "content"] == 1
    assert any(
        getattr(record, "event_name", None) == "native_attachment_read_recovered"
        for record in caplog.records
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403, 404])
async def test_permanent_metadata_failure_stops_without_fetching_content(status: int) -> None:
    paths: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(status)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="https://example.test"
    ) as client:
        result = await native_attachments.resolve_file_id_block(
            {"type": "input_image", "file_id": "missing-file"},
            session_id="session-example",
            client=client,
        )

    assert result is None
    assert paths == ["/v1/sessions/session-example/resources/files/missing-file"]


@pytest.mark.asyncio
async def test_persistent_attachment_failure_has_bounded_attempts() -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadError("synthetic disconnection", request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="https://example.test"
    ) as client:
        result = await native_attachments.resolve_file_id_block(
            {"type": "input_file", "file_id": "file-example"},
            session_id="session-example",
            client=client,
        )
    assert result is None
    assert calls == 3


@pytest.mark.asyncio
async def test_attachment_cancellation_is_not_retried() -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise asyncio.CancelledError

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="https://example.test"
    ) as client:
        with pytest.raises(asyncio.CancelledError):
            await native_attachments.resolve_file_id_block(
                {"type": "input_file", "file_id": "file-example"},
                session_id="session-example",
                client=client,
            )
    assert calls == 1


@pytest.mark.asyncio
async def test_attachment_resolution_deadline_releases_a_stalled_request(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    released = asyncio.Event()

    async def handle(request: httpx.Request) -> httpx.Response:
        try:
            await asyncio.Event().wait()
        finally:
            released.set()
        raise AssertionError("request unexpectedly completed")

    monkeypatch.setattr(native_attachments, "_ATTACHMENT_RESOLVE_TIMEOUT_S", 0.01)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="https://example.test"
    ) as client:
        with caplog.at_level(logging.WARNING, logger=native_attachments.__name__):
            result = await native_attachments.resolve_file_id_block(
                {"type": "input_file", "file_id": "file-example"},
                session_id="session-example",
                client=client,
            )
    assert result is None
    assert released.is_set()
    row = record_to_row(caplog.records[-1], source="runner")
    assert row["event_name"] == "native_attachment_read_failed"
    assert row["attributes"]["deadline_exceeded"] == "True"


@pytest.mark.asyncio
async def test_attachment_failure_diagnostics_omit_file_and_response_contents(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(404, text="private-response")
        ),
        base_url="https://private.example.test",
    ) as client:
        with caplog.at_level(logging.WARNING, logger=native_attachments.__name__):
            result = await native_attachments.resolve_file_id_block(
                {"type": "input_file", "file_id": "private-file", "filename": "private-name"},
                session_id="session-example",
                client=client,
            )
    assert result is None
    row = record_to_row(caplog.records[-1], source="runner")
    assert row["session_id"] == "session-example"
    assert row["attributes"]["http_status"] == "404"
    assert row["attributes"]["stage"] == "metadata"
    assert "private-" not in json.dumps(row)
    assert "private.example.test" not in json.dumps(row)
