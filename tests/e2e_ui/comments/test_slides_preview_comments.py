"""E2E: commenting on a rendered ``*.slides.html`` deck (the bridge path).

Slide decks open in ``SlidesViewer`` rather than the plain HTML preview. The
viewer injects the same comment bridge into the preview srcDoc (after design
injection) so selecting rendered text on a slide opens the floating Add comment
button and stores anchors against the workspace file source.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

_REPO_ROOT = Path(__file__).resolve().parents[3]

_SLIDES_PATH = "pitch.slides.html"
_ANCHOR_SENTENCE = "uniqueslideanchortoken review this slide sentence"

_SLIDES_CONTENT = f"""\
<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <title>Pitch</title>
  </head>
  <body>
    <section>
      <h1>Title</h1>
      <p>Intro slide without the anchor.</p>
    </section>
    <section>
      <h1>Details</h1>
      <p id="anchor">{_ANCHOR_SENTENCE}</p>
    </section>
  </body>
</html>
"""


def _cleanup_session_workdir(session_id: str) -> None:
    shutil.rmtree(_REPO_ROOT / session_id, ignore_errors=True)


@pytest.fixture
def seeded_slides(seeded_session: tuple[str, str]) -> Iterator[tuple[str, str, str]]:
    """Seed the slide deck and yield ``(base_url, session_id, path)``."""
    base_url, session_id = seeded_session
    resp = httpx.put(
        f"{base_url}/v1/sessions/{session_id}"
        f"/resources/environments/default/filesystem/{_SLIDES_PATH}",
        json={"content": _SLIDES_CONTENT, "encoding": "utf-8"},
        timeout=10.0,
    )
    resp.raise_for_status()
    try:
        yield (base_url, session_id, _SLIDES_PATH)
    finally:
        _cleanup_session_workdir(session_id)


def test_slides_preview_add_comment(
    page: Page,
    seeded_slides: tuple[str, str, str],
) -> None:
    """Select rendered slide text, add a comment, and verify it persists."""
    base_url, session_id, file_path = seeded_slides
    page.set_viewport_size({"width": 1600, "height": 900})
    page.goto(f"{base_url}/c/{session_id}?file={_SLIDES_PATH}")

    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer).to_be_visible()

    iframe_el = file_viewer.locator('iframe[title="Slide deck"]')
    expect(iframe_el).to_be_visible(timeout=10_000)

    # Move to the slide that holds the anchor (2 / 2).
    file_viewer.get_by_role("button", name="Next slide").click()
    expect(file_viewer.get_by_text("2 / 2")).to_be_visible()

    preview = file_viewer.frame_locator('iframe[title="Slide deck"]')
    expect(preview.locator("#anchor")).to_have_text(_ANCHOR_SENTENCE, timeout=10_000)
    preview.locator("#anchor").select_text()

    add_comment_btn = page.get_by_role("button", name="Add comment")
    expect(add_comment_btn).to_be_visible(timeout=10_000)
    add_comment_btn.click()

    expect(file_viewer.locator("span.font-semibold", has_text="Comments")).to_be_visible()
    comment_body = "This slide needs a citation."
    comment_textarea = file_viewer.locator("textarea[placeholder='Add a comment…']")
    expect(comment_textarea).to_be_visible()
    comment_textarea.fill(comment_body)
    file_viewer.get_by_role("button", name="Add Comment").click()
    expect(file_viewer).to_contain_text(comment_body)

    comments_resp = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/comments?path={file_path}",
        timeout=10.0,
    )
    comments_resp.raise_for_status()
    comments = comments_resp.json()
    assert len(comments) == 1, f"Expected 1 comment, got {len(comments)}: {comments}"

    comment = comments[0]
    assert comment["body"] == comment_body
    assert comment["anchor_content"] == _ANCHOR_SENTENCE
    raw_idx = _SLIDES_CONTENT.find(_ANCHOR_SENTENCE)
    assert raw_idx != -1
    assert comment["start_index"] == raw_idx
    assert comment["end_index"] == raw_idx + len(_ANCHOR_SENTENCE)
