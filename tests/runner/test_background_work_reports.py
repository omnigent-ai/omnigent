"""Tests for the runner's background-work reports to the server."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from omnigent.runner import background_work_reports
from omnigent.runner.background_work_reports import BackgroundWorkReporter

_URL = "/v1/sessions/conv_1/events"


def _event(running: bool) -> dict[str, Any]:
    return {"type": "external_background_work", "data": {"running": running}}


class _Server:
    """Records each posted event; answers from ``responses``, then 202."""

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.responses = list(responses)
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.release = asyncio.Event()
        self.release.set()

    async def post(self, url: str, *, json: dict[str, Any]) -> httpx.Response:
        self.posts.append((url, json))
        await self.release.wait()
        response = self.responses.pop(0) if self.responses else _response(202)
        if isinstance(response, Exception):
            raise response
        return response


def _response(status: int, text: str = "") -> httpx.Response:
    return httpx.Response(status, text=text, request=httpx.Request("POST", _URL))


async def _settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_a_value_reported_during_a_post_is_sent_after_it() -> None:
    """One request per session at a time, and the newest value is the last one sent."""
    server = _Server()
    server.release.clear()
    reporter = BackgroundWorkReporter(server.post)

    reporter.report("conv_1", True)
    await _settle()
    reporter.report("conv_1", False)
    reporter.report("conv_1", True)
    reporter.report("conv_1", False)
    server.release.set()
    await _settle()

    assert server.posts == [(_URL, _event(True)), (_URL, _event(False))]


@pytest.mark.asyncio
async def test_a_transient_failure_retries_with_the_newest_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 503 or a dropped connection is retried, sending whatever is newest by then."""
    monkeypatch.setattr(background_work_reports, "_RETRY_INITIAL_S", 0.0)
    server = _Server(_response(503), httpx.ConnectError("reset"))
    reporter = BackgroundWorkReporter(server.post)

    reporter.report("conv_1", True)
    await _settle()

    assert server.posts == [(_URL, _event(True))] * 3


@pytest.mark.asyncio
async def test_a_server_without_the_event_is_not_asked_again() -> None:
    """An older server rejects the type once; later reports send nothing."""
    unknown = "Unknown event type: 'external_background_work'. Allowed types: []"
    server = _Server(_response(400, unknown))
    reporter = BackgroundWorkReporter(server.post)

    reporter.report("conv_1", True)
    await _settle()
    reporter.report("conv_1", False)
    reporter.report("conv_2", True)
    await _settle()

    assert server.posts == [(_URL, _event(True))]


@pytest.mark.asyncio
async def test_a_rejection_from_before_a_reconnect_does_not_stop_reporting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A late refusal from the replaced server neither disables reporting nor drops a value."""
    monkeypatch.setattr(background_work_reports, "_RETRY_INITIAL_S", 0.0)
    unknown = "Unknown event type: 'external_background_work'. Allowed types: []"
    held = asyncio.Event()
    posts: list[tuple[str, bool]] = []

    async def post(url: str, *, json: dict[str, Any]) -> httpx.Response:
        posts.append((url.split("/")[3], json["data"]["running"]))
        if len(posts) == 1:
            await held.wait()  # conv_2's request to the old server, answered late
            return _response(400, unknown)
        if len(posts) == 2:
            return _response(400, unknown)  # conv_1's, which disables reporting
        return _response(202)

    reporter = BackgroundWorkReporter(post)
    reporter.report("conv_2", True)
    await _settle()
    reporter.report("conv_1", True)
    await _settle()

    reporter.resume()
    reporter.report("conv_1", True)
    reporter.report("conv_2", True)
    held.set()
    await _settle()
    reporter.report("conv_3", True)
    await _settle()

    assert posts == [
        ("conv_2", True),
        ("conv_1", True),
        ("conv_1", True),
        ("conv_2", True),
        ("conv_3", True),
    ]


@pytest.mark.asyncio
async def test_old_server_disable_is_retried_after_server_reconnect() -> None:
    """A reconnect may reach an upgraded server, so the resynced value is sent again."""
    unknown = "Unknown event type: 'external_background_work'. Allowed types: []"
    server = _Server(_response(400, unknown))
    reporter = BackgroundWorkReporter(server.post)
    reporter.report("conv_1", True)
    await _settle()

    reporter.resume()
    reporter.report("conv_1", True)
    await _settle()

    assert server.posts == [(_URL, _event(True)), (_URL, _event(True))]
