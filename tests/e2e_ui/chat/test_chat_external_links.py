"""E2E: external markdown links open outside the current chat page."""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Page, expect

_AGENT_NAME = "hello_world"
_LINK_LABEL = "server health"


@pytest.fixture
def external_link_session(
    seeded_session: tuple[str, str],
) -> Iterator[tuple[str, str, str]]:
    """Seed a deterministic assistant reply containing an external link."""
    base_url, session_id = seeded_session
    link_url = f"{base_url}/health"
    event_resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={
            "type": "external_assistant_message",
            "data": {
                "agent": _AGENT_NAME,
                "text": f"Check the [{_LINK_LABEL}]({link_url}).",
            },
        },
        timeout=10.0,
    )
    event_resp.raise_for_status()
    yield (base_url, session_id, link_url)


def test_external_chat_link_opens_new_page(
    page: Page,
    external_link_session: tuple[str, str, str],
) -> None:
    """External links open a new page while the chat remains in place."""
    base_url, session_id, link_url = external_link_session
    chat_url = f"{base_url}/c/{session_id}"
    page.goto(chat_url)

    link = page.get_by_role("link", name=_LINK_LABEL)
    expect(link).to_be_visible(timeout=30_000)
    expect(link).to_have_attribute("href", link_url)
    expect(link).to_have_attribute("target", "_blank")

    with page.expect_popup() as popup_info:
        link.click()

    popup = popup_info.value
    popup.wait_for_load_state("domcontentloaded")
    expect(popup).to_have_url(link_url)
    expect(page).to_have_url(chat_url)
