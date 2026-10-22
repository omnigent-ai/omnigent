"""E2E: a ``/rename`` typed in a native terminal reaches the open UI live.

A ``/rename`` in a claude-native session's Claude Code pane appends a
``custom-title`` record to the transcript; the forwarder mirrors it onto
the Omnigent session by posting an ``external_session_title`` event to
``POST /v1/sessions/{id}/events``. The server persists ``title`` and
publishes a ``session.title`` SSE event on the session's own stream. The
open tab, subscribed to that stream, parses it (``sse.ts``), and the
chat store's ``session_title`` handler overlays the new title into the
sidebar / session caches (``overlayTitleIntoCaches``) — so the row
re-titles with no reload and no list refetch.

This is a *different* seam from ``test_session_rename_streams_to_open_tabs``:
that renames over ``PATCH /v1/sessions/{id}`` and rides the
``WS /v1/sessions/updates`` diff into tabs watching (but not necessarily
viewing) the session. Here the rename never touches the REST title
endpoint — it arrives as a terminal-originated event that only surfaces
in an *open* tab through the ``session.title`` SSE path this PR added. A
regression anywhere along that path (the event rejected at the route,
the SSE never published, ``parseEvent`` dropping ``session.title``, or
the store not overlaying it) makes the live assertion time out.

Selector: the sidebar renders each session as ``<a href="/c/{id}">``
whose text is the title (``web/src/shell/Sidebar.tsx``). The default
pytest-playwright viewport (1280×720) is desktop, so the sidebar shows
without a toggle.
"""

from __future__ import annotations

import uuid

import httpx
from playwright.sync_api import Page, expect


def test_terminal_rename_streams_to_open_ui(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A terminal-originated rename re-titles the open sidebar row live.

    Posts the exact event the claude-native forwarder sends for a
    ``/rename`` — ``{"type": "external_session_title", "data": {"title":
    ...}}`` — to the session's events endpoint, with no action in the
    browser. The open tab must then show the new title fast (via the
    ``session.title`` SSE push), and the server must have persisted it.

    :param page: Playwright page fixture (fresh context per test,
        default desktop viewport).
    :param seeded_session: ``(base_url, session_id)`` for a runner-bound
        session, created by the fixture.
    """
    base_url, session_id = seeded_session
    # Unique per run so the row-text assertion can only match the title we
    # set here, never a leftover from another test on the shared DB.
    new_title = f"terminal-rename-{uuid.uuid4().hex[:8]}"
    row = f'a[href="/c/{session_id}"]'

    page.goto(f"{base_url}/c/{session_id}")

    # The sidebar row renders ⇒ the tab loaded the list and opened the
    # session's SSE stream. This must hold *before* the rename so a later
    # match is a live SSE delivery, not the initial snapshot.
    expect(page.locator(row)).to_be_visible()

    # A terminal ``/rename`` mirrored by the forwarder: post the external
    # title event straight to the events endpoint, exactly as the
    # claude-native forwarder does. No browser action triggers it, so any
    # UI change is server-pushed over the open session stream.
    resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={"type": "external_session_title", "data": {"title": new_title}},
        timeout=10.0,
    )
    resp.raise_for_status()

    # KEY ASSERTION: the open tab re-titles the row from the ``session.title``
    # SSE push alone — no reload, no list refetch. A broken path along
    # server-publish → parseEvent → overlayTitleIntoCaches times out here.
    expect(page.locator(row)).to_contain_text(new_title, timeout=20_000)

    # And the server persisted it, so the rename survives a reload rather
    # than living only in the client cache.
    snap = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    snap.raise_for_status()
    assert snap.json().get("title") == new_title, (
        f"server should persist the terminal-renamed title {new_title!r}, "
        f"got {snap.json().get('title')!r}"
    )
