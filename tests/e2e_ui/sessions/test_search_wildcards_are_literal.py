"""Browser e2e: the sidebar Search palette treats ``%`` and ``_`` in the query as
literal text and previews the item that literally contains the query."""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from playwright.sync_api import Locator, Page, Response, expect

from tests.e2e_ui.conftest import _create_runner_bound_session, _server_state, configure_mock_llm

_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'


@dataclass(frozen=True)
class _SearchSessions:
    base_url: str
    marker: str
    decoy_title: str
    progress_title: str
    lookalike_title: str
    underscore_title: str


def _send(page: Page, composer: Locator, text: str, *, replies: int) -> None:
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()
    expect(page.locator(_ASSISTANT)).to_have_count(replies, timeout=30_000)


def _wait_until_searchable(base_url: str, session_id: str, query: str) -> None:
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        resp = httpx.get(
            f"{base_url}/v1/sessions",
            params={"search_query": query, "limit": 50},
            timeout=10.0,
        )
        resp.raise_for_status()
        if any(conv["id"] == session_id for conv in resp.json()["data"]):
            return
        time.sleep(0.5)
    raise AssertionError(f"{query!r} never became searchable in session {session_id}")


@pytest.fixture
def search_sessions(
    page: Page,
    seeded_session_pair: tuple[str, str, str],
    mock_llm_server_url: str,
) -> Iterator[_SearchSessions]:
    """Four sessions a literal search can tell apart: a decoy with no ``%``, a chat with
    ``50%`` behind an earlier plain message, and two titles differing only at the ``_``."""
    base_url, decoy_id, progress_id = seeded_session_pair
    marker = uuid.uuid4().hex[:8]
    runner_id = str(_server_state["runner_id"])
    lookalike_id = _create_runner_bound_session(base_url, runner_id)
    underscore_id = _create_runner_bound_session(base_url, runner_id)
    try:
        configure_mock_llm(
            mock_llm_server_url,
            [{"text": "Noted."}] * 3,
            key=f"search-wildcards-{marker}",
            match=marker,
        )
        # Automatic titling would rename the sessions under test mid-journey.
        page.add_init_script(
            "window.localStorage.setItem('omnigent:background-session-titles', 'off')"
        )

        page.goto(f"{base_url}/c/{decoy_id}")
        composer = page.get_by_label("Message the agent")
        expect(composer).to_be_visible(timeout=30_000)
        _send(page, composer, f"innocent prose {marker}", replies=1)
        _wait_until_searchable(base_url, decoy_id, f"prose {marker}")

        page.goto(f"{base_url}/c/{progress_id}")
        composer = page.get_by_label("Message the agent")
        expect(composer).to_be_visible(timeout=30_000)
        _send(page, composer, f"innocent first {marker}", replies=1)
        _send(page, composer, f"literal 50% later {marker}", replies=2)
        _wait_until_searchable(base_url, progress_id, f"later {marker}")

        sessions = _SearchSessions(
            base_url=base_url,
            marker=marker,
            decoy_title=f"Plain prose {marker}",
            progress_title=f"Progress report {marker}",
            lookalike_title=f"fileXname {marker}",
            underscore_title=f"file_name {marker}",
        )
        for session_id, title in (
            (decoy_id, sessions.decoy_title),
            (progress_id, sessions.progress_title),
            (lookalike_id, sessions.lookalike_title),
            (underscore_id, sessions.underscore_title),
        ):
            httpx.patch(
                f"{base_url}/v1/sessions/{session_id}", json={"title": title}, timeout=10.0
            ).raise_for_status()
        yield sessions
    finally:
        for session_id in (lookalike_id, underscore_id):
            httpx.delete(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)


def _open_palette(page: Page) -> tuple[Locator, Locator]:
    page.get_by_test_id("sidebar-search-button").click()
    dialog = page.get_by_role("dialog", name="Command palette", exact=True)
    palette_input = page.get_by_test_id("command-palette-input")
    expect(palette_input).to_be_visible()
    return dialog, palette_input


def _search(page: Page, palette_input: Locator, query: str) -> None:
    def _is_this_search(response: Response) -> bool:
        url = urlparse(response.url)
        return url.path.endswith("/v1/sessions") and parse_qs(url.query).get("search_query") == [
            query
        ]

    # The palette drops its rows while a search is in flight, so a zero-count
    # assertion is only meaningful once this query's response has landed.
    with page.expect_response(_is_this_search):
        palette_input.fill(query)


def _session_rows(dialog: Locator, title: str) -> Locator:
    return dialog.get_by_role("option").filter(has_text=title)


# A bare ``_`` is not asserted here: every runner-bound session carries a
# resource-event item whose indexed text (``terminal_tui_main``) contains a
# literal underscore, so listing them is correct. The lookalike test covers ``_``.
def test_bare_percent_does_not_list_unrelated_session(
    page: Page, search_sessions: _SearchSessions
) -> None:
    sessions = search_sessions
    dialog, palette_input = _open_palette(page)

    _search(page, palette_input, "%")
    expect(_session_rows(dialog, sessions.progress_title)).to_be_visible()
    expect(_session_rows(dialog, sessions.decoy_title)).to_have_count(0)


def test_underscore_matches_single_character_lookalike(
    page: Page, search_sessions: _SearchSessions
) -> None:
    sessions = search_sessions
    dialog, palette_input = _open_palette(page)

    _search(page, palette_input, f"file_name {sessions.marker}")
    expect(_session_rows(dialog, sessions.underscore_title)).to_be_visible()
    expect(_session_rows(dialog, sessions.lookalike_title)).to_have_count(0)


def test_percent_match_shows_content_preview(page: Page, search_sessions: _SearchSessions) -> None:
    sessions = search_sessions
    dialog, palette_input = _open_palette(page)

    _search(page, palette_input, "%")
    row = _session_rows(dialog, sessions.progress_title)
    expect(row).to_be_visible()
    expect(row).to_contain_text("50% later")
