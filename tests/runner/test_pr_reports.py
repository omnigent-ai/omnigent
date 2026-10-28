"""Tests for reporting session PR discoveries to the server until it acknowledges them."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.runner import pr_reports
from omnigent.runner.session_prs import PullRequestRef, SessionPrRegistry

A = "https://github.com/example/one/pull/42"


class _Server:
    """Answers event posts with the given statuses, then 202."""

    def __init__(self, *responses: tuple[int, str]) -> None:
        self.responses = list(responses)
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def post(self, url: str, *, json: dict[str, Any]) -> httpx.Response:
        self.events.append((url, json))
        status, text = self.responses.pop(0) if self.responses else (202, "")
        return httpx.Response(status, text=text, request=httpx.Request("POST", url))


@pytest.fixture
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SessionPrRegistry:
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(pr_reports, "_RETRY_INITIAL_S", 0)
    registry = SessionPrRegistry("session")
    registry.record([PullRequestRef.from_url(A)], relationship="created", source="test")
    return registry


async def _drain(reporter: pr_reports.PullRequestReporter) -> None:
    await asyncio.sleep(0)
    while reporter._tasks:
        await asyncio.gather(*reporter._tasks.values())


async def test_reports_until_the_server_acknowledges(registry: SessionPrRegistry) -> None:
    server = _Server((503, ""))
    reporter = pr_reports.PullRequestReporter(server.post)
    reporter.notify("session")
    await _drain(reporter)

    assert [url for url, _ in server.events] == ["/v1/sessions/session/events"] * 2
    event = server.events[-1][1]
    assert event["type"] == "external_pull_requests"
    [entry] = event["data"]["prs"]
    assert (entry["url"], entry["relationship"], entry["number"]) == (A, "created", 42)
    assert registry.pending_reports()[1] == []
    reporter.notify("session")
    await _drain(reporter)
    assert len(server.events) == 2


async def test_older_servers_stop_reports_and_keep_them_for_a_newer_one(
    registry: SessionPrRegistry,
) -> None:
    server = _Server((400, "Unknown event type: 'external_pull_requests'"))
    reporter = pr_reports.PullRequestReporter(server.post)
    reporter.notify("session")
    await _drain(reporter)
    reporter.notify("session")
    await _drain(reporter)

    assert len(server.events) == 1
    assert [entry.url for entry in registry.pending_reports()[1]] == [A]


def test_notify_is_a_noop_without_a_reporter(registry: SessionPrRegistry) -> None:
    pr_reports.notify("session")
    assert [entry.url for entry in registry.pending_reports()[1]] == [A]
