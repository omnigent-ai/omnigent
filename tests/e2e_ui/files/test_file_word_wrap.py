"""E2E: the file viewer's "Wrap lines" toggle soft-wraps long lines in real Monaco.

Component tests mock Monaco; these check the rendered layout and the reload.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

from tests._helpers.polling import poll_until

# Seeded files land in the server's cwd, the repo root (<repo>/tests/e2e_ui/files/...).
_REPO_ROOT = Path(__file__).resolve().parents[3]

_FILE_PATH = "ProxyHandler.scala"
_TAIL_MARKER = "endOfTheLongComment"
_EDIT_MARKER = "typedBeforeWrapping"
_LONG_COMMENT = (
    "  // Forwards each MCP request to the upstream service, retrying idempotent calls "
    "with exponential backoff, and records latency, status, and the caller's workspace "
    "so that slow or failing upstreams can be found from the proxy metrics dashboard "
    f"without reading raw logs {_TAIL_MARKER}"
)
_FILE_CONTENT = f"""\
object ProxyHandler {{
{_LONG_COMMENT}
  def handle(request: Request): Response = forward(request)
}}
"""


@pytest.fixture
def seeded_long_line_file(seeded_session: tuple[str, str]) -> Iterator[tuple[str, str]]:
    """Seed a Scala file with one long comment line; yield ``(base_url, session_id)``."""
    base_url, session_id = seeded_session
    resp = httpx.put(
        f"{base_url}/v1/sessions/{session_id}/resources/environments/default/filesystem/{_FILE_PATH}",
        json={"content": _FILE_CONTENT, "encoding": "utf-8"},
        timeout=10.0,
    )
    resp.raise_for_status()
    try:
        yield (base_url, session_id)
    finally:
        shutil.rmtree(_REPO_ROOT / session_id, ignore_errors=True)


def _open_long_line_file(request: pytest.FixtureRequest, base_url: str, session_id: str) -> Page:
    """Create the browser page after setup and open the seeded file in Monaco."""
    page: Page = request.getfixturevalue("page")
    page.goto(f"{base_url}/c/{session_id}?file={_FILE_PATH}")
    editor = _editor(page)
    expect(editor.locator(".view-line", has_text="def handle")).to_be_visible(timeout=30_000)
    return page


def _editor(page: Page) -> Locator:
    return page.locator('[data-testid="file-viewer"]:visible .monaco-editor')


def _tail_fits_in_editor(page: Page) -> bool:
    """Whether the long line's last word ends inside the editor's visible width."""
    editor = _editor(page)
    tail = editor.locator(".view-line span span", has_text=_TAIL_MARKER).last
    tail.wait_for(state="attached")
    editor_box = editor.bounding_box()
    tail_box = tail.bounding_box()
    assert editor_box is not None and tail_box is not None
    return tail_box["x"] + tail_box["width"] <= editor_box["x"] + editor_box["width"]


def _toggle_wrap_lines(page: Page) -> None:
    page.locator('[data-testid="file-viewer"]:visible').get_by_role(
        "button", name="View settings"
    ).click()
    page.get_by_role("menuitem", name="Wrap lines").click()
    page.keyboard.press("Escape")


def test_wrap_lines_soft_wraps_a_long_code_line(
    request: pytest.FixtureRequest,
    seeded_long_line_file: tuple[str, str],
) -> None:
    """Turning Wrap lines on brings the end of a long line into view; off cuts it again."""
    page = _open_long_line_file(request, *seeded_long_line_file)
    assert not _tail_fits_in_editor(page), "the long line should run past the pane by default"

    editor = _editor(page)
    editor.locator(".view-line", has_text="def handle").click()
    page.keyboard.press("End")
    page.keyboard.type(f" // {_EDIT_MARKER}")
    expect(editor.locator(".selected-text")).to_have_count(0)
    page.keyboard.press("Shift+Home")
    expect(editor.locator(".selected-text").first).to_be_attached()

    _toggle_wrap_lines(page)
    poll_until(lambda: _tail_fits_in_editor(page), timeout_s=10)
    expect(editor.locator(".view-line", has_text=_TAIL_MARKER)).to_be_visible()
    # A remounted editor would drop the selection; the options update keeps it.
    expect(editor.locator(".view-line", has_text=_EDIT_MARKER)).to_be_visible()
    expect(editor.locator(".selected-text").first).to_be_attached()

    _toggle_wrap_lines(page)
    poll_until(lambda: not _tail_fits_in_editor(page), timeout_s=10)


def test_wrap_lines_choice_survives_a_reload(
    request: pytest.FixtureRequest,
    seeded_long_line_file: tuple[str, str],
) -> None:
    """The wrap preference is restored when the page is reloaded."""
    page = _open_long_line_file(request, *seeded_long_line_file)
    _toggle_wrap_lines(page)
    poll_until(lambda: _tail_fits_in_editor(page), timeout_s=10)

    page.reload()
    expect(_editor(page).locator(".view-line", has_text="def handle")).to_be_visible(
        timeout=30_000
    )
    poll_until(lambda: _tail_fits_in_editor(page), timeout_s=10)
    expect(_editor(page).locator(".view-line", has_text=_TAIL_MARKER)).to_be_visible()
