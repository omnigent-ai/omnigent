"""Full-stack file-line citation through a real workspace resource."""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager

import httpx
import pytest
from playwright.sync_api import Page, expect

_FILE_PATH = "src/citation_target.py"
_LINES = [f"# source line {line}" for line in range(1, 501)]
_SOURCE = "\n".join(_LINES)
_CENTERED_LINE = """text => {
  for (const line of document.querySelectorAll(
    '[data-testid="file-viewer"] .view-line'
  )) {
    if (line.textContent.replace(/\\u00a0/g, ' ') !== text) continue;
    const rect = line.getBoundingClientRect();
    const editor = line.closest('.monaco-editor').getBoundingClientRect();
    if (!rect.height || !editor.height) continue;
    if (Math.abs((rect.top + rect.bottom - editor.top - editor.bottom) / 2) < 25) return true;
  }
  return false;
}"""


@contextmanager
def _cited_workspace_file(
    base_url: str, session_id: str, path: str, source: str, message: str
) -> Iterator[str]:
    """Yield the resource URL of ``path`` holding ``source``, cited by an assistant ``message``."""
    resource_url = (
        f"{base_url}/v1/sessions/{session_id}/resources/environments/default/filesystem/{path}"
    )
    httpx.put(
        resource_url, json={"content": source, "encoding": "utf-8"}, timeout=10
    ).raise_for_status()
    try:
        served = httpx.get(resource_url, timeout=10)
        served.raise_for_status()
        assert served.json()["content"] == source
        httpx.post(
            f"{base_url}/v1/sessions/{session_id}/events",
            json={
                "type": "external_assistant_message",
                "data": {"agent": "hello_world", "text": message},
            },
            timeout=10,
        ).raise_for_status()
        yield resource_url
    finally:
        httpx.delete(resource_url, timeout=10).raise_for_status()


def test_chat_citation_opens_real_file_resource_at_line(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """The backend serves the cited file and Chromium reveals its requested line."""
    base_url, session_id = seeded_session
    with _cited_workspace_file(
        base_url, session_id, _FILE_PATH, _SOURCE, f"[Line 350]({_FILE_PATH}:350)"
    ) as resource_url:
        page.set_viewport_size({"width": 1600, "height": 1000})
        page.goto(f"{base_url}/c/{session_id}")
        citation = page.get_by_role("button", name="Line 350", exact=True)
        expect(citation).to_be_visible(timeout=30_000)
        with page.expect_response(
            lambda response: response.url.startswith(resource_url) and response.status == 200
        ):
            citation.click()
        expect(page).to_have_url(
            re.compile(r"[?&]file=src(?:%2F|/)citation_target\.py&line=350(?:&|$)")
        )
        viewer = page.locator('[data-testid="file-viewer"]:visible')
        expect(viewer.locator(".monaco-editor")).to_be_visible(timeout=30_000)
        page.wait_for_function(_CENTERED_LINE, arg=_LINES[349], timeout=30_000)


_RANGE_FILE_PATH = "sql/core/SQLFunctionExpression.scala"
_RANGE_LINES = [f"  // expression line {line}" for line in range(1, 201)]


def test_chat_range_citation_shows_file_name_and_highlights_range(
    request: pytest.FixtureRequest, seeded_session: tuple[str, str]
) -> None:
    """A `path:start-end` citation shows its file name and opens with the range marked."""
    base_url, session_id = seeded_session
    citation_text = f"{_RANGE_FILE_PATH}:68-72"
    with _cited_workspace_file(
        base_url, session_id, _RANGE_FILE_PATH, "\n".join(_RANGE_LINES), f"See `{citation_text}`."
    ):
        # Created after setup so a recorded run (OMNIGENT_E2E_RECORD_DIR) starts at the journey.
        page: Page = request.getfixturevalue("page")
        page.set_viewport_size({"width": 1600, "height": 1000})
        page.goto(f"{base_url}/c/{session_id}")
        citation = page.get_by_role("button", name=citation_text, exact=True)
        expect(citation).to_be_visible(timeout=30_000)
        expect(citation).to_have_text("SQLFunctionExpression.scala:68-72")
        expect(citation).to_have_attribute("title", citation_text)
        citation.click()
        expect(page).to_have_url(re.compile(r"[?&]line=68&endLine=72(?:&|$)"))
        viewer = page.locator('[data-testid="file-viewer"]:visible')
        expect(viewer.locator(".monaco-editor")).to_be_visible(timeout=30_000)
        expect(viewer.locator(".oa-cited-lines")).to_have_count(5, timeout=30_000)
        page.wait_for_function(_CENTERED_LINE, arg=_RANGE_LINES[69], timeout=30_000)
