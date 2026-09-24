"""Full-stack file-line citation through a real workspace resource."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import httpx
from playwright.sync_api import Page, expect

_REPO_ROOT = Path(__file__).resolve().parents[2]
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


def test_chat_citation_opens_real_file_resource_at_line(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """The backend serves the cited file and Chromium reveals its requested line."""
    base_url, session_id = seeded_session
    resource_url = (
        f"{base_url}/v1/sessions/{session_id}/resources/environments/default/"
        f"filesystem/{_FILE_PATH}"
    )
    try:
        written = httpx.put(
            resource_url, json={"content": _SOURCE, "encoding": "utf-8"}, timeout=10
        )
        written.raise_for_status()
        served = httpx.get(resource_url, timeout=10)
        served.raise_for_status()
        assert served.json()["content"] == _SOURCE

        message = httpx.post(
            f"{base_url}/v1/sessions/{session_id}/events",
            json={
                "type": "external_assistant_message",
                "data": {"agent": "hello_world", "text": f"[Line 350]({_FILE_PATH}:350)"},
            },
            timeout=10,
        )
        message.raise_for_status()

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
    finally:
        shutil.rmtree(_REPO_ROOT / session_id, ignore_errors=True)
