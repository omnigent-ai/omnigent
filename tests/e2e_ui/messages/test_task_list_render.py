"""E2E: task lists render as bullet-free checkboxes in chat message bubbles.

Chat counterpart to ``files/test_markdown_task_lists.py`` (which covers the file
editor). Chat bubbles render markdown via Streamdown + remark-gfm: remark-gfm
tags each task item ``task-list-item`` with a disabled ``<input type="checkbox">``
but Streamdown also applies Tailwind ``list-disc``, so without the task-list CSS
a disc renders right next to the checkbox. This pins the
``[data-streamdown="list-item"].task-list-item`` rule in ``web/src/index.css``:
the marker is dropped per task item, while a plain list item keeps its ``disc``.

User messages render through the SAME Streamdown path as assistant replies
(``FilePathAwareMessageResponse`` in ``ChatPage.tsx``), so a typed task list
exercises the chat rendering directly — the assertion is on the user bubble,
which renders client-side independent of any agent reply, so it's deterministic
with or without gateway credentials.
"""

from __future__ import annotations

from playwright.sync_api import Page, expect

# Stable aria-label; the visible placeholder is state-dependent.
_COMPOSER = "Message the agent"
_USER_BUBBLE = '[data-testid="message-bubble"][data-role="user"]'

# A task list (unchecked + checked) and, after a blank line, a plain bullet
# list. The plain list proves the bullet is dropped for task items only.
_MESSAGE = "- [ ] task one\n- [x] task two\n\nPlain list:\n\n- plain item"


def test_chat_task_list_renders_without_bullet(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A task-list chat message renders checkboxes with no disc; plain items keep theirs."""
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_label(_COMPOSER)
    expect(composer).to_be_enabled(timeout=30_000)
    composer.fill(_MESSAGE)
    page.get_by_role("button", name="Send", exact=True).click()

    # The user bubble renders the markdown client-side; no agent reply needed.
    bubble = page.locator(_USER_BUBBLE).last
    expect(bubble).to_be_visible(timeout=30_000)

    # Two task items, each a real checkbox; checked state tracks [ ] vs [x].
    task_items = bubble.locator("li.task-list-item")
    expect(task_items).to_have_count(2)
    checkboxes = bubble.locator('li.task-list-item > input[type="checkbox"]')
    expect(checkboxes).to_have_count(2)
    expect(checkboxes.nth(0)).not_to_be_checked()
    expect(checkboxes.nth(1)).to_be_checked()
    expect(bubble).to_contain_text("task one")
    expect(bubble).to_contain_text("task two")

    # The fix: each task item drops its list marker...
    for i in range(2):
        marker = task_items.nth(i).evaluate("el => getComputedStyle(el).listStyleType")
        assert marker == "none", f"task item {i} should have no marker, got {marker!r}"

    # ...while a plain list item (no `task-list-item`) keeps its disc bullet.
    plain = bubble.locator("li:not(.task-list-item)").filter(has_text="plain item")
    expect(plain).to_have_count(1)
    plain_marker = plain.evaluate("el => getComputedStyle(el).listStyleType")
    assert plain_marker == "disc", f"plain item should keep its bullet, got {plain_marker!r}"
