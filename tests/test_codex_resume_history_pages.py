"""Codex resumes keep complete history when large backend pages fail."""

from __future__ import annotations

import logging

import click
import httpx
import pytest

from omnigent.harnesses.codex_native.main import (
    _CodexResumeHistoryUnavailableError,
    _fetch_all_session_items_for_codex_resume,
)


@pytest.mark.parametrize("failure", ["server_error", "connection_drop"])
async def test_resume_retries_same_cursor_with_smaller_pages(
    failure: str, caplog: pytest.LogCaptureFixture
) -> None:
    requests: list[tuple[str | None, int]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        after = request.url.params.get("after")
        limit = int(request.url.params["limit"])
        requests.append((after, limit))
        if after == "first" and limit > 250:
            if failure == "connection_drop":
                raise httpx.ReadError("private transport detail", request=request)
            return httpx.Response(500, json={"error": "private response body"})
        if after is None:
            return httpx.Response(
                200, json={"data": [{"id": "first"}], "has_more": True, "last_id": "first"}
            )
        if after == "first":
            return httpx.Response(
                200, json={"data": [{"id": "second"}], "has_more": True, "last_id": "second"}
            )
        return httpx.Response(200, json={"data": [{"id": "third"}], "has_more": False})

    with caplog.at_level(logging.INFO):
        async with httpx.AsyncClient(
            base_url="http://test", transport=httpx.MockTransport(handler)
        ) as client:
            items = await _fetch_all_session_items_for_codex_resume(client, "session")

    assert items == [{"id": "first"}, {"id": "second"}, {"id": "third"}]
    assert requests == [
        (None, 1000),
        ("first", 1000),
        ("first", 500),
        ("first", 250),
        ("second", 250),
    ]
    retries = [
        r
        for r in caplog.records
        if getattr(r, "event_name", "") == "codex_resume_history_page_retry"
    ]
    assert len(retries) == 2
    assert [r.attributes["next_page_limit"] for r in retries] == [500, 250]
    assert all(r.session_id == "session" for r in retries)
    assert all("private" not in str(r.attributes) for r in retries)


async def test_persistent_history_failure_stops_at_page_floor() -> None:
    limits: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        limits.append(int(request.url.params["limit"]))
        return httpx.Response(503, json={"error": "unavailable"})

    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(_CodexResumeHistoryUnavailableError):
            await _fetch_all_session_items_for_codex_resume(client, "session")
    assert limits == [1000, 500, 250, 125, 100]


@pytest.mark.parametrize("status", [401, 403, 404, 429])
async def test_history_request_rejection_is_not_retried(status: int) -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(status, json={"error": "rejected"})

    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(click.ClickException) as exc:
            await _fetch_all_session_items_for_codex_resume(client, "session")
    assert not isinstance(exc.value, _CodexResumeHistoryUnavailableError)
    assert requests == 1
