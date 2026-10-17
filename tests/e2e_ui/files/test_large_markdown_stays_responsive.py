"""E2E: opening a large Markdown file must not freeze the browser tab.

The server ships every byte under its read cap to the browser, and the viewer
parses the whole document into the rich-text editor on the main thread. A
~1 MiB file leaves the tab unresponsive for more than a minute, so this test
asserts responsiveness while the file opens and that the editor still renders
within the window: any fix that keeps the page interactive and opens the
document in Edit mode (lazy render, worker parse) passes without the test
prescribing the UI.
"""

from __future__ import annotations

import contextlib
import re
import time
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Page, expect
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from tests.e2e_ui.conftest import open_right_rail
from tests.e2e_ui.files._large_markdown import build_large_markdown

_FILE_PATH = "large_design.md"
_FILE_BYTES = 1024 * 1024
# A healthy open answers in well under a second; the frozen tab takes minutes.
_PROBE_TIMEOUT_MS = 10_000
# Short, so a stall is measured by the heartbeat probe rather than hidden in a
# long wait for the rendered heading.
_RENDER_POLL_MS = 1_000
_WATCH_SECONDS = 60
_RENDERED_HEADING = "[data-testid='file-viewer'] [contenteditable='true'] h1"


@pytest.fixture
def seeded_large_markdown_session(
    seeded_session: tuple[str, str],
) -> Iterator[tuple[str, str, str]]:
    """Seed a ~1 MiB GFM file and yield (base_url, session_id, path)."""
    base_url, session_id = seeded_session
    file_url = (
        f"{base_url}/v1/sessions/{session_id}"
        f"/resources/environments/default/filesystem/{_FILE_PATH}"
    )
    resp = httpx.put(
        file_url,
        json={"content": build_large_markdown(_FILE_BYTES), "encoding": "utf-8"},
        timeout=60.0,
    )
    resp.raise_for_status()
    try:
        yield (base_url, session_id, _FILE_PATH)
    finally:
        with contextlib.suppress(httpx.HTTPError):
            httpx.delete(file_url, timeout=10.0).raise_for_status()


@pytest.mark.browser_context_args(viewport={"width": 1600, "height": 900})
def test_opening_large_markdown_keeps_page_responsive(
    page: Page,
    seeded_large_markdown_session: tuple[str, str, str],
) -> None:
    """The page keeps answering scripts while a 1 MiB markdown file opens."""
    base_url, session_id, file_path = seeded_large_markdown_session
    page.goto(f"{base_url}/c/{session_id}?view=explore")
    open_right_rail(page)

    file_button = page.get_by_role("button", name=re.compile(rf"^{re.escape(file_path)}\b"))
    expect(file_button).to_be_visible(timeout=30_000)
    # Anchor the watch on the content response so the probes cover the render.
    with page.expect_response(
        lambda response: (
            response.ok
            and response.request.method == "GET"
            and response.url.split("?")[0].endswith(f"/filesystem/{file_path}")
        )
    ):
        file_button.click()

    # Every renderer round-trip below carries a timeout, so a frozen main
    # thread surfaces as a failed probe instead of a hung test.
    deadline = time.monotonic() + _WATCH_SECONDS
    rendered = False
    while not rendered and (remaining := deadline - time.monotonic()) > 0:
        try:
            page.wait_for_function(
                f"() => !!document.querySelector({_RENDERED_HEADING!r})",
                timeout=min(_RENDER_POLL_MS, remaining * 1000),
            )
            rendered = True
        except PlaywrightTimeoutError:
            try:
                page.wait_for_function("() => true", timeout=_PROBE_TIMEOUT_MS)
            except PlaywrightTimeoutError:
                pytest.fail(
                    f"page stopped responding to a {_PROBE_TIMEOUT_MS} ms probe "
                    f"while opening {file_path}"
                )

    assert rendered, f"{file_path} did not render within {_WATCH_SECONDS}s"
    expect(page.get_by_role("button", name=f"Close {file_path}", exact=True).first).to_be_visible()
