"""Browser e2e for the sidebar's session status filter.

The Sessions heading's filter funnel (``session-filter``) carries a second
radio group below the scope options: Any status / Active / Completed.
"Active" is a session currently running or waiting on an agent; "Completed"
is everything else, including any session carrying the ``omnigent.closed``
label regardless of its live status (see ``matchesSessionStatusFilter`` in
``useSessionState.ts``).

This drives the real chain the ``Sidebar`` unit tests mock out: the real
funnel, the real Radix menu, and the live unmocked ``GET /v1/sessions`` list.
Neither seeded session is ever actually running, so this covers the
"completed" bucket (idle and closed) and "Active" correctly excluding both;
the "running" bucket would need a live agent turn, out of scope for a
UI-only gate.
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
    label — the same key ``sys_session_close`` writes.

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


def _set_status_filter(page: Page, value: str) -> None:
    """Open the Sessions filter funnel and pick a status (all/active/completed)."""
    page.get_by_test_id("session-filter").click()
    page.get_by_test_id(f"session-status-filter-{value}").click()


def test_sidebar_status_filter_narrows_the_session_list(
    page: Page,
    live_server: str,
) -> None:
    """Selecting a status in the Sessions filter menu narrows the list.

    Seeds one plain (idle, never-observed) session and one closed session,
    then asserts:

    - "Active" hides both (neither is running);
    - "Completed" shows both, including the closed one;
    - "Any status" restores the full list.
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

        _set_status_filter(page, "active")

        # Active: neither session is running.
        expect(idle_row).to_be_hidden()
        expect(closed_row).to_be_hidden()

        _set_status_filter(page, "completed")

        # Completed: idle (never observed) and closed both qualify.
        expect(idle_row).to_be_visible()
        expect(closed_row).to_be_visible()

        _set_status_filter(page, "all")

        # Back to unfiltered.
        expect(idle_row).to_be_visible()
        expect(closed_row).to_be_visible()
    finally:
        _delete_sessions(live_server, session_ids)
