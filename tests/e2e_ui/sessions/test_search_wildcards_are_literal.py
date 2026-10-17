"""Browser e2e: the sidebar Search palette matches ``%`` literally.

The palette forwards the query to ``GET /v1/sessions?search_query=…``, a
case-insensitive substring filter, so ``%`` must only list sessions containing
``%`` and the dimmed preview must show the text that contains the query. Two
hello_world sessions are seeded once per module through the real composer and
titled through the sessions API: ``Plain prose`` (``innocent prose``, the decoy)
and ``Progress report`` (``innocent first`` then ``literal 50% later``).
Underscores are covered at the store layer: every runner-bound session indexes a
resource event named ``terminal_tui_main``, so a palette search for ``_``
correctly lists them all.
"""

from __future__ import annotations

import subprocess
import urllib.parse
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import httpx
import pytest
from playwright.sync_api import Browser, Locator, Page, Response, expect

from tests.e2e_ui.conftest import (
    _create_runner_bound_session,
    _ensure_runner_online,
    _server_state,
    set_fallback_mock_llm,
)

_REPLY = "Noted."
_HELLO_WORLD_MODEL = "gpt-4o-mini"
# Browser-local preference the SPA sends as a request header; keeps background
# title inference from renaming the seeded sessions mid-journey.
_TITLES_OFF_STORAGE_KEY = "omnigent:background-session-titles"
_TITLES_OFF_INIT_SCRIPT = f'window.localStorage.setItem("{_TITLES_OFF_STORAGE_KEY}", "off");'
_TITLES_OFF_HEADERS = {"x-omnigent-background-session-titles": "off"}


@dataclass(frozen=True)
class SeededSearchSessions:
    base_url: str
    marker: str
    ids: dict[str, str]

    def title(self, name: str) -> str:
        return f"{name} {self.marker}"


def _send_and_wait(page: Page, text: str, expected_replies: int) -> None:
    composer = page.get_by_label("Message the agent", exact=True)
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(text)
    composer.press("Enter")
    expect(page.get_by_text(_REPLY, exact=True)).to_have_count(expected_replies, timeout=60_000)


@pytest.fixture(scope="module")
def seeded_search_sessions(
    live_server: str,
    mock_llm_server_url: str,
    browser: Browser,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[SeededSearchSessions]:
    respawned_runner = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    marker = uuid.uuid4().hex[:8]
    ids: dict[str, str] = {}
    # One try/finally around seeding and the yield, so a failure while seeding
    # still deletes the sessions created so far and stops a respawned runner.
    try:
        set_fallback_mock_llm(mock_llm_server_url, _HELLO_WORLD_MODEL, _REPLY)
        for name in ("Plain prose", "Progress report"):
            ids[name] = _create_runner_bound_session(live_server, runner_id)
        seeded = SeededSearchSessions(base_url=live_server, marker=marker, ids=ids)

        context = browser.new_context()
        context.add_init_script(_TITLES_OFF_INIT_SCRIPT)
        page = context.new_page()
        try:
            page.goto(f"{live_server}/c/{ids['Plain prose']}")
            _send_and_wait(page, f"innocent prose {marker}", 1)
            page.goto(f"{live_server}/c/{ids['Progress report']}")
            _send_and_wait(page, f"innocent first {marker}", 1)
            _send_and_wait(page, f"literal 50% later {marker}", 2)
        finally:
            context.close()

        for name, session_id in ids.items():
            httpx.patch(
                f"{live_server}/v1/sessions/{session_id}",
                json={"title": seeded.title(name)},
                headers=_TITLES_OFF_HEADERS,
                timeout=10.0,
            ).raise_for_status()

        yield seeded
    finally:
        for session_id in ids.values():
            httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        if respawned_runner is not None:
            respawned_runner.terminate()
            try:
                respawned_runner.wait(timeout=5)
            except subprocess.TimeoutExpired:
                respawned_runner.kill()
                respawned_runner.wait(timeout=5)


def _session_row(dialog: Locator, title: str) -> Locator:
    return dialog.get_by_role("option").filter(has_text=title)


def test_percent_lists_only_the_literal_match_with_its_preview(
    page: Page,
    seeded_search_sessions: SeededSearchSessions,
) -> None:
    seeded = seeded_search_sessions
    page.add_init_script(_TITLES_OFF_INIT_SCRIPT)
    page.goto(f"{seeded.base_url}/c/{seeded.ids['Plain prose']}")
    search_button = page.get_by_test_id("sidebar-search-button")
    expect(search_button).to_be_visible(timeout=30_000)
    search_button.click()
    palette_input = page.get_by_test_id("command-palette-input")
    expect(palette_input).to_be_visible()
    dialog = page.get_by_role("dialog")

    def _is_this_search(response: Response) -> bool:
        url = urllib.parse.urlparse(response.url)
        return url.path.endswith("/v1/sessions") and urllib.parse.parse_qs(url.query).get(
            "search_query"
        ) == ["%"]

    # The palette empties while the debounced query is in flight, so the
    # negative assertion is only meaningful once this query's results arrived.
    with page.expect_response(_is_this_search, timeout=15_000):
        palette_input.fill("%")

    row = _session_row(dialog, seeded.title("Progress report"))
    expect(row).to_be_visible()
    expect(row).to_contain_text(f"literal 50% later {seeded.marker}")
    expect(_session_row(dialog, seeded.title("Plain prose"))).to_have_count(0)
