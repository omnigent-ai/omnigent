"""E2E: Settings → Appearance default Chat / Terminal session view.

The browser flow uses two real server-backed sessions and patches only their
browser-visible session/terminal snapshots. This keeps the test deterministic
without launching a native CLI while exercising the production Settings and
AppShell code paths end to end.
"""

from __future__ import annotations

import json
from urllib.parse import urlparse

import httpx
from playwright.sync_api import Page, Route, expect

STORAGE_KEY = "omnigent:default-session-view"


def _patch_terminal_first_snapshots(
    page: Page,
    *,
    terminal_session_id: str,
    no_terminal_session_id: str,
    session_payload: dict[str, object],
) -> None:
    """Present two sessions as terminal-first, with a terminal only on one."""
    session_ids = {terminal_session_id, no_terminal_session_id}

    def _handle(route: Route) -> None:
        request = route.request
        path = urlparse(request.url).path

        for session_id in session_ids:
            if path == f"/v1/sessions/{session_id}/resources/terminals":
                data = []
                if session_id == terminal_session_id:
                    data.append(
                        {
                            "id": "terminal_claude_main",
                            "type": "terminal",
                            "name": "claude",
                            "metadata": {
                                "terminal_name": "claude",
                                "session_key": "main",
                                "running": True,
                                "terminal_transport": "pty",
                            },
                        }
                    )
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"data": data}),
                )
                return

            if path == f"/v1/sessions/{session_id}" and request.method == "GET":
                payload = dict(session_payload)
                payload["id"] = session_id
                payload["labels"] = {
                    **payload.get("labels", {}),
                    "omnigent.ui": "terminal",
                    "omnigent.wrapper": "claude-code-native-ui",
                }
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(payload),
                )
                return

        route.continue_()

    page.route("**/v1/sessions/**", _handle)


def _expect_view(page: Page, view: str) -> None:
    """Wait for the terminal-first switcher and assert its active segment."""
    expect(page.get_by_test_id("view-mode-toggle")).to_be_visible(timeout=30_000)
    expect(page.get_by_test_id(f"view-mode-{view}")).to_have_attribute(
        "aria-pressed", "true", timeout=30_000
    )


def test_default_session_view_persists_and_respects_session_eligibility(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Terminal persists, explicit Chat wins, and no-terminal sessions stay Chat."""
    base_url, terminal_session_id = seeded_session
    no_terminal_session_id = "conv_no_terminal_default_view"
    snapshot = httpx.get(f"{base_url}/v1/sessions/{terminal_session_id}", timeout=10.0)
    snapshot.raise_for_status()
    _patch_terminal_first_snapshots(
        page,
        terminal_session_id=terminal_session_id,
        no_terminal_session_id=no_terminal_session_id,
        session_payload=snapshot.json(),
    )

    page.goto(f"{base_url}/settings/appearance")
    expect(page.get_by_role("radiogroup", name="Default session view")).to_be_visible(
        timeout=30_000
    )
    chat_card = page.get_by_test_id("default-session-view-chat")
    terminal_card = page.get_by_test_id("default-session-view-terminal")
    expect(chat_card).to_have_attribute("aria-checked", "true")
    assert page.evaluate(f"() => localStorage.getItem('{STORAGE_KEY}')") is None

    terminal_card.click()
    expect(terminal_card).to_have_attribute("aria-checked", "true")
    assert page.evaluate(f"() => localStorage.getItem('{STORAGE_KEY}')") == "terminal"

    page.reload()
    expect(page.get_by_test_id("default-session-view-terminal")).to_have_attribute(
        "aria-checked", "true", timeout=30_000
    )

    page.goto(f"{base_url}/c/{terminal_session_id}")
    _expect_view(page, "terminal")

    page.get_by_test_id("view-mode-chat").click()
    _expect_view(page, "chat")
    assert (
        page.evaluate(
            f"() => sessionStorage.getItem('omnigent.web.panel-key:{terminal_session_id}')"
        )
        == "chat"
    )

    page.goto(f"{base_url}/settings/appearance")
    page.goto(f"{base_url}/c/{terminal_session_id}")
    _expect_view(page, "chat")

    page.goto(f"{base_url}/c/{no_terminal_session_id}")
    _expect_view(page, "chat")
    expect(page.get_by_test_id("view-mode-terminal")).to_be_disabled()
