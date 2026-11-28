"""Browser e2e for renaming a session from the sidebar.

The row kebab's "Rename" item swaps the row for an inline edit field
(``data-testid="rename-conversation-input"``); committing it (Enter)
fires ``PATCH /v1/sessions/{id}`` with the new title. The rename is
persisted server-side, so it must survive a full page reload — that's
the regression this guards: a rename that only patches the in-memory
TanStack cache (and is lost on reload) would pass the ``Sidebar`` unit
tests, which mock the mutation, but fail here.

We assert persistence two ways after a reload: the row re-renders with
the new title from a fresh ``GET /v1/sessions``, and the server's
own ``GET /v1/sessions/{id}`` snapshot returns it.
"""

from __future__ import annotations

import sys
import uuid

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

from omnigent.entities import USER_SESSION_TITLE_MAX_CHARS


def _row(page: Page, session_id: str) -> Locator:
    """Locate the sidebar row (``<li>``) for *session_id* by its href."""
    return page.locator("li").filter(has=page.locator(f'a[href="/c/{session_id}"]'))


# Records the row's rendered title once per animation frame, so a title that is
# only wrong for a frame is still observable. Reads the title span's own text
# nodes to skip the screen-reader "(unread)" suffix, and flags whether the
# inline edit field was mounted at that frame.
_FRAME_SAMPLER = """
(sessionId) => {
  window.__renameSamples = [];
  const tick = () => {
    const link = document.querySelector(`a[href="/c/${sessionId}"]`);
    const span = link && (link.querySelector('span.relative') || link.querySelector('span'));
    window.__renameSamples.push({
      text: span
        ? Array.from(span.childNodes)
            .filter((n) => n.nodeType === 3)
            .map((n) => n.textContent)
            .join('')
            .trim()
        : null,
      editing: !!document.querySelector('[data-testid="rename-conversation-input"]'),
    });
    window.__renameRaf = requestAnimationFrame(tick);
  };
  window.__renameRaf = requestAnimationFrame(tick);
}
"""


def test_rename_session_enforces_user_title_limit(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The browser accepts 200 title characters and blocks the 201st."""
    base_url, session_id = seeded_session
    title = "x" * USER_SESSION_TITLE_MAX_CHARS

    page.goto(f"{base_url}/c/{session_id}")
    row = _row(page, session_id)
    expect(row).to_be_visible()
    row.hover()
    row.get_by_test_id("conversation-actions").click()
    page.get_by_test_id("rename-conversation").click()

    edit = page.get_by_test_id("rename-conversation-input")
    expect(edit).to_have_attribute("maxlength", str(USER_SESSION_TITLE_MAX_CHARS))
    edit.fill(title)
    edit.press("End")
    edit.type("y")
    expect(edit).to_have_value(title)
    edit.press("Enter")

    snap = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    snap.raise_for_status()
    assert snap.json().get("title") == title


def test_rename_row_never_repaints_the_old_title(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The row must not flash the pre-rename name as the editor closes.

    The rename's cache write reaches the row as a prop from the sidebar
    list above it, which re-renders a tick after the row's own
    "close the editor" state change. Before the row held the committed
    title locally, that one-frame gap repainted the old name.

    Sampling per animation frame is what makes this visible at all: the
    auto-retrying ``expect`` above re-polls until the new title lands, so
    it passes with or without the flicker.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound session.
    """
    base_url, session_id = seeded_session
    new_title = f"e2e-noflicker-{uuid.uuid4().hex[:8]}"

    page.goto(f"{base_url}/c/{session_id}")

    row = _row(page, session_id)
    expect(row).to_be_visible()
    link = page.locator(f'a[href="/c/{session_id}"]')
    old_title = link.inner_text().strip()

    row.hover()
    row.get_by_test_id("conversation-actions").click()
    page.get_by_test_id("rename-conversation").click()
    edit = page.get_by_test_id("rename-conversation-input")
    expect(edit).to_be_visible()

    page.evaluate(_FRAME_SAMPLER, session_id)
    edit.fill(new_title)
    edit.press("Enter")
    expect(link).to_contain_text(new_title)

    samples = page.evaluate(
        "() => { cancelAnimationFrame(window.__renameRaf); return window.__renameSamples; }"
    )
    edit_frames = [i for i, s in enumerate(samples) if s["editing"]]
    assert edit_frames, "sampler never observed the inline edit field — rename did not open"

    after_commit = samples[edit_frames[-1] + 1 :]
    stale = [s for s in after_commit if s["text"] == old_title]
    assert not stale, (
        f"row repainted the pre-rename title {old_title!r} for {len(stale)} frame(s) after "
        f"the editor closed; frames after commit: {[s['text'] for s in after_commit[:5]]}"
    )


def _rename_shortcut() -> str:
    """Return the platform's Cmd/Ctrl+Alt+R chord for Playwright."""
    modifier = "Meta" if sys.platform == "darwin" else "Control"
    return f"{modifier}+Alt+r"


def test_rename_hotkey_edits_sidebar_rows_in_sequence(
    request: pytest.FixtureRequest,
    seeded_session_pair: tuple[str, str, str],
) -> None:
    """The shortcut renames the open session in its sidebar row, one session after another."""
    base_url, first_id, second_id = seeded_session_pair
    titles = {first_id: "Rename shortcut first", second_id: "Rename shortcut second"}
    for session_id, title in titles.items():
        httpx.patch(
            f"{base_url}/v1/sessions/{session_id}", json={"title": title}, timeout=10.0
        ).raise_for_status()
    page: Page = request.getfixturevalue("page")
    page.goto(f"{base_url}/c/{first_id}")

    composer = page.get_by_placeholder("Send a message…")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill("Keep this draft while renaming")
    # The edit field replaces the row's link, so locate it within the sidebar.
    edit = page.locator("aside.conversations-sidebar").get_by_test_id("rename-conversation-input")
    renamed = {
        first_id: "First renamed from sidebar",
        second_id: "Second renamed from sidebar",
    }
    for session_id in (first_id, second_id):
        row = _row(page, session_id)
        if session_id != first_id:
            row.get_by_role("link").click()
            expect(page.get_by_test_id("header-title")).to_have_text(titles[session_id])
        page.keyboard.press(_rename_shortcut())
        expect(edit).to_be_focused()
        expect(edit).to_have_value(titles[session_id])
        assert edit.evaluate("el => [el.selectionStart, el.selectionEnd]") == [
            0,
            len(titles[session_id]),
        ]
        expect(page.get_by_test_id("header-title-input")).to_have_count(0)
        page.keyboard.type(renamed[session_id])
        edit.press("Enter")
        expect(edit).to_have_count(0)
        expect(row.get_by_role("link")).to_contain_text(renamed[session_id])
        expect(page.get_by_test_id("header-title")).to_have_text(renamed[session_id])

    page.keyboard.press(_rename_shortcut())
    expect(edit).to_be_focused()
    expect(edit).to_have_value(renamed[second_id])
    edit.fill("Cancel this rename")
    edit.press("Escape")
    expect(edit).to_have_count(0)
    expect(_row(page, second_id).get_by_role("link")).to_contain_text(renamed[second_id])

    _row(page, first_id).get_by_role("link").click()
    expect(composer).to_have_value("Keep this draft while renaming")
    page.reload()
    for session_id, title in renamed.items():
        expect(_row(page, session_id).get_by_role("link")).to_contain_text(title)
        snapshot = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
        snapshot.raise_for_status()
        assert snapshot.json()["title"] == title
