"""Browser e2e for the sidebar's session status filter.

The header filter button (``sidebar-status-filter-button``) opens a
`HoverCard`-based menu on hover — not click — offering All/Active/Completed
(``GET /v1/sessions?status=``). "Active" is a session currently
running/waiting on an agent; "Completed" is everything else, including any
session carrying the ``omnigent.closed`` label regardless of its live
status (see ``matchesSessionStatusFilter`` in ``useSessionState.ts`` and the
server-side ``status`` param on ``ConversationStore.list_conversations``).

These drive the real chain the ``Sidebar`` unit tests mock out: hovering the
real header button opens the real `HoverCard` (proving the hover interaction
and its containment inside the sidebar actually work in a browser, not just
via a synthetic ``fireEvent.pointerEnter``), and selecting an option re-reads
the live, unmocked ``GET /v1/sessions`` list. Neither seeded session here is
ever actually running, so this covers the "completed" bucket (idle and
closed) and the "active" filter correctly excluding both — the "running"
bucket would need a live agent turn, which is out of scope for this UI-only
gate.
"""

from __future__ import annotations

import contextlib
import json
import uuid

import httpx
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import _build_hello_world_bundle


def _seed_session(base_url: str, *, title: str, closed: bool = False) -> str:
    """Create a session and give it a unique title, optionally closed.

    Creation goes through the same multipart ``POST /v1/sessions`` path as
    ``test_archived_project_filter.py``'s ``_seed_archived_session``; a
    ``PATCH`` sets the title and, when ``closed``, the ``omnigent.closed``
    label — the same reserved-but-client-settable key
    ``sys_session_close`` writes (see
    ``_reject_server_reserved_label_seed``, which does not block it).

    :param base_url: The live server base URL.
    :param title: Unique title so the row is easy to spot among other
        tests' sessions on the shared server.
    :param closed: When ``True``, marks the session closed — it must read
        as "completed" regardless of live status.
    :returns: The new session id.
    """
    create_resp = httpx.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", _build_hello_world_bundle(), "application/gzip")},
        timeout=30.0,
    )
    create_resp.raise_for_status()
    session_id = create_resp.json()["session_id"]

    body: dict[str, object] = {"title": title}
    if closed:
        body["labels"] = {"omnigent.closed": "true"}
    patch_resp = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json=body,
        timeout=10.0,
    )
    patch_resp.raise_for_status()
    return session_id


def _delete_sessions(base_url: str, session_ids: list[str]) -> None:
    """Best-effort cleanup so seeded sessions don't leak into other tests."""
    for session_id in session_ids:
        with contextlib.suppress(httpx.HTTPError):
            httpx.delete(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)


def _open_status_filter(page: Page) -> None:
    """Hover the header status-filter button until its menu is visible.

    Playwright's ``hover()`` dispatches real pointer events, so this
    exercises the same ``onPointerEnter`` path Radix's `HoverCard` listens
    on (a synthetic ``fireEvent.mouseEnter`` in the unit tests does not —
    see ``useSessionState`` / ``SessionStatusFilterMenu``). ``expect(...
    ).to_be_visible()`` absorbs the HoverCard's ``openDelay``.
    """
    page.get_by_test_id("sidebar-status-filter-button").hover()
    expect(page.get_by_test_id("sidebar-status-filter-active")).to_be_visible()


def test_sidebar_status_filter_opens_on_hover_and_filters(
    page: Page,
    live_server: str,
) -> None:
    """Hovering the header button opens the menu; selecting a status filters.

    Seeds one plain (idle, never-observed) session and one closed session,
    then asserts:

    - the menu is closed until hovered, and opens without a click;
    - "Active" hides both (neither is running);
    - "Completed" shows both, including the closed one;
    - "All sessions" restores the full list.
    """
    uniq = uuid.uuid4().hex[:8]
    titles = {
        "idle": f"e2e-statusfilter-idle-{uniq}",
        "closed": f"e2e-statusfilter-closed-{uniq}",
    }
    session_ids: list[str] = []
    try:
        session_ids.append(_seed_session(live_server, title=titles["idle"]))
        session_ids.append(_seed_session(live_server, title=titles["closed"], closed=True))

        page.goto(f"{live_server}/")

        idle_row = page.get_by_text(titles["idle"], exact=True)
        closed_row = page.get_by_text(titles["closed"], exact=True)
        expect(idle_row).to_be_visible()
        expect(closed_row).to_be_visible()

        # Closed until hovered — no click has happened yet.
        expect(page.get_by_test_id("sidebar-status-filter-active")).to_be_hidden()

        _open_status_filter(page)
        page.get_by_test_id("sidebar-status-filter-active").click()

        # Active: neither session is running.
        expect(idle_row).to_be_hidden()
        expect(closed_row).to_be_hidden()

        _open_status_filter(page)
        page.get_by_test_id("sidebar-status-filter-completed").click()

        # Completed: idle (never observed) and closed both qualify.
        expect(idle_row).to_be_visible()
        expect(closed_row).to_be_visible()

        _open_status_filter(page)
        page.get_by_test_id("sidebar-status-filter-all").click()

        # Back to unfiltered.
        expect(idle_row).to_be_visible()
        expect(closed_row).to_be_visible()
    finally:
        _delete_sessions(live_server, session_ids)
