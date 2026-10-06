"""E2E: commenting on a rendered ``*.wireframe.html`` (the bridge path).

Wireframes open in ``WireframeViewer``. The viewer injects the same comment
bridge into the preview srcDoc (after design injection) so selecting rendered
text on a screen opens the floating Add comment button and stores anchors
against the workspace file source.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

_REPO_ROOT = Path(__file__).resolve().parents[3]

_WIREFRAME_PATH = "app.wireframe.html"
_ANCHOR_SENTENCE = "uniquewireframeanchortoken review this screen sentence"

_WIREFRAME_CONTENT = f"""\
<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <title>App wireframe</title>
  </head>
  <body>
    <section data-screen="home" data-title="Home">
      <h1>Home</h1>
      <p>Landing copy without the anchor.</p>
    </section>
    <section data-screen="settings" data-title="Settings">
      <h1>Settings</h1>
      <p id="anchor">{_ANCHOR_SENTENCE}</p>
    </section>
  </body>
</html>
"""


def _cleanup_session_workdir(session_id: str) -> None:
    shutil.rmtree(_REPO_ROOT / session_id, ignore_errors=True)


@pytest.fixture
def seeded_wireframe(seeded_session: tuple[str, str]) -> Iterator[tuple[str, str, str]]:
    """Seed the wireframe and yield ``(base_url, session_id, path)``."""
    base_url, session_id = seeded_session
    resp = httpx.put(
        f"{base_url}/v1/sessions/{session_id}"
        f"/resources/environments/default/filesystem/{_WIREFRAME_PATH}",
        json={"content": _WIREFRAME_CONTENT, "encoding": "utf-8"},
        timeout=10.0,
    )
    resp.raise_for_status()
    try:
        yield (base_url, session_id, _WIREFRAME_PATH)
    finally:
        _cleanup_session_workdir(session_id)


def test_wireframe_preview_add_comment(
    page: Page,
    seeded_wireframe: tuple[str, str, str],
) -> None:
    """Select rendered wireframe text, add a comment, and verify it persists."""
    base_url, session_id, file_path = seeded_wireframe
    page.set_viewport_size({"width": 1600, "height": 900})
    page.goto(f"{base_url}/c/{session_id}?file={_WIREFRAME_PATH}")

    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer).to_be_visible()

    iframe_el = file_viewer.locator('iframe[title="Wireframe"]')
    expect(iframe_el).to_be_visible(timeout=10_000)

    # Switch to the screen that holds the anchor.
    file_viewer.locator('select[aria-label="Screen"]').select_option("settings")

    preview = file_viewer.frame_locator('iframe[title="Wireframe"]')
    expect(preview.locator("#anchor")).to_have_text(_ANCHOR_SENTENCE, timeout=10_000)
    preview.locator("#anchor").select_text()

    add_comment_btn = page.get_by_role("button", name="Add comment")
    expect(add_comment_btn).to_be_visible(timeout=10_000)
    add_comment_btn.click()

    expect(file_viewer.locator("span.font-semibold", has_text="Comments")).to_be_visible()
    comment_body = "This screen needs a citation."
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
    raw_idx = _WIREFRAME_CONTENT.find(_ANCHOR_SENTENCE)
    assert raw_idx != -1
    assert comment["start_index"] == raw_idx
    assert comment["end_index"] == raw_idx + len(_ANCHOR_SENTENCE)
