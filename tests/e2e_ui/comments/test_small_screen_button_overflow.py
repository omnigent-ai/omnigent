"""Keep file-viewer comment controls usable at narrow desktop widths."""

from __future__ import annotations

import re
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

from tests.e2e_ui.conftest import open_right_rail

_FILE_PATH = "AGENTS.md"

# Must appear exactly once so the drag-selection is unambiguous.
_SELECTABLE_TEXT = "Guidance for AI agents"

_MARKDOWN_CONTENT = f"""\
# Agent guidance

{_SELECTABLE_TEXT} (Claude Code, Copilot, Cursor, etc.) working in this
repository. See CONTRIBUTING.md for the full contributor workflow.

## Committing

Run the pre-commit hook before committing (pre-commit run --all-files, or
let it run on staged files via git commit). Fix any issues it reports so the
commit lands clean - CI runs the same checks.
"""

# Both widths use the desktop layout.
_WIDTHS = [1024, 900]
_HEIGHT = 860

# Guard against a one-word-wide editor column.
_MIN_EDITOR_WIDTH = 80


@pytest.fixture
def seeded_markdown_session(
    seeded_session: tuple[str, str],
) -> Iterator[tuple[str, str]]:
    """Seed a markdown file into the session and yield (base_url, session_id)."""
    base_url, session_id = seeded_session
    file_url = (
        f"{base_url}/v1/sessions/{session_id}"
        f"/resources/environments/default/filesystem/{_FILE_PATH}"
    )
    resp = httpx.put(
        file_url,
        json={"content": _MARKDOWN_CONTENT, "encoding": "utf-8"},
        timeout=10.0,
    )
    resp.raise_for_status()
    yield (base_url, session_id)


def _open_editor_with_pending_comment(page: Page, base_url: str, session_id: str) -> Locator:
    """Open the seeded file and begin a comment in the visible viewer."""
    page.goto(f"{base_url}/c/{session_id}")
    open_right_rail(page)

    # The hidden mobile drawer also mounts a FileViewer.
    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    if not file_viewer.is_visible():
        file_button = page.get_by_role("button", name=re.compile(re.escape(_FILE_PATH))).filter(
            has_text=_FILE_PATH
        )
        expect(file_button.first).to_be_visible(timeout=30_000)
        file_button.first.click()
    expect(file_viewer).to_be_visible()

    editor_content = file_viewer.locator("[contenteditable='true']")
    expect(editor_content).to_be_visible(timeout=10_000)
    expect(editor_content).to_contain_text(_SELECTABLE_TEXT)

    selectable = editor_content.get_by_text(_SELECTABLE_TEXT)
    expect(selectable).to_be_visible()
    selectable.select_text()
    floating_add = page.get_by_role("button", name=re.compile("Add comment", re.IGNORECASE))
    expect(floating_add).to_be_visible()
    floating_add.click()

    expect(file_viewer.locator("span.font-semibold", has_text="Comments")).to_be_visible()
    expect(file_viewer.locator("textarea[placeholder='Add a comment…']")).to_be_visible()
    return file_viewer


def _horizontal_violations(page: Page, file_viewer: Locator, width: int) -> list[str]:
    """Geometry checks for one window size; returns human-readable violations."""
    violations: list[str] = []

    panel_controls: list[tuple[str, Locator]] = [
        ("Add Comment button", file_viewer.get_by_role("button", name="Add Comment")),
        ("comment textarea", file_viewer.locator("textarea[placeholder='Add a comment…']")),
        ("Address All button", file_viewer.get_by_role("button", name="Address All")),
        ("Open tab", file_viewer.get_by_role("button", name="Open", exact=True)),
        ("Addressed tab", file_viewer.get_by_role("button", name="Addressed", exact=True)),
    ]

    control_boxes: dict[str, dict[str, float]] = {}
    for label, locator in panel_controls:
        expect(locator).to_be_attached()
        box = locator.bounding_box()
        assert box is not None, f"{label} has no bounding box"
        control_boxes[label] = box
        right = box["x"] + box["width"]
        if right > width + 1 or box["x"] < -1:
            violations.append(
                f"{label} extends past the window edge: "
                f"x={box['x']:.0f} right={right:.0f} vs window width {width}"
            )

    # An overlapping toolbar button makes either control unusable.
    toolbar_buttons = file_viewer.locator(
        "button[title='Bold (⌘B)'], button[title='Italic (⌘I)'],"
        " button[title='Strikethrough'], button[title='Normal'],"
        " button[title='Bullet list'], button[title='Numbered list']"
    )
    for i in range(toolbar_buttons.count()):
        tb = toolbar_buttons.nth(i)
        if not tb.is_visible():
            continue
        tb_box = tb.bounding_box()
        if tb_box is None:
            continue
        tb_title = tb.get_attribute("title") or f"toolbar button {i}"
        for label, box in control_boxes.items():
            overlap_x = min(box["x"] + box["width"], tb_box["x"] + tb_box["width"]) - max(
                box["x"], tb_box["x"]
            )
            overlap_y = min(box["y"] + box["height"], tb_box["y"] + tb_box["height"]) - max(
                box["y"], tb_box["y"]
            )
            if overlap_x > 4 and overlap_y > 4:
                violations.append(
                    f"{label} overlaps editor toolbar button {tb_title!r}: "
                    f"panel rect x={box['x']:.0f}..{box['x'] + box['width']:.0f} "
                    f"y={box['y']:.0f}..{box['y'] + box['height']:.0f} vs toolbar rect "
                    f"x={tb_box['x']:.0f}..{tb_box['x'] + tb_box['width']:.0f} "
                    f"y={tb_box['y']:.0f}..{tb_box['y'] + tb_box['height']:.0f}"
                )

    editor_box = file_viewer.locator("[contenteditable='true']").bounding_box()
    assert editor_box is not None, "editor has no bounding box"
    if editor_box["width"] < _MIN_EDITOR_WIDTH:
        violations.append(
            f"editor column squeezed unusable: width {editor_box['width']:.0f}px "
            f"(minimum usable {_MIN_EDITOR_WIDTH}px)"
        )

    return violations


_TAB_STRIP_CLIP_JS = """
() => {
  const out = [];
  const strip = [...document.querySelectorAll('.workspace-tab-strip')]
    .find((s) => s.getBoundingClientRect().width > 0);
  if (!strip) return out;
  for (const b of strip.querySelectorAll("button, [role='button']")) {
    const r = b.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) continue;
    const cs = getComputedStyle(b);
    if (cs.visibility === 'hidden' || cs.display === 'none') continue;
    let a = b.parentElement, container = null;
    while (a && a !== strip.parentElement) {
      if (getComputedStyle(a).overflowX !== 'visible') { container = a; break; }
      a = a.parentElement;
    }
    if (!container) continue;
    const scrollable = ['auto', 'scroll'].includes(getComputedStyle(container).overflowX);
    if (scrollable) continue;
    const cr = container.getBoundingClientRect();
    if (r.right > cr.right + 1 || r.left < cr.left - 1) {
      out.push({
        label: (b.getAttribute('aria-label') || b.title || b.textContent || '')
          .trim().replace(/\\s+/g, ' ').slice(0, 40),
        rect: [r.left, r.top, r.right, r.bottom].map(Math.round),
        container: [cr.left, cr.top, cr.right, cr.bottom].map(Math.round),
      });
    }
  }
  return out;
}
"""


def _tab_strip_violations(page: Page) -> list[str]:
    """Tab buttons clipped by a non-scrollable strip cannot be reached at all."""
    return [
        f"workspace tab {c['label']!r} is clipped outside its non-scrollable strip: "
        f"tab x={c['rect'][0]}..{c['rect'][2]} vs strip x={c['container'][0]}..{c['container'][2]}"
        for c in page.evaluate(_TAB_STRIP_CLIP_JS)
    ]


def test_comment_controls_stay_on_screen_on_smaller_window(
    page: Page,
    seeded_markdown_session: tuple[str, str],
) -> None:
    """Keep controls, editor, and tabs usable at both desktop widths."""
    base_url, session_id = seeded_markdown_session

    all_violations: list[str] = []
    for width in _WIDTHS:
        page.set_viewport_size({"width": width, "height": _HEIGHT})
        file_viewer = _open_editor_with_pending_comment(page, base_url, session_id)
        # Let the layout settle after panel-open before measuring.
        page.wait_for_timeout(300)
        for violation in _horizontal_violations(page, file_viewer, width):
            all_violations.append(f"[{width}x{_HEIGHT}] {violation}")
        for violation in _tab_strip_violations(page):
            all_violations.append(f"[{width}x{_HEIGHT}] {violation}")

    assert not all_violations, "button overflow on smaller screen:\n" + "\n".join(all_violations)
