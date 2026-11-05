"""Tests for the runner's background-work reports to the server."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from omnigent.runner import background_work_reports
from omnigent.runner.background_work_reports import BackgroundWorkReporter
from tests._helpers.polling import poll_until_async

_URL = "/v1/sessions/conv_1/events"


def _event(running: bool) -> dict[str, Any]:
    return {"type": "external_background_work", "data": {"running": running}}


class _Server:
    """Records each posted event and its report time; answers from ``responses``, then 202."""

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.responses = list(responses)
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.reported_at: list[int] = []
        self.release = asyncio.Event()
        self.release.set()

    async def post(self, url: str, *, json: dict[str, Any]) -> httpx.Response:
        self.reported_at.append(json["data"]["reported_at"])
        self.posts.append((url, {**json, "data": {"running": json["data"]["running"]}}))
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
async def test_a_lost_answer_leaves_the_server_value_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A request may land even when its answer is lost, so the newest value is sent
    even when it matches the last one the server acknowledged.
    """
    monkeypatch.setattr(background_work_reports, "_RETRY_INITIAL_S", 0.0)
    remote: list[bool] = []
    true_answered = asyncio.Event()

    async def post(url: str, *, json: dict[str, Any]) -> httpx.Response:
        remote.append(json["data"]["running"])
        if len(remote) == 1:
            await true_answered.wait()
        elif len(remote) == 2:
            # The shell is back before the lost answer to False is noticed.
            reporter.report("conv_1", True)
            raise httpx.ReadError("answer lost")
        return _response(202)

    reporter = BackgroundWorkReporter(post)
    reporter.report("conv_1", True)
    await _settle()
    reporter.report("conv_1", False)
    true_answered.set()
    await _settle()

    assert remote == [True, False, True]


@pytest.mark.asyncio
async def test_an_unanswered_report_gives_way_to_the_newest_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A request that is never answered times out, and the newest value is sent.

    Its server side can still finish after the newer report; the report times let
    a server that keeps the newest report end on the newer value.
    """
    monkeypatch.setattr(background_work_reports, "_RETRY_INITIAL_S", 0.0)
    monkeypatch.setattr(background_work_reports, "_REQUEST_TIMEOUT_S", 0.01)
    stored: dict[str, Any] = {"running": None, "reported_at": -1}
    sent: list[bool] = []
    stalled_write_resumes = asyncio.Event()
    stalled_writes: list[asyncio.Task[None]] = []

    def store(running: bool, reported_at: int) -> None:
        if reported_at > stored["reported_at"]:
            stored.update(running=running, reported_at=reported_at)

    async def store_later(running: bool, reported_at: int) -> None:
        await stalled_write_resumes.wait()
        store(running, reported_at)

    async def post(url: str, *, json: dict[str, Any]) -> httpx.Response:
        data = json["data"]
        sent.append(data["running"])
        if len(sent) == 1:
            # The handler stalls, so its write outlives the abandoned request.
            stalled_writes.append(
                asyncio.create_task(store_later(data["running"], data["reported_at"]))
            )
            await asyncio.Event().wait()
        store(data["running"], data["reported_at"])
        return _response(202)

    reporter = BackgroundWorkReporter(post)
    reporter.report("conv_1", True)
    await _settle()
    reporter.report("conv_1", False)
    await poll_until_async(lambda: sent == [True, False], timeout_s=5)
    stalled_write_resumes.set()
    await asyncio.gather(*stalled_writes)

    assert stored["running"] is False


@pytest.mark.asyncio
async def test_each_report_is_later_than_the_last(monkeypatch: pytest.MonkeyPatch) -> None:
    """Report times strictly increase, even when the clock reads the same."""
    monkeypatch.setattr(background_work_reports.time, "time_ns", lambda: 1_000)
    server = _Server()
    reporter = BackgroundWorkReporter(server.post)

    reporter.report("conv_1", True)
    await _settle()
    reporter.report("conv_1", False)
    await _settle()

    assert server.reported_at == [1_000, 1_001]


@pytest.mark.asyncio
async def test_an_answer_from_before_a_reconnect_does_not_stand_in_for_the_resync() -> None:
    """
    The old connection's server may not have stored what it acknowledged, so a
    reconnect's resync of the same value is still sent.
    """
    server = _Server()
    server.release.clear()
    reporter = BackgroundWorkReporter(server.post)

    reporter.report("conv_1", True)
    await _settle()
    reporter.resume()
    reporter.report("conv_1", True)
    server.release.set()
    await _settle()

    assert server.posts == [(_URL, _event(True))] * 2


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
