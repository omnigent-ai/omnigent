"""E2E: opening the Files view on an asleep sandbox wakes it without a message.

A session whose managed sandbox idle-stopped reads ``host_asleep``, and the
Files panel has no workspace to list. Opening the panel must ask the server to
resume the sandbox with a ``retry_session`` event, show that it is starting,
and list the files once the sandbox reads online, without the user sending a
chat message. ``force_host_asleep`` patches the browser's view into
``host_asleep`` and workspace reads answer the runner-offline 503 until a wake
request succeeds; the seeded runner stays really connected, so the file list
and ``retry_session`` then come from the real server.
"""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import pytest
from playwright.sync_api import Page, Request, Response, Route, expect

from tests.e2e_ui.conftest import open_right_rail
from tests.e2e_ui.sessions._host_asleep import force_host_asleep

# Filesystem PUTs land in ``<repo-root>/<session_id>/`` (os_env.cwd: .).
_REPO_ROOT = Path(__file__).resolve().parents[2]
_FILE_PATH = "sandbox_notes.txt"
_RETRY_SESSION = {"type": "retry_session", "data": {}}
_RESUME_ERROR = "No runner is available to recover this session."
# Long enough for a viewer to read each state in the recorded demo.
_HOLD_MS = 2_500


def _runner_unavailable(message: str) -> str:
    """Return the server's 503 ``runner_unavailable`` error body."""
    return json.dumps({"error": {"code": "runner_unavailable", "message": message}})


@pytest.fixture
def asleep_session_file(seeded_session: tuple[str, str]) -> Iterator[tuple[str, str]]:
    """Seed ``_FILE_PATH`` in the session workspace and yield ``(base_url, session_id)``."""
    base_url, session_id = seeded_session
    response = httpx.put(
        f"{base_url}/v1/sessions/{session_id}/resources/environments/default/filesystem/{_FILE_PATH}",
        json={"content": "Notes kept in the sandbox.", "encoding": "utf-8"},
        timeout=10.0,
    )
    response.raise_for_status()
    try:
        yield (base_url, session_id)
    finally:
        shutil.rmtree(_REPO_ROOT / session_id, ignore_errors=True)
        (_REPO_ROOT / _FILE_PATH).unlink(missing_ok=True)


class _SandboxWakeRecorder:
    """Track session event POSTs and flip the patched sandbox online on wake."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.asleep = True
        self.events: list[Any] = []

    def on_request(self, request: Request) -> None:
        if request.method != "POST" or (
            urlparse(request.url).path != f"/v1/sessions/{self.session_id}/events"
        ):
            return
        self.events.append(request.post_data_json)

    def on_response(self, response: Response) -> None:
        request = response.request
        if request.method == "POST" and request.post_data_json == _RETRY_SESSION and response.ok:
            self.asleep = False


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
) -> tuple[Page, _SandboxWakeRecorder]:
    """Create the recorded page after setup and patch the session asleep."""
    recorder = _SandboxWakeRecorder(session_id)
    page: Page = request.getfixturevalue("page")
    force_host_asleep(page, session_id, asleep=lambda: recorder.asleep)
    _fail_workspace_reads_while_asleep(page, recorder)
    page.on("request", recorder.on_request)
    page.on("response", recorder.on_response)
    page.goto(f"{base_url}/c/{session_id}")
    return page, recorder


def _wait_for_wake_request(page: Page, session_id: str) -> Any:
    def _is_wake(request: Request) -> bool:
        return (
            request.method == "POST"
            and urlparse(request.url).path == f"/v1/sessions/{session_id}/events"
            and request.post_data_json == _RETRY_SESSION
        )

    return page.expect_request(_is_wake, timeout=30_000)


def test_opening_files_wakes_asleep_sandbox(
    request: pytest.FixtureRequest,
    asleep_session_file: tuple[str, str],
) -> None:
    """Opening the Workspace rail on Files starts the sandbox and lists its files."""
    base_url, session_id = asleep_session_file
    page, recorder = _open_asleep_session(request, base_url, session_id)
    expect(page.get_by_label("Message the agent")).to_be_visible(timeout=30_000)
    page.wait_for_timeout(_HOLD_MS)

    with _wait_for_wake_request(page, session_id):
        open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    expect(rail.get_by_text("Starting sandbox…")).to_be_visible()

    expect(rail.get_by_role("button", name=re.compile(re.escape(_FILE_PATH))).first).to_be_visible(
        timeout=30_000
    )
    expect(rail.get_by_text("Starting sandbox…")).to_have_count(0)
    # The wake is the only session event: no chat message was sent.
    assert recorder.events == [_RETRY_SESSION]
    page.wait_for_timeout(_HOLD_MS)


def test_failed_sandbox_wake_offers_try_again(
    request: pytest.FixtureRequest,
    asleep_session_file: tuple[str, str],
) -> None:
    """A failed wake shows the server's reason, and Try again starts the sandbox."""
    base_url, session_id = asleep_session_file
    failures_left = [1]

    def _fail_first_wake(route: Route) -> None:
        if route.request.post_data_json == _RETRY_SESSION and failures_left[0] > 0:
            failures_left[0] -= 1
            route.fulfill(
                status=503,
                content_type="application/json",
                body=_runner_unavailable(_RESUME_ERROR),
            )
            return
        route.continue_()

    page, recorder = _open_asleep_session(request, base_url, session_id)
    page.route(re.compile(rf"/v1/sessions/{re.escape(session_id)}/events$"), _fail_first_wake)
    expect(page.get_by_label("Message the agent")).to_be_visible(timeout=30_000)
    page.wait_for_timeout(_HOLD_MS)

    with _wait_for_wake_request(page, session_id):
        open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    expect(rail.get_by_text(f"Couldn't start the sandbox: {_RESUME_ERROR}")).to_be_visible(
        timeout=15_000
    )
    try_again = rail.get_by_role("button", name="Try again")
    expect(try_again).to_be_visible()
    # The failure is not retried automatically.
    page.wait_for_timeout(_HOLD_MS)
    assert recorder.events == [_RETRY_SESSION]

    with _wait_for_wake_request(page, session_id):
        try_again.click()
    expect(rail.get_by_role("button", name=re.compile(re.escape(_FILE_PATH))).first).to_be_visible(
        timeout=30_000
    )
    assert recorder.events == [_RETRY_SESSION, _RETRY_SESSION]
    page.wait_for_timeout(_HOLD_MS)
