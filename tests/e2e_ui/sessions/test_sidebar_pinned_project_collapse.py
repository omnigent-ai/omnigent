"""Regression test for #2506: clicking a pinned session that belongs to a
project was auto-expanding the project folder even after the user manually
collapsed it.

The sidebar has an "auto-expand the active session's project" effect so
navigating to a filed session reveals it. That effect ran on every navigation
into a pinned session too — even though a pinned session is already reachable
from the Pinned section — so a user who collapsed the project saw it re-open
every time they clicked the pin. The fix guards the effect: pinned targets
skip the auto-expand.

Drives the real chain the ``Sidebar`` unit tests mock: seeded runner-bound
sessions filed into a project via the real ``PATCH /v1/sessions/{id}``, the
pin quick action, the project folder's aria-expanded state, and localStorage
persistence.
"""

from __future__ import annotations

import re
import uuid

import httpx
from playwright.sync_api import Locator, Page, expect


def _set_title(base_url: str, session_id: str, title: str) -> None:
    """Give a session a unique title so its row is easy to spot in a shared
    server."""
    resp = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"title": title},
        timeout=10.0,
    )
    resp.raise_for_status()


def _file_into_project(base_url: str, session_id: str, project: str) -> None:
    """File a session under *project* via the reserved ``omni_project`` label.

    Mirrors what the row kebab's "Add to project" does, but skips the UI so
    the test can prepare the fixture state in one call.
    """
    resp = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"labels": {"omni_project": project}},
        timeout=10.0,
    )
    resp.raise_for_status()


def _section(page: Page, title: str) -> Locator:
    """Locate the sidebar ``<section>`` whose collapse-header button matches
    *title*."""
    return page.locator("section").filter(has=page.get_by_role("button", name=title, exact=True))


def _row(page: Page, session_id: str) -> Locator:
    """Locate the sidebar row (``<li>``) for *session_id* by its href."""
    return page.locator("li").filter(has=page.locator(f'a[href="/c/{session_id}"]'))


def test_click_pinned_project_session_keeps_project_collapsed(
    page: Page,
    seeded_session_pair: tuple[str, str, str],
) -> None:
    """Clicking a pinned session that belongs to a manually-collapsed project
    must NOT re-open the project folder.

    Setup:
    - Two sessions filed under the same project.
    - One of them is pinned (so it renders in "Pinned" too).
    - The project folder is expanded initially (auto-expand on file), so the
      test manually collapses it as the reporter did.
    - Navigate away, then click the pinned row.

    Assertion: the project header stays ``aria-expanded="false"`` and the
    non-pinned sibling row remains hidden. Before the fix, the auto-expand
    effect flipped the folder back open on every navigation into the pinned
    session.
    """
    base_url, session_a, session_b = seeded_session_pair
    project = f"e2e-2506-{uuid.uuid4().hex[:6]}"
    title_a = f"pinned-{uuid.uuid4().hex[:6]}"
    title_b = f"sibling-{uuid.uuid4().hex[:6]}"
    _set_title(base_url, session_a, title_a)
    _set_title(base_url, session_b, title_b)
    _file_into_project(base_url, session_a, project)
    _file_into_project(base_url, session_b, project)

    page.goto(f"{base_url}/c/{session_a}")

    row_a = _row(page, session_a)
    expect(row_a).to_be_visible()

    # Pin session A via the row's quick action. Hover first so the
    # hover-revealed button is interactable.
    row_a.hover()
    pin_button = row_a.get_by_test_id("quick-pin-conversation")
    expect(pin_button).to_have_attribute("aria-label", "Pin conversation")
    pin_button.click()

    # After pinning, the row now lives under "Pinned" (peeled out of its
    # project folder — the folder still holds session_b as its non-pinned
    # member).
    expect(_section(page, "Pinned").locator(f'a[href="/c/{session_a}"]')).to_be_visible()

    # The project folder auto-expanded when the label was applied; collapse
    # it manually — this is the state the reporter is in when they click the
    # pinned row.
    folder_header = page.get_by_role("button", name=project, exact=True)
    expect(folder_header).to_be_visible()
    if folder_header.get_attribute("aria-expanded") == "true":
        folder_header.click()
    expect(folder_header).to_have_attribute("aria-expanded", "false")
    # The sibling is not rendered while the folder is collapsed.
    expect(_section(page, project).locator(f'a[href="/c/{session_b}"]')).to_have_count(0)

    # Navigate away (New session button → "/"), then click back into the
    # pinned session. This is the specific interaction from the issue video:
    # the pinned session becomes active again, and the auto-expand effect
    # was firing on the resulting ``activeProjectName`` transition.
    page.get_by_role("link", name=re.compile(r"New session", re.IGNORECASE)).first.click()
    expect(page).to_have_url(re.compile(r"/$"))
    _section(page, "Pinned").locator(f'a[href="/c/{session_a}"]').click()
    expect(page).to_have_url(re.compile(rf"/c/{session_a}$"))

    # Regression assertion: the project folder must remain collapsed. Before
    # the fix, aria-expanded flipped back to "true" and the sibling row
    # reappeared.
    expect(folder_header).to_have_attribute("aria-expanded", "false")
    expect(_section(page, project).locator(f'a[href="/c/{session_b}"]')).to_have_count(0)
