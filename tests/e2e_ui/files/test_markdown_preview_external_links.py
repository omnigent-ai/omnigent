"""E2E: a web link in the Markdown Preview opens in a new tab.

Clicking an HTTPS link opens a separate page while the app stays on its
session. Seeded via the filesystem PUT endpoint (no agent run); the linked site
is a routed stub, so the test never reaches the network.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import open_previewable_files_in_preview

_MARKDOWN_FILE_PATH = "external_link_preview.md"

_LINK_URL = "https://docs.external.example/guide"

_MARKDOWN_CONTENT = f"""\
# Release notes

See the [external guide]({_LINK_URL}) for details.
"""


@pytest.fixture
def seeded_external_link_markdown_session(
    seeded_session: tuple[str, str],
) -> Iterator[tuple[str, str]]:
    base_url, session_id = seeded_session
    file_url = (
        f"{base_url}/v1/sessions/{session_id}"
        f"/resources/environments/default/filesystem/{_MARKDOWN_FILE_PATH}"
    )
    resp = httpx.put(
        file_url,
        json={"content": _MARKDOWN_CONTENT, "encoding": "utf-8"},
        timeout=10.0,
    )
    resp.raise_for_status()
    try:
        yield (base_url, session_id)
    finally:
        # Deleting the session keeps its workspace files, so remove the seed here.
        httpx.delete(file_url, timeout=10.0)


def _fulfill_external_page(route: Route) -> None:
    route.fulfill(
        status=200,
        content_type="text/html",
        body="<title>External guide</title><h1>External guide</h1>",
    )


def test_markdown_preview_web_link_opens_new_tab(
    page: Page,
    seeded_external_link_markdown_session: tuple[str, str],
) -> None:
    """Clicking a URL in the Preview opens it in a new page; the app stays put."""
    base_url, session_id = seeded_external_link_markdown_session
    page.context.route("https://docs.external.example/**", _fulfill_external_page)
    open_previewable_files_in_preview(page)
    page.goto(f"{base_url}/c/{session_id}?file={_MARKDOWN_FILE_PATH}")

    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    link = file_viewer.locator(".markdown-preview").get_by_role("link", name="external guide")
    expect(link).to_be_visible(timeout=10_000)
    expect(link).to_have_attribute("href", _LINK_URL)
    expect(link).to_have_attribute("target", "_blank")

    with page.context.expect_page(timeout=10_000) as new_page_info:
        link.click()
    new_page = new_page_info.value
    new_page.wait_for_load_state()
    expect(new_page).to_have_url(_LINK_URL)
    expect(new_page.get_by_role("heading", name="External guide")).to_be_visible()

    expect(page).to_have_url(re.compile(rf"/c/{session_id}"))
    expect(link).to_be_visible()
