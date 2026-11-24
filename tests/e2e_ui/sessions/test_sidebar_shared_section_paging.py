"""Browser E2E: "Shared with me" lists sessions older than the viewer's loaded pages.

On Display → All, sessions shared with the viewer get their own "Shared with me"
section above Sessions. The viewer's own sessions and the shared ones load in
separate pages, so a shared session that is older than every own session the
sidebar has loaded must still be listed in its section, without the viewer
scrolling their own list to its end first.

Runs against a dedicated multi-user server: the suite's ``live_server`` is
single-user, which hides sharing entirely.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from playwright.sync_api import Browser, Locator, Page, expect

from tests._helpers.browser import user_page
from tests._helpers.polling import poll_until
from tests.e2e_ui.collaboration._multi_user_server import (
    ADMIN_EMAIL,
    MultiUserServer,
    create_user_session,
    spawn_multi_user_server,
)
from tests.e2e_ui.sessions._sidebar_helpers import choose_session_filter

# Read access (1) is the least a grant gives. Mirrors LEVEL_READ in
# omnigent/server/auth.py.
_LEVEL_READ = 1

# The sidebar requests 30 sessions per page (``sessionPageSize`` in
# web/src/appConfig.ts) and loads another by itself only while its list end is
# near the viewport, so four full pages plus one keep the viewer's own list
# unfinished until they scroll for more.
_PAGE_SIZE = 30
_OWN_SESSIONS = 4 * _PAGE_SIZE + 1

_ADMIN_HEADERS = {"X-Forwarded-Email": ADMIN_EMAIL}
_FILTER = '[data-testid="session-filter"]'


@pytest.fixture(scope="module")
def multi_user_server(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[MultiUserServer]:
    """A NON-single-user server so the Shared with me section can render."""
    server_tmp = tmp_path_factory.mktemp("e2e_ui_sidebar_shared_section_paging")
    yield from spawn_multi_user_server(mock_llm_server_url, server_tmp)


def _list_sessions(
    server: MultiUserServer, viewer_email: str, visibility: str, limit: int
) -> list[dict[str, Any]]:
    """The viewer's *visibility* sessions, ordered as the sidebar requests them."""
    response = httpx.get(
        f"{server.base_url}/v1/sessions",
        params={
            "visibility": visibility,
            "limit": limit,
            "order": "desc",
            "sort_by": "updated_at",
        },
        headers={"X-Forwarded-Email": viewer_email},
        timeout=30.0,
    )
    response.raise_for_status()
    return response.json()["data"]


def _section(page: Page, title: str) -> Locator:
    """The sidebar ``<section>`` whose collapse-header button reads *title*."""
    return page.locator("section").filter(has=page.get_by_role("button", name=title, exact=True))


def test_shared_section_lists_a_session_older_than_the_loaded_own_pages(
    browser: Browser,
    multi_user_server: MultiUserServer,
) -> None:
    """A shared session behind pages of newer own sessions stays in its section."""
    server = multi_user_server
    uniq = uuid.uuid4().hex[:6]
    viewer_email = f"viewer-shared-paging-{uniq}@ui.test"
    viewer_headers = {"X-Forwarded-Email": viewer_email}

    shared = create_user_session(
        server.base_url,
        headers=_ADMIN_HEADERS,
        metadata={"title": f"e2e-shared-paging-shared-{uniq}"},
    )
    grant = httpx.put(
        f"{server.base_url}/v1/sessions/{shared}/permissions",
        json={"user_id": viewer_email, "level": _LEVEL_READ},
        headers=_ADMIN_HEADERS,
        timeout=30.0,
    )
    grant.raise_for_status()
    shared_view = httpx.get(
        f"{server.base_url}/v1/sessions/{shared}", headers=_ADMIN_HEADERS, timeout=30.0
    )
    shared_view.raise_for_status()
    # Timestamps have one-second resolution: start the viewer's own sessions in a
    # later second, so all of them sort ahead of the shared one.
    shared_updated_at = shared_view.json()["updated_at"]
    poll_until(lambda: time.time() >= shared_updated_at + 1, timeout_s=5, interval_s=0.05)
    for index in range(_OWN_SESSIONS):
        create_user_session(
            server.base_url,
            headers=viewer_headers,
            metadata={"title": f"e2e-shared-paging-own-{index:03d}-{uniq}"},
        )

    # The server confirms the setup: every own session is newer than the shared one.
    own_rows = _list_sessions(server, viewer_email, "mine", limit=_OWN_SESSIONS + 1)
    shared_rows = _list_sessions(server, viewer_email, "shared", limit=_PAGE_SIZE)
    assert [row["id"] for row in shared_rows] == [shared]
    assert len(own_rows) == _OWN_SESSIONS
    assert min(row["updated_at"] for row in own_rows) > shared_rows[0]["updated_at"]

    with user_page(browser, headers=viewer_headers) as page:
        # Land on the home page: with no session open, the shared row can only
        # come from the list, and no open row scrolls the list toward its end.
        page.goto(f"{server.public_url}/")
        expect(page.locator(_FILTER)).to_be_visible(timeout=30_000)
        choose_session_filter(page, "all")

        sessions = _section(page, "Sessions")
        # The All list loaded the viewer's newest own session under Sessions...
        expect(sessions.locator(f'a[href="/c/{own_rows[0]["id"]}"]')).to_be_visible(timeout=30_000)
        # ...and lists the older shared session in its own section right away.
        expect(_section(page, "Shared with me").locator(f'a[href="/c/{shared}"]')).to_be_visible(
            timeout=15_000
        )
        # The viewer's own list is still unfinished: not every own session is listed.
        assert sessions.locator('a[href^="/c/"]').count() < _OWN_SESSIONS
