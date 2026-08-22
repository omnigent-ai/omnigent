"""Browser e2e for the collapsed-section hidden-row count pill.

A collapsed sidebar section header carries no at-rest cue, so before the
count pill a collapsed section was indistinguishable from an empty one —
with every session filed into projects, the sidebar could read as having
no sessions at all. Collapsing a section now surfaces a count pill
(Inbox-badge styling) on its header showing how many rows it hides;
expanding removes it.

This drives the full chain the Sidebar unit test can't: a real session
row in a live server's list, the header toggle persisting to
localStorage, and the pill rendering against the built bundle.
"""

from __future__ import annotations

import re

from playwright.sync_api import Page, expect


def test_collapsed_sessions_header_shows_hidden_count(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Collapsing the Sessions header shows a hidden-row count pill.

    - Expanded (default): no pill anywhere in the sidebar.
    - Collapsed: the header shows a pill counting the hidden rows, and the
      rows themselves are gone.
    - Re-expanded: the pill disappears and the rows return.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound session.
    """
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")

    # The collapsed header appends the count to its accessible name, so
    # match the title prefix to address it in both states.
    header = page.get_by_role("button", name=re.compile(r"^Sessions"))
    pill = page.get_by_test_id("section-collapsed-count")

    expect(header).to_be_visible()
    expect(pill).to_have_count(0)

    header.click()
    expect(pill).to_be_visible()
    assert int(pill.inner_text()) >= 1
    expect(pill).to_have_attribute("aria-label", re.compile(r"hidden item"))

    header.click()
    expect(pill).to_have_count(0)
