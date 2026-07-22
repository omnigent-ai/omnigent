"""Browser e2e for the sidebar's owner-vs-not row gating and tab placement.

This is the end-to-end companion to the mocked ``Sidebar`` unit tests, and the
coverage the ``E2E UI Required`` gate asks for: the sidebar derives ownership
(and every owner-only row action) from the session's ``owner`` — the creator's
user id carried on each list row — NOT from an effective ``permission_level``.
The behavior under test:

- A session's **owner** sees it under the **"My sessions"** tab, with the
  kebab's **Rename** and **Share** items **enabled**.
- A **non-owner the session is shared with** (even at EDIT) sees it under the
  **"Shared with me"** tab, with **Rename** and **Share** **disabled**
  ("Only the session owner can …"). Editing shared *content* still happens in
  the open session — that path reads the real level from the single-session
  snapshot and is unaffected — but the sidebar affordances are owner-only.

Runs against a dedicated multi-user server: the shared ``live_server`` is
single-user (``OMNIGENT_LOCAL_SINGLE_USER=1``), which hides the My/Shared tabs
AND the Share item entirely, so the ownership split can't be observed there.
The multi-user server clears that marker and declares an admin identity, exactly
like a Databricks Apps / SSO-proxy install — the deployment shape this gating
matters for.

The admin owns the session; a second header identity (the viewer) is granted
access via the REST API. Both identities' sidebar list loads via the initial
``GET /v1/sessions`` (a plain authenticatedFetch that carries the context's
``X-Forwarded-Email``), so the static post-load state asserted here is
deterministic — unlike the ``WS /v1/sessions/updates`` push, which the
sharing-journey test notes may not carry the header in all Chromium combos and
which this test deliberately does not depend on.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Browser, Locator, Page, expect

from tests.e2e_ui.collaboration._multi_user_server import (
    ADMIN_EMAIL,
    MultiUserServer,
    spawn_multi_user_server,
)

# Edit access (2) is the interesting non-owner case: it proves the sidebar gates
# on ownership, not level — an EDIT holder still can't rename/share from the
# sidebar. Mirrors LEVEL_EDIT in omnigent/server/auth.py.
_LEVEL_EDIT = 2

_TAB_MINE = '[data-testid="sidebar-tab-mine"]'
_TAB_SHARED = '[data-testid="sidebar-tab-shared"]'


@pytest.fixture(scope="module")
def multi_user_server(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[MultiUserServer]:
    """A NON-single-user server so the My/Shared tabs + Share item render."""
    server_tmp = tmp_path_factory.mktemp("e2e_ui_sidebar_ownership_multi_user")
    yield from spawn_multi_user_server(mock_llm_server_url, server_tmp)


def _grant(server: MultiUserServer, user_id: str, level: int) -> None:
    """Grant *user_id* *level* on the admin-owned session (admin acts)."""
    resp = httpx.put(
        f"{server.base_url}/v1/sessions/{server.session_id}/permissions",
        json={"user_id": user_id, "level": level},
        headers={"X-Forwarded-Email": ADMIN_EMAIL},
        timeout=30.0,
    )
    resp.raise_for_status()


def _row(page: Page, session_id: str) -> Locator:
    """The sidebar row (``<li>``) for *session_id*, located by its href."""
    return page.locator("li").filter(has=page.locator(f'a[href="/c/{session_id}"]'))


def _open_row_menu(page: Page, session_id: str) -> None:
    """Right-click the row to open the shared actions menu at the cursor.

    Right-click (Radix ``ContextMenu``) renders the same
    ``ConversationMenuItems`` body as the kebab, so the item testids and their
    enabled/disabled state are identical — and it avoids the kebab's
    pointer-event timing. Mirrors ``test_sidebar_context_menu.py``.
    """
    link = page.locator(f'a[href="/c/{session_id}"]')
    expect(link).to_be_visible(timeout=30_000)
    link.click(button="right")


def test_owner_sees_session_under_my_sessions_with_enabled_actions(
    browser: Browser,
    multi_user_server: MultiUserServer,
) -> None:
    """The owner's own session: "My sessions" tab, Rename + Share enabled.

    The baseline half of the ownership split — nothing about owner affordances
    regressed when the sidebar moved off ``permission_level``.
    """
    server = multi_user_server
    sid = server.session_id
    ctx = browser.new_context(extra_http_headers={"X-Forwarded-Email": ADMIN_EMAIL})
    try:
        page = ctx.new_page()
        page.goto(f"{server.public_url}/c/{sid}")

        # Owned → shows under the default "My sessions" tab, never "Shared".
        expect(page.locator(_TAB_MINE)).to_be_visible(timeout=30_000)
        expect(_row(page, sid)).to_be_visible(timeout=30_000)

        _open_row_menu(page, sid)
        # Owner → Rename and Share are enabled (no data-disabled marker).
        expect(page.get_by_test_id("rename-conversation")).not_to_have_attribute(
            "data-disabled", re.compile(r".*")
        )
        expect(page.get_by_test_id("share-conversation")).not_to_have_attribute(
            "data-disabled", re.compile(r".*")
        )

        # Rename runs the real inline-edit path from here (proves "enabled" is
        # not just cosmetic), exactly as the context-menu test asserts.
        page.get_by_test_id("rename-conversation").click()
        expect(page.get_by_test_id("rename-conversation-input")).to_be_visible(timeout=15_000)
    finally:
        ctx.close()


@pytest.mark.flaky(reruns=2, reruns_delay=5)
def test_shared_viewer_sees_session_under_shared_tab_with_owner_only_actions(
    browser: Browser,
    multi_user_server: MultiUserServer,
) -> None:
    """A non-owner (granted EDIT): "Shared with me" tab, Rename + Share disabled.

    The behavior this PR introduces: an EDIT grant is enough to open and edit
    the session, but the sidebar's Rename/Share are owner-only — so a shared
    session lands on the "Shared with me" tab (never "My sessions"), and its
    kebab Rename/Share are disabled regardless of the granted level.
    """
    server = multi_user_server
    sid = server.session_id
    viewer_email = f"viewer-{uuid.uuid4().hex[:6]}@ui.test"
    _grant(server, viewer_email, _LEVEL_EDIT)

    ctx = browser.new_context(extra_http_headers={"X-Forwarded-Email": viewer_email})
    try:
        page = ctx.new_page()
        page.goto(f"{server.public_url}/c/{sid}")

        # The shared session is NOT the viewer's own, so it must not appear on
        # the default "My sessions" tab...
        expect(page.locator(_TAB_MINE)).to_be_visible(timeout=30_000)
        expect(_row(page, sid)).to_have_count(0)

        # ...it lives under "Shared with me".
        page.locator(_TAB_SHARED).click()
        expect(_row(page, sid)).to_be_visible(timeout=30_000)

        _open_row_menu(page, sid)
        # Non-owner → Rename and Share are disabled even though the viewer holds
        # EDIT. This is the owner-only gating (was edit-/manage-gated before).
        expect(page.get_by_test_id("rename-conversation")).to_have_attribute(
            "data-disabled", re.compile(r".*"), timeout=15_000
        )
        expect(page.get_by_test_id("share-conversation")).to_have_attribute(
            "data-disabled", re.compile(r".*")
        )
    finally:
        ctx.close()
