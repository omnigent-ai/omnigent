"""Browser e2e: find an archived session by its session ID and restore it.

Two entry points lead back to an archived session when all the user has is an
ID:

* Settings › Archived — the search box sends ``search_query`` to
  ``GET /v1/sessions``; a pasted Omnigent session ID or the source harness's
  session ID matches exactly, alongside the usual title/content search.
* Settings › Import › Session by ID — re-importing a session that is already
  imported (and archived) names the existing session and offers Unarchive.

The archived sessions are real imports created through ``POST /v1/imports``,
and search and unarchive run against the live server. The import panel needs
an online host to read local transcripts, so that test stubs ``/v1/hosts`` and
the import stream's final event; the session that event names, and the
unarchive it triggers, are real. Each test creates its page after seeding so a
recording starts at the user's first navigation.
"""

from __future__ import annotations

import contextlib
import json
import os
import uuid
from collections.abc import Iterator
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from playwright.sync_api import Page, Response, Route, expect

_HOST_ID = "host_e2e_id_search"
_HOSTS_BODY = {
    "hosts": [{"host_id": _HOST_ID, "name": "e2e-laptop", "owner": "e2e", "status": "online"}]
}


def _search_archived(page: Page, query: str) -> None:
    """Type *query* into the archived search and wait for the server's answer to it.

    The search is debounced, so without this the previous query's rows can
    satisfy the next assertion before the new request is sent.
    """

    def _is_search_response(response: Response) -> bool:
        url = urlparse(response.url)
        return url.path.endswith("/v1/sessions") and parse_qs(url.query).get("search_query") == [
            query
        ]

    with page.expect_response(_is_search_response):
        page.get_by_test_id("archived-search").fill(query)


def _hold_for_recording(page: Page) -> None:
    """Keep an asserted state on screen long enough to read in a recording."""
    if os.environ.get("OMNIGENT_E2E_RECORD_DIR"):
        page.wait_for_timeout(1_500)


def _seed_archived_import(base_url: str, *, title: str, external_session_id: str) -> str:
    """Import one harness transcript, then archive the resulting session.

    :param base_url: The live server base URL.
    :param title: Unique session title, so rows are easy to find on a shared server.
    :param external_session_id: The source harness's session ID.
    :returns: The Omnigent session ID.
    """
    response = httpx.post(
        f"{base_url}/v1/imports",
        json={
            "source": "claude",
            "external_session_id": external_session_id,
            "title": title,
            "items": [
                {
                    "type": "message",
                    "response_id": "resp_1",
                    "data": {
                        "role": "user",
                        "content": [{"type": "input_text", "text": "Plan the Q3 migration"}],
                    },
                }
            ],
        },
        timeout=30.0,
    )
    response.raise_for_status()
    session_id = response.json()["session_id"]
    httpx.patch(
        f"{base_url}/v1/sessions/{session_id}", json={"archived": True}, timeout=10.0
    ).raise_for_status()
    return session_id


def _is_archived(base_url: str, session_id: str) -> bool:
    response = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    response.raise_for_status()
    return response.json()["archived"] is True


@pytest.fixture
def seeded_session_ids(live_server: str) -> Iterator[list[str]]:
    """Collect seeded session ids and delete them after the test.

    Cleanup runs in fixture teardown, after a recorded page has closed, so the
    clip ends on the asserted state rather than on rows disappearing.
    """
    session_ids: list[str] = []
    yield session_ids
    for session_id in session_ids:
        with contextlib.suppress(httpx.HTTPError):
            httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)


def test_archived_search_finds_and_unarchives_session_by_id(
    request: pytest.FixtureRequest,
    live_server: str,
    seeded_session_ids: list[str],
) -> None:
    """A pasted harness or Omnigent session ID finds the archived row; Unarchive restores it.

    Guards:

    - an ID that appears in neither title nor content still matches;
    - only the session with that ID is listed, not every archived session;
    - Unarchive from the search result puts the session back in the sidebar.
    """
    unique = uuid.uuid4().hex[:8]
    harness_session_id = str(uuid.uuid4())
    target_title = f"Q3 migration plan {unique}"
    decoy_title = f"Unrelated archive {unique}"
    target_id = _seed_archived_import(
        live_server, title=target_title, external_session_id=harness_session_id
    )
    seeded_session_ids.append(target_id)
    seeded_session_ids.append(
        _seed_archived_import(
            live_server, title=decoy_title, external_session_id=str(uuid.uuid4())
        )
    )

    page: Page = request.getfixturevalue("page")
    page.goto(f"{live_server}/settings/archived")
    rows = page.get_by_test_id("archived-row")
    expect(rows.filter(has_text=target_title)).to_have_count(1, timeout=30_000)
    expect(rows.filter(has_text=decoy_title)).to_have_count(1)
    _hold_for_recording(page)

    _search_archived(page, harness_session_id)
    expect(rows).to_have_count(1)
    expect(rows.first).to_contain_text(target_title)
    _hold_for_recording(page)

    _search_archived(page, target_id)
    expect(rows).to_have_count(1)
    expect(rows.first).to_contain_text(target_title)
    _hold_for_recording(page)

    _search_archived(page, f"no-such-session-{unique}")
    expect(page.get_by_test_id("archived-empty")).to_contain_text(
        f"No archived sessions match “no-such-session-{unique}”."
    )
    _hold_for_recording(page)

    # Back from the empty state, so the row count waits for this result.
    page.get_by_test_id("archived-search").fill(harness_session_id)
    expect(rows).to_have_count(1)
    _hold_for_recording(page)
    rows.first.get_by_test_id("unarchive-conversation").click()

    # Unarchiving opens the restored session, which is listed in the sidebar again.
    page.wait_for_url(f"**/c/{target_id}", timeout=30_000)
    expect(
        page.get_by_test_id("sidebar-conversation-list").get_by_text(target_title)
    ).to_be_visible(timeout=30_000)
    assert not _is_archived(live_server, target_id)
    _hold_for_recording(page)


def test_reimporting_archived_session_offers_unarchive(
    request: pytest.FixtureRequest,
    live_server: str,
    seeded_session_ids: list[str],
) -> None:
    """Import › Session by ID names an archived duplicate and restores it in place.

    Guards the client wiring of ``already_imported_session``: the notice names
    the existing session, links to it, and its Unarchive button clears the
    archived flag on the real session.
    """
    unique = uuid.uuid4().hex[:8]
    harness_session_id = str(uuid.uuid4())
    title = f"Archived import {unique}"
    session_id = _seed_archived_import(
        live_server, title=title, external_session_id=harness_session_id
    )
    seeded_session_ids.append(session_id)
    captured: dict[str, object] = {}

    def _handle_import(route: Route) -> None:
        captured["post"] = route.request.post_data_json
        done = {
            "event": "done",
            "imported": 0,
            "already_imported": 1,
            "failed": 0,
            "failures": [],
            "already_imported_session": {
                "session_id": session_id,
                "title": title,
                "archived": True,
                "can_unarchive": True,
            },
        }
        route.fulfill(
            status=200, content_type="application/x-ndjson", body=json.dumps(done) + "\n"
        )

    page: Page = request.getfixturevalue("page")
    page.route("**/v1/hosts", lambda route: route.fulfill(json=_HOSTS_BODY))
    page.route("**/v1/imports/local/stream", _handle_import)
    page.goto(f"{live_server}/settings/import")

    expect(page.get_by_test_id("import-sessions-panel")).to_be_visible(timeout=30_000)
    page.get_by_test_id("import-mode-select").click()
    page.get_by_role("option", name="Session by ID").click()
    page.get_by_test_id("import-session-id").fill(harness_session_id)
    _hold_for_recording(page)
    page.get_by_test_id("import-submit").click()

    notice = page.get_by_test_id("import-existing-session")
    expect(page.get_by_test_id("import-result")).to_contain_text(
        "Imported 0, 1 already imported.", timeout=30_000
    )
    expect(notice).to_contain_text(f"Already imported as “{title}” (archived).")
    expect(page.get_by_test_id("import-existing-session-open")).to_have_attribute(
        "href", f"/c/{session_id}"
    )
    post = captured["post"]
    assert isinstance(post, dict)
    assert (post["host_id"], post["source"], post["session_id"]) == (
        _HOST_ID,
        "claude",
        harness_session_id,
    )
    _hold_for_recording(page)

    page.get_by_test_id("import-existing-session-unarchive").click()
    expect(page.get_by_test_id("import-existing-session-restored")).to_be_visible(timeout=30_000)
    expect(notice).not_to_contain_text("(archived)")
    assert not _is_archived(live_server, session_id)
    _hold_for_recording(page)

    page.get_by_test_id("import-existing-session-open").click()
    page.wait_for_url(f"**/c/{session_id}", timeout=30_000)
    expect(page.get_by_test_id("sidebar-conversation-list").get_by_text(title)).to_be_visible(
        timeout=30_000
    )
    _hold_for_recording(page)
