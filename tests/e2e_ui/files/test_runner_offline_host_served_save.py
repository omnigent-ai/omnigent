"""Auto-save and Retry work when the UI reports runner-offline / host-online.

The real runner stays connected here: this covers the browser's save gate and
Retry interaction under stale liveness. Disconnected-runner recovery is covered
by test_filesystem_save_reconnects_runner_on_live_host in the server route suite.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.files.test_file_autosave import (
    _cleanup_session_workdir,
    _seed_file,
    _wait_for_persisted,
)
from tests.e2e_ui.files.test_offline_runner_host_served import (
    _patch_runner_offline_host_online,
)

_MD_PATH = "reconnect_notes.md"
_MD_CONTENT = "# Reconnect Notes\n\nA paragraph to edit while the runner appears offline.\n"


@pytest.fixture
def seeded_offline_markdown(seeded_session: tuple[str, str]) -> Iterator[tuple[str, str]]:
    """Seed a markdown file in the test session's workspace."""
    base_url, session_id = seeded_session
    _seed_file(base_url, session_id, _MD_PATH, _MD_CONTENT)
    try:
        yield base_url, session_id
    finally:
        _cleanup_session_workdir(session_id)


@pytest.mark.parametrize("fail_first_save", [False, True], ids=["autosave", "retry"])
def test_markdown_save_with_stale_runner_liveness(
    page: Page,
    seeded_offline_markdown: tuple[str, str],
    fail_first_save: bool,
) -> None:
    """An edit persists without a chat message, including after a failed save."""
    base_url, session_id = seeded_offline_markdown
    _patch_runner_offline_host_online(page, session_id)
    file_endpoint = (
        f"/v1/sessions/{session_id}/resources/environments/default/filesystem/{_MD_PATH}"
    )
    failed = False

    def _fail_once(route: Route) -> None:
        nonlocal failed
        if (
            route.request.method == "PUT"
            and urlparse(route.request.url).path == file_endpoint
            and not failed
        ):
            failed = True
            route.fulfill(
                status=503,
                content_type="application/json",
                body='{"error":{"code":"runner_unavailable","message":"wake timed out"}}',
            )
        else:
            route.continue_()

    if fail_first_save:
        page.route(re.compile(re.escape(file_endpoint) + r"(\?|$)"), _fail_once)

    page.goto(f"{base_url}/c/{session_id}?file={_MD_PATH}")
    viewer = page.locator('[data-testid="file-viewer"]:visible')
    editor = viewer.locator("[contenteditable='true']")
    expect(editor).to_be_visible(timeout=30_000)
    expect(editor).to_contain_text("A paragraph to edit")
    sentinel = "saved-without-chat-message"
    editor.click()
    page.keyboard.press("Control+End")
    page.keyboard.type(f" {sentinel}")

    if fail_first_save:
        retry = viewer.get_by_role("button", name="Save failed — click to retry")
        expect(retry).to_be_visible(timeout=15_000)
        retry.click()
        assert failed

    expect(viewer.get_by_role("button", name="All changes saved")).to_be_visible(timeout=15_000)
    assert sentinel in _wait_for_persisted(base_url, session_id, _MD_PATH, sentinel)
