"""Browser e2e: the session filter re-scopes only the flat Sessions list.

The sidebar's session filter (the funnel: All / My sessions / Shared / Archived)
picks a slice of conversations, but the **Pinned** and **Projects** sections are
built from the full non-archived set — NOT the filtered slice — so switching the
filter must never empty them:

- **Pinned** shows EVERY pinned session on every filter. A pinned *shared*
  session (owned by someone else) stays under Pinned on "My sessions", and a
  pinned *owned* session stays under Pinned on "Shared sessions"; both survive
  the "Archived sessions" filter too. Pins are per-user, so the viewer's own pin
  set drives this regardless of who owns the session.
- **Projects** renders the same folders and contents on every filter. Filing is
  owner-only, so a folder shows the viewer's owned sessions — but the group and
  its folders must not vanish when the filter switches to Shared or Archived.

These drive the real chain the mocked ``Sidebar`` unit tests exercise, but
end-to-end: seed owned + shared sessions (and a filed project) over the REST
API, pin/file through the live UI, toggle the filter, and assert the Pinned /
Projects sections hold their rows. Runs against a dedicated multi-user server
(the shared ``live_server`` is single-user, which hides the Shared filter), the
same harness as ``test_sidebar_ownership_gating.py``.
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

# Reserved label key storing project membership (see ``list_projects`` /
# ``web/src/lib/sessionListCache.ts``). Filing via the label exercises the
# dual-read the sidebar's Projects group relies on.
_PROJECT_LABEL_KEY = "omni_project"

_FILTER = '[data-testid="session-filter"]'


@pytest.fixture(scope="module")
def multi_user_server(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[MultiUserServer]:
    """A NON-single-user server so the Shared filter renders."""
    server_tmp = tmp_path_factory.mktemp("e2e_ui_sidebar_filter_independence")
    yield from spawn_multi_user_server(mock_llm_server_url, server_tmp)


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
    _patch(server, session_id, owner_email, {"title": title})
    return session_id


def _patch(server: MultiUserServer, session_id: str, actor_email: str, body: dict) -> None:
    """PATCH a session as *actor_email* (title / labels / archived)."""
    resp = httpx.patch(
        f"{server.base_url}/v1/sessions/{session_id}",
        json=body,
        headers={"X-Forwarded-Email": actor_email},
        timeout=10.0,
    )
    resp.raise_for_status()


def _grant(server: MultiUserServer, session_id: str, user_id: str, level: int) -> None:
    """Grant *user_id* *level* on *session_id* (the admin owner acts)."""
    resp = httpx.put(
        f"{server.base_url}/v1/sessions/{session_id}/permissions",
        json={"user_id": user_id, "level": level},
        headers={"X-Forwarded-Email": ADMIN_EMAIL},
        timeout=30.0,
    )
    resp.raise_for_status()


def _section(page: Page, title: str) -> Locator:
    """The sidebar ``<section>`` whose collapse-header button reads *title*."""
    return page.locator("section").filter(has=page.get_by_role("button", name=title, exact=True))


def _row(page: Page, session_id: str) -> Locator:
    """The sidebar row (``<li>``) for *session_id*, located by its href."""
    return page.locator("li").filter(has=page.locator(f'a[href="/c/{session_id}"]'))


def _set_filter(page: Page, value: str) -> None:
    """Open the filter funnel and pick *value* (all/mine/shared/archived)."""
    page.locator(_FILTER).click()
    page.locator(f'[data-testid="session-filter-{value}"]').click()


def _pin_row(page: Page, session_id: str) -> None:
    """Pin *session_id* via its hover-revealed quick-pin quick action."""
    row = _row(page, session_id)
    expect(row).to_be_visible(timeout=30_000)
    row.hover()
    pin_button = row.get_by_test_id("quick-pin-conversation")
    expect(pin_button).to_have_attribute("aria-label", "Pin conversation")
    pin_button.click()
    # The toggle flips to unpin once the pin lands — proves it went through the
    # sidebar's pin state, not a local no-op.
    expect(_row(page, session_id).get_by_test_id("quick-pin-conversation")).to_have_attribute(
        "aria-label", "Unpin conversation", timeout=15_000
    )


def _pinned_has(page: Page, session_id: str) -> None:
    """Assert *session_id* is present under the Pinned section header."""
    expect(_section(page, "Pinned").locator(f'a[href="/c/{session_id}"]')).to_be_visible(
        timeout=30_000
    )


@pytest.mark.flaky(reruns=2, reruns_delay=5)
def test_pinned_section_ignores_the_session_filter(
    browser: Browser,
    multi_user_server: MultiUserServer,
) -> None:
    """Every pin stays under Pinned on My / Shared / Archived filters.

    Pins the viewer's OWN session and a session SHARED with them, then walks the
    filter through My sessions, Shared sessions, and Archived sessions — both
    pins must stay under Pinned on all three. Before the fix the Pinned section
    was built from the filtered slice, so the shared pin vanished on "My
    sessions" and the owned pin vanished on "Shared sessions".
    """
    server = multi_user_server
    uniq = uuid.uuid4().hex[:6]
    viewer_email = f"viewer-pin-{uniq}@ui.test"

    # A session the viewer OWNS, and a session the admin owns SHARED with the
    # viewer (so "All sessions" shows both up front, ready to pin).
    owned = _create_session(server, owner_email=viewer_email, title=f"e2e-pin-owned-{uniq}")
    shared = _create_session(server, owner_email=ADMIN_EMAIL, title=f"e2e-pin-shared-{uniq}")
    _grant(server, shared, viewer_email, _LEVEL_EDIT)

    ctx = browser.new_context(extra_http_headers={"X-Forwarded-Email": viewer_email})
    try:
        page = ctx.new_page()
        page.goto(f"{server.public_url}/c/{owned}")
        expect(page.locator(_FILTER)).to_be_visible(timeout=30_000)

        # Pin both from the default "All sessions" view, where both rows show.
        _pin_row(page, owned)
        _pin_row(page, shared)
        _pinned_has(page, owned)
        _pinned_has(page, shared)

        # My sessions: the owned pin is expected, and the SHARED pin must not
        # drop out even though it isn't owned by the viewer.
        _set_filter(page, "mine")
        _pinned_has(page, owned)
        _pinned_has(page, shared)

        # Shared sessions: the shared pin is expected, and the OWNED pin must
        # not drop out even though the viewer owns it.
        _set_filter(page, "shared")
        _pinned_has(page, owned)
        _pinned_has(page, shared)

        # Archived sessions: neither pin is archived, but switching here must
        # not empty the Pinned section either.
        _set_filter(page, "archived")
        _pinned_has(page, owned)
        _pinned_has(page, shared)
    finally:
        ctx.close()


@pytest.mark.flaky(reruns=2, reruns_delay=5)
def test_projects_section_ignores_the_session_filter(
    browser: Browser,
    multi_user_server: MultiUserServer,
) -> None:
    """The Projects group + its folders survive the Shared / Archived filters.

    Files a viewer-owned session into a project, then walks the filter to Shared
    and Archived — the Projects group and the folder stay put. Before the fix
    the Projects section was suppressed entirely on the Shared/Archived tabs, so
    the folder vanished when the filter switched.
    """
    server = multi_user_server
    uniq = uuid.uuid4().hex[:6]
    viewer_email = f"viewer-proj-{uniq}@ui.test"
    project = f"E2E Proj {uniq}"

    # A viewer-owned session filed into a project (filing is owner-only, so the
    # folder surfaces for the owner). Pin nothing here — pin-precedence would
    # float it out of the folder.
    filed = _create_session(server, owner_email=viewer_email, title=f"e2e-proj-filed-{uniq}")
    _patch(server, filed, viewer_email, {"labels": {_PROJECT_LABEL_KEY: project}})

    ctx = browser.new_context(extra_http_headers={"X-Forwarded-Email": viewer_email})
    try:
        page = ctx.new_page()
        page.goto(f"{server.public_url}/c/{filed}")
        expect(page.locator(_FILTER)).to_be_visible(timeout=30_000)

        # My sessions: the Projects group and the project folder render.
        _set_filter(page, "mine")
        expect(page.get_by_role("button", name="Projects", exact=True)).to_be_visible(
            timeout=30_000
        )
        expect(page.get_by_role("button", name=project, exact=True)).to_be_visible()

        # Shared sessions: the Projects group and the folder must NOT disappear
        # (this whole section used to be hidden on the Shared tab).
        _set_filter(page, "shared")
        expect(page.get_by_role("button", name="Projects", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name=project, exact=True)).to_be_visible()

        # Archived sessions: same — the Projects group and folder stay put.
        _set_filter(page, "archived")
        expect(page.get_by_role("button", name="Projects", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name=project, exact=True)).to_be_visible()
    finally:
        ctx.close()


@pytest.mark.flaky(reruns=2, reruns_delay=5)
def test_shared_session_stays_in_flat_list_on_project_name_collision(
    browser: Browser,
    multi_user_server: MultiUserServer,
) -> None:
    """A shared session with a colliding project label isn't pulled into a folder.

    Filing is owner-only (unlike pins), so project-folder membership is
    ownership-gated. The viewer owns a session filed under a project, and a
    DIFFERENT owner's session — shared with the viewer — carries the same
    project-name label. That collision must not pull the foreign session into
    the viewer's folder (which would also drop it from the flat Sessions list).
    """
    server = multi_user_server
    uniq = uuid.uuid4().hex[:6]
    viewer_email = f"viewer-collide-{uniq}@ui.test"
    project = f"E2E Collide {uniq}"

    # The viewer's own session filed under the project (surfaces the folder).
    owned = _create_session(server, owner_email=viewer_email, title=f"e2e-collide-owned-{uniq}")
    _patch(server, owned, viewer_email, {"labels": {_PROJECT_LABEL_KEY: project}})
    # An admin-owned session SHARED with the viewer, carrying the SAME project
    # label — the name collision the ownership guard must reject.
    shared = _create_session(server, owner_email=ADMIN_EMAIL, title=f"e2e-collide-shared-{uniq}")
    _patch(server, shared, ADMIN_EMAIL, {"labels": {_PROJECT_LABEL_KEY: project}})
    _grant(server, shared, viewer_email, _LEVEL_EDIT)

    ctx = browser.new_context(extra_http_headers={"X-Forwarded-Email": viewer_email})
    try:
        page = ctx.new_page()
        page.goto(f"{server.public_url}/c/{owned}")
        expect(page.locator(_FILTER)).to_be_visible(timeout=30_000)

        # The folder exists (the owned session filed it), but the shared session
        # is NOT filed away: it stays in the flat Sessions list, not the folder.
        expect(page.get_by_role("button", name=project, exact=True)).to_be_visible(timeout=30_000)
        expect(_section(page, "Sessions").locator(f'a[href="/c/{shared}"]')).to_be_visible()
        expect(_row(page, shared)).to_be_visible()
    finally:
        ctx.close()
