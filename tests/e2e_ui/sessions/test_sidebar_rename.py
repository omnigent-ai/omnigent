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

import uuid

import httpx
from playwright.sync_api import Locator, Page, expect


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


def test_rename_session_is_preserved(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Renaming via the kebab persists across a reload and on the server.

    Failure modes this catches that the mocked unit test can't:

    - The PATCH never fires (or 4xxs on wire drift) so the title reverts
      on reload.
    - The rename only patches the client cache and is lost once the
      sidebar refetches ``GET /v1/sessions`` after a reload.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound session.
    """
    base_url, session_id = seeded_session
    new_title = f"e2e-renamed-{uuid.uuid4().hex[:8]}"

    page.goto(f"{base_url}/c/{session_id}")

    row = _row(page, session_id)
    expect(row).to_be_visible()

    # Open the row kebab and pick Rename. Hover first so the desktop
    # hover-revealed kebab trigger is interactable.
    row.hover()
    row.get_by_test_id("conversation-actions").click()
    page.get_by_test_id("rename-conversation").click()

    # The inline edit field replaces the row; type the new title + Enter.
    edit = page.get_by_test_id("rename-conversation-input")
    expect(edit).to_be_visible()
    edit.fill(new_title)
    edit.press("Enter")

    # The row reflects the new title immediately (optimistic cache patch).
    expect(page.locator(f'a[href="/c/{session_id}"]')).to_contain_text(new_title)

    # Reload: the sidebar refetches GET /v1/sessions from scratch. A
    # rename that only lived in the client cache would revert here.
    page.reload()
    expect(page.locator(f'a[href="/c/{session_id}"]')).to_contain_text(new_title)

    # And the server agrees — the rename was persisted, not just rendered.
    snap = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    snap.raise_for_status()
    assert snap.json().get("title") == new_title, (
        f"server should persist the renamed title {new_title!r}, got {snap.json().get('title')!r}"
    )


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
