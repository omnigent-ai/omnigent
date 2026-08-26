"""Browser e2e: transcript views survive a cold reload through the URL."""

from __future__ import annotations

import json
import re

import httpx
from playwright.sync_api import Page, Route, expect


def test_transcript_views_survive_cold_reload(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Selecting Terminal or Chat writes and restores the explicit URL view."""
    base_url, session_id = seeded_session
    response = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"labels": {"omnigent.ui": "terminal"}},
        timeout=10.0,
    )
    response.raise_for_status()

    # Supply the session's agent pane deterministically. The URL behavior does
    # not need a live PTY, only the same resource shape the runner publishes.
    def _serve_agent_terminal(route: Route) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "object": "list",
                    "data": [
                        {
                            "id": "terminal_tui_main",
                            "type": "terminal",
                            "session_id": session_id,
                            "name": "tui:main",
                            "metadata": {
                                "terminal_name": "tui",
                                "session_key": "main",
                                "running": True,
                            },
                        }
                    ],
                    "first_id": "terminal_tui_main",
                    "last_id": "terminal_tui_main",
                    "has_more": False,
                }
            ),
        )

    terminal_list = re.compile(rf"/v1/sessions/{re.escape(session_id)}/resources/terminals\?.*")
    page.route(terminal_list, _serve_agent_terminal)

    page.goto(f"{base_url}/c/{session_id}")
    terminal_button = page.get_by_test_id("view-mode-terminal")
    expect(terminal_button).to_be_enabled(timeout=60_000)
    terminal_button.click()

    expect(page).to_have_url(re.compile(r"[?&]view=terminal(?:&|$)"))
    expect(terminal_button).to_have_attribute("aria-pressed", "true")
    # Control-mode terminals use native browser selection without a modifier hint.
    expect(page.get_by_test_id("terminal-selection-hint")).to_have_count(0)

    # Remove the same-tab fallback so the reload must restore from the URL.
    page.evaluate("window.sessionStorage.clear()")
    page.reload()

    terminal_button = page.get_by_test_id("view-mode-terminal")
    expect(terminal_button).to_have_attribute("aria-pressed", "true", timeout=60_000)
    expect(page).to_have_url(re.compile(r"[?&]view=terminal(?:&|$)"))

    chat_button = page.get_by_test_id("view-mode-chat")
    chat_button.click()
    expect(page).to_have_url(re.compile(r"[?&]view=chat(?:&|$)"))
    expect(chat_button).to_have_attribute("aria-pressed", "true")

    page.evaluate("window.sessionStorage.clear()")
    page.reload()

    chat_button = page.get_by_test_id("view-mode-chat")
    expect(chat_button).to_have_attribute("aria-pressed", "true", timeout=60_000)
    expect(page).to_have_url(re.compile(r"[?&]view=chat(?:&|$)"))
