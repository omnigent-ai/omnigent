"""E2E: opening the Files view on an asleep sandbox wakes it without a message.

A session whose managed sandbox idle-stopped reads ``host_asleep``, and the
Files panel has no workspace to list. Opening the panel must ask the server to
resume the sandbox with a wake-only ``retry_session`` event, show that it is starting,
and list the files once the sandbox reads online, without the user sending a
chat message. ``force_host_asleep`` patches the browser's view into
``host_asleep`` and workspace reads answer the runner-offline 503 until a wake
request succeeds; the seeded runner stays really connected, so the file list
and ``retry_session`` then come from the real server.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlparse

import httpx
import pytest
from playwright.sync_api import Page, Request, Response, Route, expect

from tests.e2e_ui.conftest import _recording_requested, open_right_rail
from tests.e2e_ui.sessions._host_asleep import (
    force_host_asleep,
    hold_wake_requests,
    next_held_wake,
)

_FILE_PATH = "sandbox_notes.txt"
_RETRY_SESSION = {"type": "retry_session", "data": {"wake_only": True}}
# Long enough for a viewer to read each state in a recorded demo.
_HOLD_MS = 2_500


def _runner_unavailable(message: str) -> str:
    """Return the server's 503 ``runner_unavailable`` error body."""
    return json.dumps({"error": {"code": "runner_unavailable", "message": message}})


def _hold_for_viewer(request: pytest.FixtureRequest, page: Page) -> None:
    """Pause on the current state only when the journey is being filmed."""
    if _recording_requested(request.node):
        page.wait_for_timeout(_HOLD_MS)


@pytest.fixture
def asleep_session_file(seeded_session: tuple[str, str]) -> Iterator[tuple[str, str]]:
    """Seed ``_FILE_PATH`` in the session's workspace."""
    base_url, session_id = seeded_session
    file_url = (
        f"{base_url}/v1/sessions/{session_id}/resources/environments/default/filesystem/"
        f"{_FILE_PATH}"
    )
    response = httpx.put(
        file_url,
        json={"content": "Notes kept in the sandbox.", "encoding": "utf-8"},
        timeout=10.0,
    )
    response.raise_for_status()
    try:
        yield (base_url, session_id)
    finally:
        # Deleting the session keeps its workspace files, so remove the seed here.
        httpx.delete(file_url, timeout=10.0)


class _SandboxWakeRecorder:
    """Track session event POSTs and flip the patched sandbox online on wake."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.host_online = {"online": False}
        self.events: list[Any] = []

    @property
    def asleep(self) -> bool:
        return not self.host_online["online"]

    def _is_event_post(self, request: Request) -> bool:
        return (
            request.method == "POST"
            and urlparse(request.url).path == f"/v1/sessions/{self.session_id}/events"
        )

    def on_request(self, request: Request) -> None:
        if self._is_event_post(request):
            self.events.append(request.post_data_json)

    def on_response(self, response: Response) -> None:
        request = response.request
        if response.ok and self._is_event_post(request):
            if request.post_data_json == _RETRY_SESSION:
                self.host_online["online"] = True


def _fail_workspace_reads_while_asleep(page: Page, recorder: _SandboxWakeRecorder) -> None:
    """Answer workspace reads like a suspended sandbox until it is woken."""

    def _handle(route: Route) -> None:
        if route.request.method != "GET" or not recorder.asleep:
            route.continue_()
            return
        route.fulfill(
            status=503,
            content_type="application/json",
            body=_runner_unavailable("Session runner is offline."),
        )

    page.route(re.compile(rf"/v1/sessions/{re.escape(recorder.session_id)}/resources/"), _handle)


def _open_asleep_session(
    request: pytest.FixtureRequest, base_url: str, session_id: str
) -> tuple[Page, _SandboxWakeRecorder, list[Route]]:
    """Create the recorded page after setup, patch the session asleep and hold its wake."""
    recorder = _SandboxWakeRecorder(session_id)
    page: Page = request.getfixturevalue("page")
    force_host_asleep(page, session_id, host_online=recorder.host_online)
    _fail_workspace_reads_while_asleep(page, recorder)
    # The rail defaults open, so the panel can request the wake as the page loads.
    held = hold_wake_requests(page, session_id)
    page.on("request", recorder.on_request)
    page.on("response", recorder.on_response)
    page.goto(f"{base_url}/c/{session_id}")
    return page, recorder, held


def test_opening_files_wakes_asleep_sandbox(
    request: pytest.FixtureRequest,
    asleep_session_file: tuple[str, str],
) -> None:
    """Opening the Workspace rail on Files starts the sandbox and lists its files."""
    base_url, session_id = asleep_session_file
    page, recorder, held = _open_asleep_session(request, base_url, session_id)
    expect(page.get_by_label("Message the agent")).to_be_visible(timeout=30_000)
    _hold_for_viewer(request, page)

    open_right_rail(page)
    wake = next_held_wake(page, held, timeout_ms=30_000)
    rail = page.get_by_role("complementary", name="Workspace")
    # The wake is held, so the sandbox can't read online before this check.
    expect(rail.get_by_text("Starting sandbox…")).to_be_visible()
    wake.continue_()

    expect(rail.get_by_role("button", name=re.compile(re.escape(_FILE_PATH))).first).to_be_visible(
        timeout=30_000
    )
    expect(rail.get_by_text("Starting sandbox…")).to_have_count(0)
    # The wake is the only session event: no chat message was sent.
    assert recorder.events == [_RETRY_SESSION]
    _hold_for_viewer(request, page)
