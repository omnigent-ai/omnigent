"""Browser e2e: the sidebar's session filter survives a page reload.

The sidebar's session filter (the funnel: All / My sessions / Shared /
Archived) used to live only in React state, so every reload snapped the list
back to the default — a viewer who picked a different slice had to
re-pick it after each refresh. The pick is now persisted per-device
(``omnigent:session-filter`` in ``localStorage``) and seeded back on mount.

These drive the real chain the ``Sidebar`` unit tests mock out, end-to-end
against a live server: seed an owned session and a shared one over the REST
API, pick a filter through the live funnel, reload the browser, and assert
the list is still scoped and the radio item still reads checked.

Also covered is the read-side guard. "Shared sessions" is dropped from the
menu on a loopback-only server (one user, so the slice is meaningless), so a
value stored against a multi-user server must not be honored there — it would
scope the list to a slice with no menu entry to leave it by. That half runs
against the single-user ``live_server``; the persistence halves need the
multi-user server, since the shared filter is what makes a reload's scoping
observable (rows drop *out*, rather than merely staying put).
"""

from __future__ import annotations

import json
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
from tests.e2e_ui.conftest import _build_hello_world_bundle

# Edit access (2): enough to share a session with a non-owner. Mirrors
# LEVEL_EDIT in omnigent/server/auth.py.
_LEVEL_EDIT = 2

_FILTER = '[data-testid="session-filter"]'

# Where the sidebar persists the pick (see web/src/lib/sessionFilterPreferences.ts).
_STORAGE_KEY = "omnigent:session-filter"


def _create_session(server: MultiUserServer, *, owner_email: str, title: str) -> str:
    """Create a session owned by *owner_email* and give it a unique *title*.

    A multi-user header-auth server 401s headerless writes, so both the create
    and the title PATCH carry the owner's ``X-Forwarded-Email``. The title makes
    the row easy to spot among other tests' sessions on the shared server.
    """
    headers = {"X-Forwarded-Email": owner_email}
    create = httpx.post(
        f"{server.base_url}/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", _build_hello_world_bundle(), "application/gzip")},
        headers=headers,
        timeout=30.0,
    )
    create.raise_for_status()
    session_id = create.json()["session_id"]
    resp = httpx.patch(
        f"{server.base_url}/v1/sessions/{session_id}",
        json={"title": title},
        headers=headers,
        timeout=10.0,
    )
    resp.raise_for_status()
    return session_id


def _grant(server: MultiUserServer, session_id: str, user_id: str, level: int) -> None:
    """Grant *user_id* *level* on *session_id* (the admin owner acts)."""
    resp = httpx.put(
        f"{server.base_url}/v1/sessions/{session_id}/permissions",
        json={"user_id": user_id, "level": level},
        headers={"X-Forwarded-Email": ADMIN_EMAIL},
        timeout=30.0,
    )
    resp.raise_for_status()


def _row(page: Page, session_id: str) -> Locator:
    """The sidebar row (``<li>``) for *session_id*, located by its href."""
    return page.locator("li").filter(has=page.locator(f'a[href="/c/{session_id}"]'))


def _set_filter(page: Page, value: str) -> None:
    """Open the filter funnel and pick *value* (all/mine/shared/archived)."""
    page.locator(_FILTER).click()
    page.locator(f'[data-testid="session-filter-{value}"]').click()


def _expect_checked_filter(page: Page, value: str) -> None:
    """Open the funnel and assert *value* is the checked radio item."""
    page.locator(_FILTER).click()
    expect(page.locator(f'[data-testid="session-filter-{value}"]')).to_have_attribute(
        "aria-checked", "true", timeout=15_000
    )
    # Close the menu so a follow-up assertion isn't blocked by Radix's
    # aria-hidden on the rest of the tree.
    page.keyboard.press("Escape")


@pytest.fixture(scope="module")
def multi_user_server(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[MultiUserServer]:
    """A NON-single-user server so the Shared filter renders."""
    server_tmp = tmp_path_factory.mktemp("e2e_ui_session_filter_persistence")
    yield from spawn_multi_user_server(mock_llm_server_url, server_tmp)


@pytest.mark.flaky(reruns=2, reruns_delay=5)
def test_all_sessions_filter_survives_a_reload(
    browser: Browser,
    multi_user_server: MultiUserServer,
) -> None:
    """Picking "All sessions" still scopes the list after a full page reload.

    The default filter is "My sessions", which hides sessions shared to the
    viewer by others. Seeds a session the viewer owns and one the admin shared
    with them, picks "All sessions" (bringing the shared row in), then reloads.
    Without persistence the filter would snap back to the "My sessions" default
    on load, dropping the shared row again.

    "All" is a non-default slice, so this genuinely exercises persistence: a
    reload that landed on the default would fail here. Asserts both the visible
    effect (shared row returns and stays) and the menu state (the radio item
    reads checked), so a list that happened to look right for another reason
    can't pass.
    """
    server = multi_user_server
    uniq = uuid.uuid4().hex[:6]
    viewer_email = f"viewer-filter-{uniq}@ui.test"

    owned = _create_session(server, owner_email=viewer_email, title=f"e2e-filter-owned-{uniq}")
    shared = _create_session(server, owner_email=ADMIN_EMAIL, title=f"e2e-filter-shared-{uniq}")
    _grant(server, shared, viewer_email, _LEVEL_EDIT)

    ctx = browser.new_context(extra_http_headers={"X-Forwarded-Email": viewer_email})
    try:
        page = ctx.new_page()
        page.goto(f"{server.public_url}/c/{owned}")
        expect(page.locator(_FILTER)).to_be_visible(timeout=30_000)

        # Default "My sessions": the owned row shows and the shared one is
        # hidden, so picking "All" below is a real change rather than a no-op.
        expect(_row(page, owned)).to_be_visible(timeout=30_000)
        expect(_row(page, shared)).to_have_count(0, timeout=30_000)

        _set_filter(page, "all")
        expect(_row(page, shared)).to_be_visible(timeout=15_000)
        expect(_row(page, owned)).to_be_visible()

        # The reload the fix is about: a fresh document, so the sidebar must
        # re-seed the filter from storage rather than snap back to "My sessions".
        page.reload()
        expect(page.locator(_FILTER)).to_be_visible(timeout=30_000)
        expect(_row(page, owned)).to_be_visible(timeout=30_000)
        expect(_row(page, shared)).to_be_visible(timeout=30_000)
        _expect_checked_filter(page, "all")
    finally:
        ctx.close()


@pytest.mark.flaky(reruns=2, reruns_delay=5)
def test_shared_sessions_filter_survives_a_reload(
    browser: Browser,
    multi_user_server: MultiUserServer,
) -> None:
    """The Shared filter also persists — the write isn't special-cased to "mine".

    The persistence hangs off the sidebar's single tab-change funnel, so every
    option should round-trip. This picks the opposite slice from the test above
    and reloads: the shared row stays, the owned one stays out.
    """
    server = multi_user_server
    uniq = uuid.uuid4().hex[:6]
    viewer_email = f"viewer-shared-{uniq}@ui.test"

    owned = _create_session(server, owner_email=viewer_email, title=f"e2e-shared-owned-{uniq}")
    shared = _create_session(server, owner_email=ADMIN_EMAIL, title=f"e2e-shared-shared-{uniq}")
    _grant(server, shared, viewer_email, _LEVEL_EDIT)

    ctx = browser.new_context(extra_http_headers={"X-Forwarded-Email": viewer_email})
    try:
        page = ctx.new_page()
        page.goto(f"{server.public_url}/c/{owned}")
        expect(page.locator(_FILTER)).to_be_visible(timeout=30_000)
        # Default "My sessions" hides the shared row; picking "Shared" brings it
        # in and drops the owned one.
        _set_filter(page, "shared")
        expect(_row(page, shared)).to_be_visible(timeout=15_000)
        expect(_row(page, owned)).to_have_count(0, timeout=15_000)

        page.reload()
        expect(page.locator(_FILTER)).to_be_visible(timeout=30_000)
        expect(_row(page, shared)).to_be_visible(timeout=30_000)
        expect(_row(page, owned)).to_have_count(0)
        _expect_checked_filter(page, "shared")
    finally:
        ctx.close()


@pytest.mark.flaky(reruns=2, reruns_delay=5)
def test_stored_shared_filter_is_dropped_on_a_single_user_server(
    page: Page,
    live_server: str,
    seeded_session: tuple[str, str],
) -> None:
    """A persisted "shared" doesn't strand the viewer on a single-user server.

    ``live_server`` is loopback-only, so the funnel drops "Shared sessions" —
    there's one user and the slice is meaningless. Seeding the stored value
    that a multi-user server would have written (or a hand-edited entry) must
    therefore fall back to the "My sessions" default: honoring it would scope
    the list to a slice with no menu entry to leave it by, i.e. an empty list
    the viewer can't escape.

    Uses ``add_init_script`` so the value is in storage *before* any app script
    runs, which is what a returning viewer's first paint actually sees.
    """
    _base_url, session_id = seeded_session
    page.add_init_script(f"localStorage.setItem({_STORAGE_KEY!r}, 'shared');")

    page.goto(f"{live_server}/c/{session_id}")
    expect(page.locator(_FILTER)).to_be_visible(timeout=30_000)

    # The viewer's own session renders (an honored "shared" would hide it), and
    # the menu falls back to the "My sessions" default with no Shared option to
    # have selected.
    expect(_row(page, session_id)).to_be_visible(timeout=30_000)
    page.locator(_FILTER).click()
    expect(page.locator('[data-testid="session-filter-mine"]')).to_have_attribute(
        "aria-checked", "true", timeout=15_000
    )
    expect(page.locator('[data-testid="session-filter-shared"]')).to_have_count(0)
