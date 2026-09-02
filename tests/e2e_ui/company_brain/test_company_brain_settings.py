from __future__ import annotations

from playwright.sync_api import Page, expect


def test_admin_can_open_company_brain_settings(page: Page, live_server: str) -> None:
    page.goto(f"{live_server}/settings/company-brain")

    expect(page).to_have_url(f"{live_server}/settings/company-brain", timeout=30_000)
    expect(page.get_by_test_id("settings-nav-company-brain")).to_be_visible(timeout=30_000)
    expect(page.get_by_role("heading", name="Company brain", exact=True)).to_be_visible()
    expect(page.get_by_text("Brain health", exact=True)).to_be_visible()
    expect(page.get_by_text("Not provisioned", exact=True)).to_be_visible()
    expect(page.get_by_text("No sources connected.", exact=True)).to_be_visible()

    page.get_by_role("button", name="Connect source", exact=True).click()

    expect(page.get_by_role("dialog", name="Connect a company source")).to_be_visible()
    expect(page.get_by_text("Google Workspace", exact=True)).to_be_visible()
    expect(page.get_by_text("Slack", exact=True)).to_be_visible()
    expect(page.get_by_text("Notion", exact=True)).to_be_visible()
    expect(page.get_by_text("Not configured", exact=True)).to_have_count(3)
