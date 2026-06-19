"""Browser e2e for labelling a session from the sidebar.

The row kebab's "Label" item opens a dialog with a text input; saving
it fires ``PATCH /v1/sessions/{id}`` with
``labels: {"user.label": "<value>"}``. The label is stored in the
existing ``conversation_labels`` table and must survive a full page
reload — that's the regression this guards: a label that only patches
the in-memory TanStack cache (and is lost on reload) would pass the
unit tests but fail here.

After labelling, the sidebar shows a color-coded badge on the row and
a clickable filter chip below the search bar.
"""

from __future__ import annotations

import uuid

import httpx
from playwright.sync_api import Locator, Page, expect


def _row(page: Page, session_id: str) -> Locator:
    """Locate the sidebar row (``<li>``) for *session_id* by its href."""
    return page.locator("li").filter(has=page.locator(f'a[href="/c/{session_id}"]'))


def test_label_session_persists_and_filters(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Setting a label via the kebab persists across a reload.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound session.
    """
    base_url, session_id = seeded_session
    label_text = f"e2e-label-{uuid.uuid4().hex[:8]}"

    page.goto(f"{base_url}/c/{session_id}")

    row = _row(page, session_id)
    expect(row).to_be_visible()

    # Open the kebab and pick Label.
    row.hover()
    row.get_by_test_id("conversation-actions").click()
    page.get_by_test_id("label-conversation").click()

    # The label dialog appears; type the label and save.
    label_input = page.locator('input[placeholder*="project-x"]')
    expect(label_input).to_be_visible()
    label_input.fill(label_text)
    page.locator("button:has-text('Save')").click()

    # The label badge appears on the row.
    row_link = page.locator(f'a[href="/c/{session_id}"]')
    expect(row_link.locator(f"text={label_text}")).to_be_visible()

    # A filter chip appears below the search bar.
    filter_chip = page.locator(f"button:has-text('{label_text}')").first
    expect(filter_chip).to_be_visible()

    # Reload: the label must survive (persisted server-side).
    page.reload()
    row_link = page.locator(f'a[href="/c/{session_id}"]')
    expect(row_link.locator(f"text={label_text}")).to_be_visible()

    # The server agrees — the label was persisted.
    snap = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    snap.raise_for_status()
    labels = snap.json().get("labels", {})
    assert labels.get("user.label") == label_text, (
        f"server should persist label {label_text!r}, got {labels!r}"
    )


def test_remove_label(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Removing a label via the dialog clears it server-side.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound session.
    """
    base_url, session_id = seeded_session
    label_text = f"e2e-remove-{uuid.uuid4().hex[:8]}"

    # Set a label via the API first.
    httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"labels": {"user.label": label_text}},
        timeout=10.0,
    ).raise_for_status()

    page.goto(f"{base_url}/c/{session_id}")

    row = _row(page, session_id)
    expect(row).to_be_visible()

    # Confirm the label badge is visible.
    row_link = page.locator(f'a[href="/c/{session_id}"]')
    expect(row_link.locator(f"text={label_text}")).to_be_visible()

    # Open kebab → Label → Remove.
    row.hover()
    row.get_by_test_id("conversation-actions").click()
    page.get_by_test_id("label-conversation").click()

    remove_button = page.locator("button:has-text('Remove')")
    expect(remove_button).to_be_visible()
    remove_button.click()

    # The label badge should disappear from the row.
    expect(row_link.locator(f"text={label_text}")).not_to_be_visible()

    # The server confirms the label is cleared.
    snap = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    snap.raise_for_status()
    labels = snap.json().get("labels", {})
    assert labels.get("user.label", "") == "", (
        f"server should clear the label, got {labels!r}"
    )
