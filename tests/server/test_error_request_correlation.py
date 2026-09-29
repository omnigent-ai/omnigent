"""An API exception can be joined to its request without logging request data."""

import asyncio
import json
import logging

import httpx
import pytest
from fastapi import FastAPI

from omnigent.debug_logging import record_to_row


@pytest.mark.asyncio
async def test_concurrent_api_errors_keep_request_identity(
    app: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    @app.get("/v1/sessions/{session_id}/_diagnostic_failure")
    async def diagnostic_failure(session_id: str) -> None:
        await asyncio.sleep(0)
        try:
            raise ValueError("synthetic underlying failure")
        except ValueError as exc:
            raise RuntimeError("synthetic API failure") from exc

    # The fixture may mount the SPA at /; keep the fault route before it.
    app.router.routes.insert(0, app.router.routes.pop())

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        with caplog.at_level(logging.ERROR, logger="omnigent.server.app"):
            responses = await asyncio.gather(
                *(
                    client.get(
                        f"/v1/sessions/{session_id}/_diagnostic_failure",
                        params={"secret": "private-query-value"},
                        headers={"X-Private": "private-header-value"},
                    )
                    for session_id in ("child-one", "child-two")
                )
            )

    assert [response.status_code for response in responses] == [500, 500]
    rows = [
        record_to_row(record, source="server")
        for record in caplog.records
        if record.name == "omnigent.server.app"
        and record.getMessage().startswith("Unhandled exception:")
    ]
    assert len(rows) == 2
    by_session = {row["session_id"]: row for row in rows}
    request_ids = set()
    for session_id, response in zip(("child-one", "child-two"), responses, strict=True):
        assert response.status_code == 500
        assert response.json()["error"]["code"] == "internal_error"
        attrs = by_session[session_id]["attributes"]
        assert attrs["request_id"] == response.headers["X-Request-Id"]
        request_ids.add(attrs["request_id"])
        assert attrs["route"] == "/v1/sessions/{session_id}/_diagnostic_failure"
        assert attrs["method"] == "GET"
        assert attrs["exception_type"] == "RuntimeError"
        assert attrs["exception_cause_type"] == "ValueError"
        assert "private-query-value" not in json.dumps(attrs)
        assert "private-header-value" not in json.dumps(attrs)
    assert len(request_ids) == 2
