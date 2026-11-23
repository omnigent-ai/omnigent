"""Custom agents' list and an agent's page are Settings catalog rows: a link to either opens
that page at the control it names.

Settings search sends a chosen result to `/settings/<section>#<row>[:<part>]`, and so does any
link to a row. An agent's own page has an id in its path, so a result for one of its controls
lands on the list, which stands in for it; on the agent's open page the same link reaches it.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._helpers.session import bundle_files


@pytest.fixture
def agent_id(live_server: str) -> Iterator[str]:
    """An agent of your own, installed for the journey and removed after it."""
    name = f"rows-{uuid.uuid4().hex[:8]}"
    bundle = bundle_files(
        {
            "config.yaml": (
                f"spec_version: 1\nname: {name}\ndescription: Row links\n"
                "instructions: notes.md\nexecutor:\n  config:\n    harness: claude-sdk\n"
            ).encode(),
            "notes.md": b"Follow the links.\n",
        }
    )
    with httpx.Client(base_url=live_server, timeout=20) as api:
        installed = api.post("/v1/agents", files={"bundle": ("agent.tar.gz", bundle)})
        installed.raise_for_status()
        installed_id = installed.json()["id"]
        try:
            yield installed_id
        finally:
            api.delete(f"/v1/agents/{installed_id}")


def test_a_link_to_the_agent_list_focuses_its_search(page: Page, live_server: str) -> None:
    page.goto(f"{live_server}/settings/custom-agents#custom-agents-list")

    row = page.locator('[data-settings-item="custom-agents-list"]')
    expect(row.get_by_role("searchbox", name="Search agents")).to_be_focused(timeout=10_000)


def test_settings_search_finds_the_agent_list_and_opens_it_at_its_search(
    page: Page, live_server: str
) -> None:
    page.goto(f"{live_server}/settings/general")
    field = page.get_by_role("combobox", name="Search settings")
    expect(field).to_be_visible(timeout=30_000)
    field.fill("search agents")
    page.get_by_role("option", name=re.compile(r"^Search agents")).first.click()

    expect(page).to_have_url(re.compile(r"/settings/custom-agents#custom-agents-list:search$"))
    row = page.locator('[data-settings-item="custom-agents-list"]')
    expect(row.get_by_role("searchbox", name="Search agents")).to_be_focused(timeout=10_000)


def test_a_link_to_an_agents_control_lands_on_the_list_that_stands_in_for_it(
    page: Page, live_server: str
) -> None:
    page.goto(f"{live_server}/settings/custom-agents#custom-agent-detail:instructions")

    row = page.locator('[data-settings-item="custom-agents-list"]')
    expect(row.get_by_role("searchbox", name="Search agents")).to_be_focused(timeout=10_000)


def test_on_an_agents_page_a_link_reaches_the_control_it_names(
    page: Page, live_server: str, agent_id: str
) -> None:
    page.goto(f"{live_server}/settings/custom-agents/{agent_id}")
    row = page.locator('[data-settings-item="custom-agent-detail"]')
    instructions = row.get_by_role("textbox", name="Instructions", exact=True)
    expect(instructions).to_have_value("Follow the links.\n")

    # A same-page link to the control: only the hash changes, so the page stays open.
    page.evaluate("() => { window.location.hash = 'custom-agent-detail:instructions'; }")
    expect(instructions).to_be_focused(timeout=10_000)
    # Its Back names where it goes, and still goes there.
    row.get_by_role("button", name="Back to Custom agents").click()
    expect(page.get_by_role("heading", name="Custom agents", exact=True)).to_be_visible()


def test_a_cold_link_on_an_agents_page_reaches_the_control_once_the_agent_loads(
    page: Page, live_server: str, agent_id: str
) -> None:
    # The form renders only after the agent's configuration loads; Back stands in until then.
    page.goto(f"{live_server}/settings/custom-agents/{agent_id}#custom-agent-detail:instructions")

    row = page.locator('[data-settings-item="custom-agent-detail"]')
    instructions = row.get_by_role("textbox", name="Instructions", exact=True)
    expect(instructions).to_have_value("Follow the links.\n")
    expect(instructions).to_be_focused(timeout=10_000)


def test_each_mcp_servers_fields_are_named_after_their_own_server(
    page: Page, live_server: str
) -> None:
    """Two servers' fields each say which server they belong to, and edit only that one."""
    name = f"servers-{uuid.uuid4().hex[:8]}"
    bundle = bundle_files(
        {
            "config.yaml": (
                f"spec_version: 1\nname: {name}\ndescription: Two servers\n"
                "executor:\n  config:\n    harness: claude-sdk\n"
                "tools:\n"
                "  docs:\n    type: mcp\n    url: https://docs.example.com/mcp\n"
                "    headers:\n      Authorization: test-credential\n"
                "  notes:\n    type: mcp\n    url: https://notes.example.com/mcp\n"
            ).encode(),
        }
    )
    with httpx.Client(base_url=live_server, timeout=20) as api:
        installed = api.post("/v1/agents", files={"bundle": ("agent.tar.gz", bundle)})
        installed.raise_for_status()
        installed_id = installed.json()["id"]
        try:
            page.goto(f"{live_server}/settings/custom-agents/{installed_id}")
            form = page.get_by_role("form", name="Agent configuration")
            expect(form.get_by_role("textbox", name="URL for docs", exact=True)).to_have_value(
                "https://docs.example.com/mcp"
            )
            notes_url = form.get_by_role("textbox", name="URL for notes", exact=True)
            expect(notes_url).to_have_value("https://notes.example.com/mcp")
            expect(form.get_by_role("combobox", name="Action for Authorization")).to_have_value(
                "keep"
            )

            notes_url.fill("https://notes.example.com/v2")
            expect(form.get_by_role("textbox", name="URL for docs", exact=True)).to_have_value(
                "https://docs.example.com/mcp"
            )
            expect(form.get_by_role("button", name="Save changes")).to_be_enabled()
        finally:
            api.delete(f"/v1/agents/{installed_id}")


def test_on_the_create_page_a_link_reaches_its_field_and_servers_are_named_by_their_name(
    page: Page, live_server: str
) -> None:
    page.goto(f"{live_server}/settings/custom-agents/new")
    row = page.locator('[data-settings-item="custom-agent-create"]')
    instructions = row.get_by_role("textbox", name="System instructions", exact=True)
    expect(instructions).to_be_visible(timeout=30_000)

    # A same-page link to the control: only the hash changes, so the page stays open.
    page.evaluate("() => { window.location.hash = 'custom-agent-create:instructions'; }")
    expect(instructions).to_be_focused(timeout=10_000)

    row.get_by_role("button", name="Add server").click()
    server_name = row.get_by_role("textbox", name="Name for server 1", exact=True)
    expect(row.get_by_role("textbox", name="Command for server 1", exact=True)).to_be_visible()
    server_name.fill("docs")
    # The name field keeps its own label; the server's other fields follow its name.
    expect(server_name).to_have_value("docs")
    expect(row.get_by_role("textbox", name="Command for docs", exact=True)).to_be_visible()
    row.get_by_role("button", name="Remove docs").click()
    expect(server_name).to_have_count(0)
