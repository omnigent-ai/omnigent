"""Terminal prompts reach the live chat and remain readable after reload."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

import httpx
from playwright.sync_api import Page, expect

from omnigent.harnesses.claude_native import forwarder
from omnigent.harnesses.claude_native.tui_messages import terminal_message_from_pane
from tests.test_claude_native_tui_messages import TOOL_SEARCH_PROMPT


def test_terminal_prompt_notice_streams_and_survives_reload(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_role("textbox", name="Message the agent")).to_be_visible(timeout=15_000)

    async def relay() -> None:
        async with httpx.AsyncClient(base_url=base_url) as client:
            for _ in range(2):
                dedupe = forwarder._ForwardDedupeState()
                for _ in range(2):
                    await forwarder._relay_terminal_message(
                        client,
                        session_id=session_id,
                        message=terminal_message_from_pane(TOOL_SEARCH_PROMPT),
                        response_id="terminal-notice-turn",
                        dedupe=dedupe,
                    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(asyncio.run, relay()).result(timeout=30)
    for reload in (False, True):
        if reload:
            page.reload()
        notice = page.get_by_test_id("error-pill")
        expect(notice).to_have_count(1, timeout=15_000)
        expect(notice).to_have_attribute("data-level", "info")
        notice.locator('button[aria-expanded="false"]').click()
        contents = notice.get_by_test_id("error-message-content")
        for text in (
            "ToolSearch",
            "Databricks token refresh returned no token",
            "settings.json to update hooks",
            "1. Yes",
            "2. Yes, and don't ask again",
            "3. No",
        ):
            expect(contents).to_contain_text(text)
