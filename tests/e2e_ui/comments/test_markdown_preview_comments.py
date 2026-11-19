"""E2E: review comments on rendered Markdown in the FileViewer Preview.

The unit suite covers the selection-to-source mapping matrix. These journeys
cover what only a real browser shows: a mouse-drag selection over rendered
Markdown, the floating Add comment button, the stored raw-source offsets, the
CSS highlight painted over the preview, and the same comment in the editor.
"""

from __future__ import annotations

import io
import re

import httpx
import pytest
from PIL import Image, ImageStat
from playwright.sync_api import Locator, Page, expect

from tests.e2e_ui.conftest import open_right_rail, switch_markdown_view_mode

_FILE_PATH = "preview_review.md"

_MARKDOWN = """\
# Release checklist

Ship the **preview** comments before the beta.

- Confirm the rollout plan with support.
- Update the changelog.
"""

# The drag starts inside the bold word and ends after plain text, so the stored
# range must skip the closing ``**`` markup between them.
_SELECTED_TEXT = "preview comments"
_SELECTED_RAW = "preview** comments"
_EXISTING_ANCHOR = "rollout plan"


def _seed_file(base_url: str, session_id: str) -> None:
    httpx.put(
        f"{base_url}/v1/sessions/{session_id}/resources/environments/default/filesystem/{_FILE_PATH}",
        json={"content": _MARKDOWN, "encoding": "utf-8"},
        timeout=10.0,
    ).raise_for_status()


def _comments(base_url: str, session_id: str) -> list[dict]:
    response = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/comments?path={_FILE_PATH}",
        timeout=10.0,
    )
    response.raise_for_status()
    return response.json()


def _open_preview(page: Page, base_url: str, session_id: str) -> tuple[Locator, Locator]:
    page.goto(f"{base_url}/c/{session_id}")
    open_right_rail(page)
    file_button = page.get_by_role("button", name=re.compile(re.escape(_FILE_PATH))).filter(
        has_text=_FILE_PATH
    )
    expect(file_button).to_be_visible(timeout=30_000)
    file_button.click()
    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer).to_be_visible()
    switch_markdown_view_mode(page, file_viewer, "Preview")
    # An open menu blocks pointer events on the page; wait until it has closed.
    expect(page.get_by_role("menu")).to_have_count(0)
    preview = file_viewer.locator(".markdown-preview")
    expect(preview).to_contain_text("Ship the preview comments", timeout=10_000)
    return file_viewer, preview


_TEXT_RECTS_JS = """(element, text) => {
  const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
  const nodes = [];
  let full = "";
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    nodes.push([node, full.length]);
    full += node.data;
  }
  const start = full.indexOf(text);
  if (start < 0) return null;
  const locate = (offset, isEnd) => {
    for (const [node, base] of nodes) {
      const within = offset - base;
      if (within >= 0 && (within < node.data.length || (isEnd && within === node.data.length))) {
        return [node, within];
      }
    }
    return null;
  };
  const range = document.createRange();
  range.setStart(...locate(start, false));
  range.setEnd(...locate(start + text.length, true));
  return Array.from(range.getClientRects(), (rect) => ({
    x: rect.left, y: rect.top, width: rect.width, height: rect.height,
  }));
}"""


def _text_rects(block: Locator, text: str) -> list[dict[str, float]]:
    """Client rects of the first occurrence of ``text`` in ``block``'s text."""
    rects = block.evaluate(_TEXT_RECTS_JS, text)
    assert rects, f"{text!r} is not rendered in the block"
    return rects


def _drag_select(page: Page, block: Locator, text: str) -> None:
    """Drag the mouse across ``text`` inside ``block``, like a user would."""
    # The viewer may still be sliding in; measure only once its layout settles.
    handle = block.element_handle()
    handle.wait_for_element_state("stable")
    rects = _text_rects(block, text)
    middle = rects[0]["y"] + rects[0]["height"] / 2
    page.mouse.move(rects[0]["x"] + 1, middle)
    page.mouse.down()
    page.mouse.move(rects[-1]["x"] + rects[-1]["width"] - 1, middle, steps=12)
    page.mouse.up()


def _mean_color(page: Page, rect: dict[str, float]) -> tuple[float, float, float]:
    image = Image.open(io.BytesIO(page.screenshot(clip=rect))).convert("RGB")
    red, green, blue = ImageStat.Stat(image).mean
    return red, green, blue


def _color_distance(first: tuple[float, ...], second: tuple[float, ...]) -> float:
    return sum(abs(a - b) for a, b in zip(first, second, strict=True))


def _wait_for_highlights(page: Page, name: str, count: int = 1, *, exact: bool = False) -> None:
    """Wait until the CSS custom highlight ``name`` holds ``count`` (or more) ranges."""
    page.wait_for_function(
        """([name, count, exact]) => {
          const size = globalThis.CSS?.highlights?.get(name)?.size || 0;
          return exact ? size === count : size >= count;
        }""",
        arg=[name, count, exact],
    )


def test_add_comment_from_markdown_preview(
    request: pytest.FixtureRequest,
    seeded_session: tuple[str, str],
) -> None:
    """Select rendered text in Preview, comment on it, and find it in Edit."""
    base_url, session_id = seeded_session
    _seed_file(base_url, session_id)
    page: Page = request.getfixturevalue("page")
    file_viewer, preview = _open_preview(page, base_url, session_id)

    # The old switch-to-edit hint is gone for regular Markdown.
    expect(file_viewer.get_by_text("switch to edit mode", exact=False)).to_have_count(0)

    paragraph = preview.locator("p").filter(has_text="Ship the")
    _drag_select(page, paragraph, _SELECTED_TEXT)
    selected = page.evaluate("window.getSelection()?.toString()")
    assert selected == _SELECTED_TEXT, f"drag selected {selected!r}"
    add_comment = page.get_by_role("button", name="Add comment", exact=True)
    expect(add_comment).to_be_visible()
    add_comment.click()

    expect(file_viewer.get_by_text(f"Selection: {_SELECTED_TEXT}", exact=True)).to_be_visible()
    _wait_for_highlights(page, "md-preview-comment-pending")
    draft = file_viewer.get_by_placeholder("Add a comment", exact=False)
    body = "Does this cover the large-file fallback too?"
    draft.fill(body)
    file_viewer.get_by_role("button", name="Add Comment", exact=True).click()
    expect(file_viewer).to_contain_text(body)

    stored = _comments(base_url, session_id)
    assert len(stored) == 1, stored
    raw_start = _MARKDOWN.index(_SELECTED_RAW)
    assert stored[0]["start_index"] == raw_start
    assert stored[0]["end_index"] == raw_start + len(_SELECTED_RAW)
    assert stored[0]["anchor_content"] == _SELECTED_TEXT
    _wait_for_highlights(page, "md-preview-comment")
    # The saved comment is painted over the rendered words, not just registered.
    page.evaluate("window.getSelection()?.removeAllRanges()")
    highlighted = _mean_color(page, _text_rects(paragraph, "comments")[0])
    plain = _mean_color(page, _text_rects(paragraph, "before")[0])
    assert _color_distance(highlighted, plain) > 20, (highlighted, plain)

    switch_markdown_view_mode(page, file_viewer, "Edit")
    editor_mark = file_viewer.locator(".tiptap [data-comment-id]")
    expect(editor_mark.first).to_be_visible(timeout=10_000)
    # The editor splits the mark at the bold boundary.
    marked = editor_mark.evaluate_all("(marks) => marks.map((mark) => mark.textContent).join('')")
    assert marked == _SELECTED_TEXT


def test_click_preview_highlight_activates_comment(
    request: pytest.FixtureRequest,
    seeded_session: tuple[str, str],
) -> None:
    """Clicking highlighted Preview text selects its card in the Comments panel."""
    base_url, session_id = seeded_session
    _seed_file(base_url, session_id)
    for anchor, body in (
        (_EXISTING_ANCHOR, "Who owns the rollout?"),
        ("changelog", "Link the migration notes."),
    ):
        start = _MARKDOWN.index(anchor)
        httpx.post(
            f"{base_url}/v1/sessions/{session_id}/comments",
            json={
                "path": _FILE_PATH,
                "body": body,
                "start_index": start,
                "end_index": start + len(anchor),
                "anchor_content": anchor,
            },
            timeout=10.0,
        ).raise_for_status()
    page: Page = request.getfixturevalue("page")
    file_viewer, preview = _open_preview(page, base_url, session_id)

    _wait_for_highlights(page, "md-preview-comment", 2)

    item = preview.locator("li").filter(has_text="rollout plan")
    rollout = _text_rects(item, "rollout")[0]
    page.mouse.click(rollout["x"] + rollout["width"] / 2, rollout["y"] + rollout["height"] / 2)

    card = file_viewer.locator("div.rounded-lg.border").filter(has_text="Who owns the rollout?")
    expect(card).to_be_visible()
    expect(card).to_have_class(re.compile(r"border-primary"))
    _wait_for_highlights(page, "md-preview-comment-active", 1, exact=True)
