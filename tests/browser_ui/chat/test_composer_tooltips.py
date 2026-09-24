"""Browser-only pointer contracts for the composer configuration tooltip."""

import pytest
from playwright.sync_api import Page, expect


@pytest.mark.parametrize("turns", [0, 1], ids=["new-session", "existing-session"])
def test_pill_uses_one_tooltip_and_suppresses_it_while_picker_is_open(
    page: Page, chat_session_contract, turns: int
) -> None:
    chat_session_contract.seed_transcript(turns)
    chat_session_contract.set_catalog(
        harness="claude-native",
        models=[
            {
                "id": "sonnet",
                "model": "system.ai.claude-sonnet-5",
                "displayName": "Sonnet 5",
                "isDefault": True,
                "source": {
                    "kind": "databricks",
                    "label": "Workspace",
                    "name": "oss",
                    "host": "ws.example.com",
                },
            }
        ],
        selected_model="sonnet",
    )
    page.goto(chat_session_contract.url)
    if turns:
        expect(page.get_by_text("Request 1", exact=False)).to_be_visible()

    pill = page.get_by_test_id("composer-config-gear")
    expect(pill).to_be_visible()
    tooltip = page.get_by_test_id("composer-config-gear-tooltip")
    picker = page.get_by_test_id("composer-agent-menu")

    pill.hover()
    page.wait_for_timeout(100)
    pill.click()
    expect(picker).to_be_visible()
    page.wait_for_timeout(1_000)
    expect(picker).to_be_visible()
    expect(page.locator('[data-slot="tooltip-content"]')).to_have_count(0)

    page.keyboard.press("Escape")
    expect(picker).not_to_be_visible()
    expect(tooltip).not_to_be_visible()

    page.mouse.move(0, 0)
    pill.hover()
    expect(tooltip).to_be_visible()
    expect(page.locator('[data-slot="tooltip-content"]')).to_have_count(1)
    expect(tooltip).to_contain_text("Harness: Claude")
    expect(tooltip).to_contain_text("Model: Default")
    expect(tooltip).to_contain_text("Connection: Databricks · oss")

    pill.click()
    expect(picker).to_be_visible()
    expect(tooltip).not_to_be_visible()
    expect(page.locator('[data-slot="tooltip-content"]')).to_have_count(0)

    page.keyboard.press("Escape")
    expect(picker).not_to_be_visible()
    expect(tooltip).not_to_be_visible()

    page.mouse.move(0, 0)
    pill.hover()
    expect(tooltip).to_be_visible()
