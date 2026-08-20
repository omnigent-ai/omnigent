"""E2E: the Files panel keeps its scroll position across session switches.

The panel's scroll container clamps to the top while a newly-selected
session's file queries load; ``FilesPanel`` caches ``scrollTop`` per
conversation and restores it once the content is tall enough again. This
drives the real flow — scroll in session A, switch to session B via the
sidebar (client-side navigation, NOT ``page.goto``: a reload would reset
the module-level cache and dissolve the scenario), switch back — and
asserts the position survives.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Enough files that the tree overflows the rail and can scroll well past 0.
_FILE_COUNT = 80


def _seed_file(base_url: str, session_id: str, path: str) -> None:
    resp = httpx.put(
        f"{base_url}/v1/sessions/{session_id}/resources/environments/default/filesystem/{path}",
        json={"content": f"contents of {path}\n", "encoding": "utf-8"},
        timeout=10.0,
    )
    resp.raise_for_status()


@pytest.fixture
def seeded_scroll_sessions(
    seeded_session_pair: tuple[str, str, str],
) -> Iterator[tuple[str, str, str]]:
    base_url, session_a, session_b = seeded_session_pair
    for i in range(_FILE_COUNT):
        _seed_file(base_url, session_a, f"scroll_file_{i:03d}.txt")
    _seed_file(base_url, session_b, "other_session_file.txt")
    try:
        yield (base_url, session_a, session_b)
    finally:
        for session_id in (session_a, session_b):
            shutil.rmtree(_REPO_ROOT / session_id, ignore_errors=True)


def test_files_panel_scroll_survives_session_switch(
    page: Page,
    seeded_scroll_sessions: tuple[str, str, str],
) -> None:
    """Scroll in A, switch to B and back via the sidebar: position restored."""
    base_url, session_a, session_b = seeded_scroll_sessions
    page.goto(f"{base_url}/c/{session_a}?view=explore")

    rail = page.get_by_role("complementary", name="Workspace")
    expect(rail.get_by_text("scroll_file_000.txt")).to_be_visible(timeout=30_000)
    # Both sessions must be in the sidebar for the SPA switch.
    expect(page.locator(f'a[href="/c/{session_b}"]')).to_be_visible(timeout=30_000)

    section = rail.locator("section")
    # Scroll partway down; a real scroll event fires and the panel saves it.
    section.evaluate("el => { el.scrollTop = 300; }")
    expect(section).to_have_js_property("scrollTop", 300)

    # Switch to session B in the SPA and let its (near-empty) panel render.
    page.locator(f'a[href="/c/{session_b}"]').click()
    expect(page).to_have_url(f"{base_url}/c/{session_b}", timeout=15_000)
    expect(rail.get_by_text("other_session_file.txt")).to_be_visible(timeout=30_000)

    # Back to session A: once its tree is tall again, the position returns.
    page.locator(f'a[href="/c/{session_a}"]').click()
    expect(page).to_have_url(f"{base_url}/c/{session_a}", timeout=15_000)
    expect(rail.get_by_text("scroll_file_000.txt")).to_be_visible(timeout=30_000)
    expect(section).to_have_js_property("scrollTop", 300, timeout=10_000)


# Climb from the rendered markdown editor to whichever ancestor actually
# scrolls (the ref'd container in MarkdownRichTextViewer), then set / read
# its scrollTop. Re-run after a session round-trip: the viewer remounts, so
# the element must be re-found each time.
_FIND_EDITOR_SCROLLER_JS = """
  const findScroller = () => {
    for (const pm of document.querySelectorAll('.ProseMirror')) {
      let el = pm;
      while (el && el.scrollHeight <= el.clientHeight + 1) el = el.parentElement;
      if (el && el !== document.documentElement && el.clientHeight > 0) return el;
    }
    return null;
  };
"""

_SET_EDITOR_SCROLL_JS = f"""
(target) => {{
  {_FIND_EDITOR_SCROLLER_JS}
  const el = findScroller();
  if (!el) return null;
  el.scrollTop = target;
  return el.scrollTop;
}}
"""

_GET_EDITOR_SCROLL_JS = f"""
() => {{
  {_FIND_EDITOR_SCROLLER_JS}
  const el = findScroller();
  return el ? el.scrollTop : null;
}}
"""


def test_open_file_scroll_survives_session_switch(
    page: Page,
    seeded_scroll_sessions: tuple[str, str, str],
) -> None:
    """A file open in the viewer reopens at its saved scroll offset.

    The app persists which file is open per session, so switching back
    re-opens the viewer — historically at the top. Uses a long markdown
    file (default view mode is the rich-text editor).
    """
    base_url, session_a, session_b = seeded_scroll_sessions
    long_md = "".join(
        f"## Section {i}\n\nSome paragraph text for section {i}.\n\n" for i in range(120)
    )
    resp = httpx.put(
        f"{base_url}/v1/sessions/{session_a}/resources/environments/default/filesystem/long_doc.md",
        json={"content": long_md, "encoding": "utf-8"},
        timeout=10.0,
    )
    resp.raise_for_status()

    page.goto(f"{base_url}/c/{session_a}?view=explore")
    rail = page.get_by_role("complementary", name="Workspace")
    # `.first`: the file renders both as a tree row and in the changed strip.
    row = rail.get_by_role("button", name=re.compile("long_doc.md")).first
    expect(row).to_be_visible(timeout=30_000)
    row.click()
    expect(page.locator(".ProseMirror").first).to_be_visible(timeout=30_000)

    # Scroll partway into the document.
    assert page.evaluate(_SET_EDITOR_SCROLL_JS, 250) == 250

    # Round-trip through session B via the sidebar (client-side navigation).
    page.locator(f'a[href="/c/{session_b}"]').click()
    expect(page).to_have_url(f"{base_url}/c/{session_b}", timeout=15_000)
    expect(rail.get_by_text("other_session_file.txt")).to_be_visible(timeout=30_000)

    page.locator(f'a[href="/c/{session_a}"]').click()
    expect(page).to_have_url(f"{base_url}/c/{session_a}", timeout=15_000)
    # The viewer re-opens the remembered file…
    expect(page.locator(".ProseMirror").first).to_be_visible(timeout=30_000)
    # …and returns to the saved offset once the content is tall enough.
    restored = None
    for _ in range(50):
        restored = page.evaluate(_GET_EDITOR_SCROLL_JS)
        if restored == 250:
            break
        page.wait_for_timeout(200)
    assert restored == 250
