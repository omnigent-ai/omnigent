"""Deep browser journeys for session unread/read entry points.

These tests deliberately exercise the real list cache, route lifecycle, and
per-viewer read-state API instead of only asserting a colored badge. The
existing mark-unread persistence tests cover localStorage/replica fallback;
this file covers the remaining user entry points and cross-session behavior.
"""

from __future__ import annotations

import re
import time
import uuid
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import Locator, Page, Response, expect

from tests.e2e_ui.sessions.unread_helpers import append_assistant_message


def _row(page: Page, session_id: str) -> Locator:
    """Locate a session row in normal or mobile sidebar layouts."""
    return page.locator("li").filter(
        has=page.locator(f'a[href^="/c/{session_id}"]'),
    )


def _dot(row: Locator) -> Locator:
    return row.locator('[data-testid="session-state-badge"][data-state="unseen"]')


def _link_by_title(page: Page, title: str) -> Locator:
    # Selection mode changes the href to # and unread rows add an sr-only suffix.
    return page.get_by_role(
        "link",
        name=re.compile(rf"^{re.escape(title)}(?:\s+\(unread\))?$"),
    ).first


def _set_title(page: Page, base_url: str, session_id: str, title: str) -> None:
    response = page.request.patch(
        f"{base_url}/v1/sessions/{session_id}",
        data={"title": title},
    )
    assert response.ok, response.text()


def _viewer_state(page: Page, base_url: str, session_id: str) -> dict[str, object]:
    response = page.request.get(
        f"{base_url}/v1/sessions?limit=200&order=desc&sort_by=updated_at&visibility=all"
    )
    assert response.ok, response.text()
    rows = response.json().get("data", [])
    row = next((candidate for candidate in rows if candidate.get("id") == session_id), None)
    assert row is not None, f"session {session_id} missing from session list"
    return row


def _wait_viewer_unread(page: Page, base_url: str, session_id: str, expected: bool) -> None:
    deadline = time.monotonic() + 10
    last: object = None
    while time.monotonic() < deadline:
        last = _viewer_state(page, base_url, session_id).get("viewer_unread")
        if last is expected:
            return
        time.sleep(0.2)
    raise AssertionError(f"viewer_unread should become {expected!r}, got {last!r}")


def _mine_sessions_response(response: Response) -> bool:
    """Match the unpinned Inbox sidebar list projection response."""
    parsed = urlparse(response.url)
    query = parse_qs(parsed.query)
    return (
        parsed.path == "/v1/sessions"
        and response.request.method == "GET"
        and query.get("visibility") == ["mine"]
        and "pinned" not in query
        and response.status == 200
    )


def _mark_unread_from_header(page: Page) -> None:
    page.get_by_test_id("header-conversation-actions").click()
    page.get_by_test_id("header-mark-unread-conversation").click()


def _mark_unread_from_row(page: Page, row: Locator) -> None:
    row.hover()
    row.get_by_test_id("conversation-actions").click()
    page.get_by_test_id("mark-unread-conversation").click()


def _create_project(page: Page, name: str) -> None:
    page.get_by_role("button", name="Projects", exact=True).hover()
    page.get_by_test_id("new-project").click()
    page.get_by_placeholder("Project name…").fill(name)
    page.get_by_test_id("new-project-confirm").click()
    expect(page.get_by_test_id("new-project-confirm")).to_have_count(0)


def _move_to_project(page: Page, row: Locator, project: str) -> None:
    row.hover()
    row.get_by_test_id("conversation-actions").click()
    page.get_by_test_id("move-to-project").click()
    page.get_by_role("menuitem", name=project, exact=True).click()


def _project_header(page: Page, project: str) -> Locator:
    return page.locator('button[data-slot="context-menu-trigger"]').filter(has_text=project).first


def test_header_mark_unread_syncs_the_viewer_state(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")
    row = _row(page, session_id)
    expect(row).to_be_visible()

    _mark_unread_from_header(page)

    expect(_dot(row)).to_be_visible()
    _wait_viewer_unread(page, base_url, session_id, True)


def test_row_mark_read_clears_the_dot_and_syncs_the_viewer_state(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    base_url, session_id = seeded_session
    with page.expect_response(_mine_sessions_response, timeout=30_000):
        page.goto(f"{base_url}/c/{session_id}")
    row = _row(page, session_id)
    row.hover()
    row.get_by_test_id("conversation-actions").click()
    with page.expect_response(
        lambda response: (
            response.request.method == "PUT"
            and response.url.endswith(f"/v1/sessions/{session_id}/read-state")
            and response.request.post_data_json["unread"] is True
            and response.status == 204
        ),
        timeout=30_000,
    ):
        page.get_by_test_id("mark-unread-conversation").click()
    expect(_dot(row)).to_be_visible()

    # Read the same row from the inactive Inbox route; this keeps the row-menu
    # entry point distinct from the active-header action and avoids menu focus
    # racing the active-row read lifecycle.
    page.get_by_test_id("inbox-button").click()
    expect(page).to_have_url(f"{base_url}/inbox")
    # The URL commits before AppShell replaces the active ChatPage tree. Wait
    # for the rendered Inbox route before interacting with the sidebar again.
    expect(page.get_by_role("heading", name="Inbox", exact=True)).to_be_visible(
        timeout=30_000,
    )
    row = _row(page, session_id)
    row.hover()
    row.get_by_test_id("conversation-actions").click()
    expect(page.get_by_test_id("mark-read-conversation")).to_be_visible()
    with page.expect_response(
        lambda response: (
            response.request.method == "PUT"
            and response.url.endswith(f"/v1/sessions/{session_id}/read-state")
            and response.request.post_data_json["unread"] is False
            and response.status == 204
        ),
        timeout=30_000,
    ):
        page.get_by_test_id("mark-read-conversation").click()

    expect(_dot(row)).to_have_count(0)
    _wait_viewer_unread(page, base_url, session_id, False)


def test_context_menu_mark_read_round_trip(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")
    row = _row(page, session_id)
    link = row.locator(f'a[href^="/c/{session_id}"]')

    link.click(button="right")
    page.get_by_test_id("mark-unread-conversation").click()
    expect(_dot(row)).to_be_visible()

    link.click(button="right")
    page.get_by_test_id("mark-read-conversation").click()
    expect(_dot(row)).to_have_count(0)
    _wait_viewer_unread(page, base_url, session_id, False)


def test_bulk_mark_read_isolated_to_the_selected_session(
    page: Page,
    seeded_session_pair: tuple[str, str, str],
) -> None:
    base_url, session_a, session_b = seeded_session_pair
    title_a = f"unread-a-{uuid.uuid4().hex[:8]}"
    title_b = f"unread-b-{uuid.uuid4().hex[:8]}"
    _set_title(page, base_url, session_a, title_a)
    _set_title(page, base_url, session_b, title_b)
    page.goto(f"{base_url}/c/{session_a}")

    expect(_link_by_title(page, title_a)).to_be_visible()
    expect(_link_by_title(page, title_b)).to_be_visible()
    page.get_by_test_id("toggle-selection-mode").click()
    _link_by_title(page, title_a).click()
    _link_by_title(page, title_b).click()
    page.get_by_test_id("bulk-mark-unread").click()

    expect(_dot(_row(page, session_a))).to_be_visible()
    expect(_dot(_row(page, session_b))).to_be_visible()
    _wait_viewer_unread(page, base_url, session_a, True)
    _wait_viewer_unread(page, base_url, session_b, True)

    page.get_by_test_id("toggle-selection-mode").click()
    _link_by_title(page, title_a).click()
    page.get_by_test_id("bulk-mark-read").click()

    expect(_dot(_row(page, session_a))).to_have_count(0)
    expect(_dot(_row(page, session_b))).to_be_visible()
    _wait_viewer_unread(page, base_url, session_a, False)
    _wait_viewer_unread(page, base_url, session_b, True)


def test_collapsed_project_marker_clears_after_command_palette_reopen(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A collapsed folder marker follows a read in an off-row route."""
    base_url, session_id = seeded_session
    title = f"project-unread-{uuid.uuid4().hex[:8]}"
    project = f"Unread project {uuid.uuid4().hex[:8]}"
    _set_title(page, base_url, session_id, title)
    page.goto(f"{base_url}/c/{session_id}")
    _create_project(page, project)
    _move_to_project(page, _row(page, session_id), project)

    project_header = _project_header(page, project)
    row = _row(page, session_id)
    _mark_unread_from_row(page, row)
    expect(_dot(row)).to_be_visible()
    project_header.click()
    expect(project_header).to_have_attribute("aria-expanded", "false")
    expect(project_header.locator('[data-state="unseen"]')).to_be_visible()

    # Leave the chat while the project remains collapsed, then search back to
    # it. AppShell's active-view mark should update the hidden marker.
    page.get_by_test_id("inbox-button").click()
    expect(page).to_have_url(f"{base_url}/inbox")
    page.get_by_test_id("sidebar-search-button").click()
    palette = page.get_by_test_id("command-palette-input")
    palette.fill(title)
    page.get_by_role("option", name=re.compile(re.escape(title))).click()
    expect(page).to_have_url(re.compile(rf"/c/{re.escape(session_id)}$"))
    expect(project_header.locator('[data-state="unseen"]')).to_have_count(0)


def test_collapsed_project_marker_follows_focus_read_without_route_change(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A same-route focus read clears a newly arrived visible message marker."""
    base_url, session_id = seeded_session
    title = f"project-focus-{uuid.uuid4().hex[:8]}"
    project = f"Focus project {uuid.uuid4().hex[:8]}"
    _set_title(page, base_url, session_id, title)
    page.goto(f"{base_url}/c/{session_id}")
    _create_project(page, project)
    _move_to_project(page, _row(page, session_id), project)

    project_header = _project_header(page, project)
    project_header.click()
    expect(project_header).to_have_attribute("aria-expanded", "false")
    expect(project_header.locator('[data-state="unseen"]')).to_have_count(0)

    # Browser emulation of the focus source used by the native-shell path.
    page.evaluate("window.dispatchEvent(new Event('blur'))")
    page.wait_for_timeout(2_100)
    append_assistant_message(
        page,
        base_url,
        session_id,
        "Visible content for collapsed marker coverage.",
    )
    expect(project_header.locator('[data-state="unseen"]')).to_be_visible(timeout=15_000)

    route_before_focus = page.url
    page.evaluate("window.dispatchEvent(new Event('focus'))")
    expect(page).to_have_url(route_before_focus)
    expect(project_header.locator('[data-state="unseen"]')).to_have_count(0)


def test_settings_back_browser_back_and_reload_have_distinct_read_lifecycle(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")

    _mark_unread_from_header(page)
    page.get_by_test_id("settings-button").click()
    page.wait_for_url("**/settings/**")
    page.get_by_role("link", name="Back", exact=True).click()
    expect(page).to_have_url(f"{base_url}/c/{session_id}")
    expect(_dot(_row(page, session_id))).to_have_count(0)

    _mark_unread_from_header(page)
    page.get_by_test_id("inbox-button").click()
    expect(page).to_have_url(f"{base_url}/inbox")
    expect(page.get_by_role("heading", name="Inbox")).to_be_visible()
    page.go_back()
    expect(page).to_have_url(f"{base_url}/c/{session_id}")
    expect(_dot(_row(page, session_id))).to_have_count(0)

    _mark_unread_from_header(page)
    expect(_dot(_row(page, session_id))).to_be_visible()
    page.reload()
    expect(_dot(_row(page, session_id))).to_be_visible()


def test_mobile_session_menu_marks_unread(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    base_url, session_id = seeded_session
    page.set_viewport_size({"width": 390, "height": 780})
    page.goto(f"{base_url}/c/{session_id}")
    page.get_by_test_id("header-conversation-actions").click()
    page.get_by_test_id("header-mark-unread-conversation").click()

    # The mobile drawer's scrim intentionally sits above the chat header. Open
    # it after the header action to verify the row's visible marker.
    page.goto(f"{base_url}/c/{session_id}?sidebar=open")
    row = _row(page, session_id)
    expect(row).to_be_visible()
    expect(_dot(row)).to_be_visible()
    _wait_viewer_unread(page, base_url, session_id, True)


def test_focus_event_reads_active_session_when_has_focus_misreports(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    base_url, session_id = seeded_session
    page.add_init_script(
        "Object.defineProperty(Document.prototype, 'hasFocus', "
        "{ configurable: true, value: () => false });"
    )
    page.goto(f"{base_url}/c/{session_id}")
    expect(_row(page, session_id)).to_be_visible()

    # This is browser emulation of the native-shell quirk, not a claim about
    # real Electron. The DOM focus event is the deterministic signal under test.
    page.evaluate("window.dispatchEvent(new Event('focus'))")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        state = _viewer_state(page, base_url, session_id)
        if isinstance(state.get("viewer_last_seen"), int):
            return
        time.sleep(0.2)
    raise AssertionError("focus event should persist a viewer_last_seen baseline")
