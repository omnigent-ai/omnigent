"""E2E: HTML-artifact preview opens links in a new tab.

Regression for #777: a plain ``<a href>`` inside a rendered HTML artifact did
nothing on click, because the preview iframe used ``sandbox=""`` (no popups,
no top-navigation) and the links carried no ``target``. The fix injects
``<base target="_blank">`` into the preview document and widens the iframe
sandbox to ``allow-popups allow-popups-to-escape-sandbox`` so links open in a
new tab. Seeded via the filesystem PUT endpoint (no agent run).
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Page, expect

# ---------------------------------------------------------------------------
# Test constants
# ---------------------------------------------------------------------------

_HTML_FILE_PATH = "report.html"

# A minimal HTML artifact with a single external link that names no target.
_HTML_CONTENT = """\
<!doctype html>
<html>
  <head><title>Report</title></head>
  <body>
    <h1>Quarterly Report</h1>
    <a href="https://example.databricks.com/docs">project homepage</a>
  </body>
</html>
"""


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded_html_session(
    seeded_session: tuple[str, str],
) -> Iterator[tuple[str, str, str]]:
    """Seed the HTML file and yield (base_url, session_id, path).

    :param seeded_session: Runner-bound (base_url, session_id) pair.
    :returns: ``(base_url, session_id, file_path)`` for the test body.
    """
    base_url, session_id = seeded_session
    file_url = (
        f"{base_url}/v1/sessions/{session_id}"
        f"/resources/environments/default/filesystem/{_HTML_FILE_PATH}"
    )
    resp = httpx.put(
        file_url,
        json={"content": _HTML_CONTENT, "encoding": "utf-8"},
        timeout=10.0,
    )
    resp.raise_for_status()
    yield (base_url, session_id, _HTML_FILE_PATH)


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


def test_html_preview_links_open_in_new_tab(
    page: Page,
    seeded_html_session: tuple[str, str, str],
) -> None:
    """The HTML preview iframe defaults links to a new tab and allows popups."""
    base_url, session_id, _file_path = seeded_html_session
    page.goto(f"{base_url}/c/{session_id}?view=explore")

    file_button = page.get_by_role("button", name=re.compile(rf"^{re.escape(_HTML_FILE_PATH)}\b"))
    expect(file_button).to_be_visible(timeout=30_000)
    file_button.click()

    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer).to_be_visible()

    # HTML files default to the preview surface (no rich-text editor mode), so
    # the iframe is present without toggling.
    preview = file_viewer.locator('iframe[title="HTML preview"]')
    expect(preview).to_be_visible(timeout=10_000)

    # The sandbox must permit popups (so target=_blank actually opens) while
    # still withholding scripts and same-origin access.
    sandbox = preview.get_attribute("sandbox")
    assert sandbox is not None
    assert "allow-popups" in sandbox
    assert "allow-scripts" not in sandbox
    assert "allow-same-origin" not in sandbox

    # The injected <base target="_blank"> makes the un-targeted link default to
    # a new tab; without it the click would no-op inside the sandboxed frame.
    srcdoc = preview.get_attribute("srcdoc")
    assert srcdoc is not None
    assert '<base target="_blank">' in srcdoc

    # And the link resolves to a new-tab navigation at the DOM level inside the
    # frame (the <base> default applies; the anchor itself names no target).
    frame = preview.content_frame
    assert frame is not None
    link = frame.locator('a:has-text("project homepage")')
    expect(link).to_have_attribute("href", "https://example.databricks.com/docs")
