"""Browser e2e: a project rename must reach other connected clients live.

Renaming a project folder in one client (sidebar folder context menu ->
"Rename project" -> ``PATCH /v1/projects/{id}``) updates that client through
its own mutation's cache invalidation, but nothing pushes the change to OTHER
connected clients: the sidebar's folder names come from
``GET /v1/sessions/projects`` (react-query key ``["projects"]``, no refetch
interval, no window-focus refetch), and the live lane
``WS /v1/sessions/updates`` streams session rows only (``project_id``, never
project names). So a second open client -- another web tab, or the iOS app's
WebView showing the same SPA -- keeps displaying the old folder name until a
full reload.

These tests drive two real browser contexts against the live server: client B
creates and then renames a project through the real sidebar UI, and the
assertion is that client A (already connected, no reload) observes the new
folder name within a live-update window. The window mirrors the sessions-list
analog in ``test_session_updates_stream.py``, which asserts a *session* rename
reaches other open tabs in well under 20 s. A failure here means project
changes are not broadcast (or otherwise refreshed) across clients.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

from playwright.sync_api import Browser, Locator, Page, ViewportSize, expect

# Live-delivery window: generous for any push/refresh path, far below a
# user-initiated reload. The sessions-updates stream delivers session renames
# to other tabs in a few seconds; a project rename should arrive on the same
# order once it is broadcast at all.
_LIVE_UPDATE_TIMEOUT_MS = 20_000

# iPhone-sized viewport: the iOS app is a thin native shell rendering this
# same SPA in a WebView, so a phone-profile context stands in for it.
_MOBILE_VIEWPORT: ViewportSize = {"width": 390, "height": 844}


def _context_kwargs() -> dict[str, Any]:
    """Video-record kwargs for manually created contexts.

    The conftest's ``OMNIGENT_E2E_RECORD_DIR`` auto-injection patches the
    async API only; these tests open sync contexts by hand, so they honor the
    same env var explicitly to keep the journey filmable.
    """
    record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
    return {"record_video_dir": record_dir} if record_dir else {}


def _folder_header(page: Page, project: str) -> Locator:
    """Locate a project folder's collapse-header button by its visible name."""
    return page.locator('button[data-slot="context-menu-trigger"]').filter(has_text=project)


def _create_project(page: Page, name: str) -> None:
    """Create an empty project from the Projects group header's + action."""
    # Fine-pointer layouts reveal this control only while its header is hovered.
    page.get_by_role("button", name="Projects", exact=True).hover()
    page.get_by_test_id("new-project").click()
    page.get_by_placeholder("Project name…").fill(name)
    page.get_by_test_id("new-project-confirm").click()
    expect(page.get_by_test_id("new-project-confirm")).to_have_count(0)
    expect(_folder_header(page, name)).to_be_visible()


def _rename_project(page: Page, old: str, new: str) -> None:
    """Rename a project via the folder context menu's "Rename project" dialog.

    Asserts the renaming client itself shows the new name afterwards -- that
    half works via the local mutation's invalidation and is the sanity check,
    not the cross-client delivery under test.
    """
    _folder_header(page, old).click(button="right")
    page.get_by_test_id("rename-project").click()
    dialog = page.get_by_role("dialog")
    expect(dialog).to_be_visible()
    # The dialog's single text input holds the current name; replace it.
    dialog.locator("input").fill(new)
    page.get_by_test_id("rename-project-confirm").click()
    expect(page.get_by_test_id("rename-project-confirm")).to_have_count(0)
    expect(_folder_header(page, new)).to_be_visible()


def _bring_into_view(header: Locator) -> None:
    """Scroll the folder header to the middle of the sidebar viewport."""
    header.evaluate("el => el.scrollIntoView({ block: 'center', behavior: 'instant' })")
    expect(header).to_be_in_viewport()


def test_project_rename_reaches_other_open_web_client(
    browser: Browser,
    seeded_session: tuple[str, str],
) -> None:
    """A project rename in one web client appears live in another open tab.

    Client B creates a project and renames it through the sidebar UI. Client
    A, already showing that folder, must display the new name within the
    live-update window without reloading. A timeout here means the rename was
    persisted (client B shows it) but never delivered to other clients.
    """
    base_url, session_id = seeded_session
    old = f"Repro {uuid.uuid4().hex[:6]}"
    new = f"Renamed {uuid.uuid4().hex[:6]}"

    renamer_ctx = browser.new_context(**_context_kwargs())
    observer_ctx = browser.new_context(**_context_kwargs())
    try:
        renamer = renamer_ctx.new_page()
        renamer.goto(f"{base_url}/c/{session_id}")
        _create_project(renamer, old)

        # The observer connects AFTER the project exists so its initial
        # projects fetch renders the folder under its old name; any later
        # change on its screen must therefore be live-delivered.
        observer = observer_ctx.new_page()
        observer.goto(f"{base_url}/c/{session_id}")
        old_header = _folder_header(observer, old)
        expect(old_header).to_be_visible()
        _bring_into_view(old_header)

        _rename_project(renamer, old, new)

        # KEY ASSERTION: the already-open client shows the renamed folder
        # within the live window, with no reload. Nothing currently pushes
        # project changes to other clients, so this times out with the stale
        # name still on screen.
        expect(_folder_header(observer, new)).to_be_visible(timeout=_LIVE_UPDATE_TIMEOUT_MS)
        expect(_folder_header(observer, old)).to_have_count(0)
    finally:
        observer_ctx.close()
        renamer_ctx.close()


def test_project_rename_reaches_mobile_viewport_client(
    browser: Browser,
    seeded_session: tuple[str, str],
) -> None:
    """A project rename made on the web reaches a connected phone client.

    The reported journey: the iOS app (this SPA in a WebView at a phone
    viewport) has the sidebar open showing the project folder; the project is
    renamed in the web UI on another device; the phone must show the new name
    without an app reload. A timeout here reproduces the report -- the phone
    keeps the old name until the app reloads the SPA.
    """
    base_url, session_id = seeded_session
    old = f"Repro {uuid.uuid4().hex[:6]}"
    new = f"Renamed {uuid.uuid4().hex[:6]}"

    web_ctx = browser.new_context(**_context_kwargs())
    phone_ctx = browser.new_context(
        has_touch=True,
        viewport=_MOBILE_VIEWPORT,
        **_context_kwargs(),
    )
    try:
        web = web_ctx.new_page()
        web.goto(f"{base_url}/c/{session_id}")
        _create_project(web, old)

        # Phone client connects with the sidebar drawer open and the folder
        # visible under its old name.
        phone = phone_ctx.new_page()
        phone.goto(f"{base_url}/c/{session_id}?sidebar=open")
        old_header = _folder_header(phone, old)
        expect(old_header).to_be_visible()
        _bring_into_view(old_header)

        # The rename happens on the desktop web UI -- another device entirely
        # from the phone's point of view.
        _rename_project(web, old, new)

        # KEY ASSERTION: the phone shows the renamed folder live. Today the
        # rename is never broadcast, so the drawer keeps the stale name until
        # the app is reloaded and this times out.
        expect(_folder_header(phone, new)).to_be_visible(timeout=_LIVE_UPDATE_TIMEOUT_MS)
        expect(_folder_header(phone, old)).to_have_count(0)
    finally:
        phone_ctx.close()
        web_ctx.close()
