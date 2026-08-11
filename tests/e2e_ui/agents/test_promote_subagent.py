"""UI journey: promote an idle sub-agent into a top-level session.

The child is seeded through the public session API so this test stays fast and
LLM-free. Playwright covers the user-facing contract: the owner opens the
child's action menu, confirms promotion, and sees the child leave the Agents
tree and appear in the top-level Sessions list.
"""

from __future__ import annotations

import re

import httpx
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import open_right_rail

_SUBAGENT_ROW = '[data-testid="subagent-row"]'
_PROMOTION_DIALOG = '[data-testid="promote-agent-dialog"]'


def _create_child(base_url: str, parent_id: str, title: str) -> str:
    """Create an idle child under ``parent_id`` and return its session id."""
    agent = httpx.get(f"{base_url}/v1/sessions/{parent_id}/agent", timeout=10.0)
    agent.raise_for_status()
    response = httpx.post(
        f"{base_url}/v1/sessions",
        json={
            "agent_id": agent.json()["id"],
            "parent_session_id": parent_id,
            "title": title,
        },
        timeout=10.0,
    )
    response.raise_for_status()
    return str(response.json()["id"])


def test_promote_subagent_from_agents_panel(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Confirming promotion moves the child from the tree to the sidebar."""
    base_url, parent_id = seeded_session
    child_title = "ui:hello_world:promotion-e2e"
    promoted_title = "hello_world: promotion-e2e"
    child_id = _create_child(base_url, parent_id, child_title)

    try:
        page.goto(f"{base_url}/c/{parent_id}")
        open_right_rail(page)
        rail = page.get_by_role("complementary", name="Workspace")
        rail.get_by_role("tab", name=re.compile("^Agents")).click()

        child_row = rail.locator(f'{_SUBAGENT_ROW}[data-child-session-id="{child_id}"]')
        expect(child_row).to_be_visible(timeout=30_000)

        rail.get_by_role("button", name="Actions for promotion-e2e").click()
        page.get_by_role("menuitem", name="Promote to session").click()

        dialog = page.locator(_PROMOTION_DIALOG)
        expect(dialog).to_be_visible()
        expect(dialog).to_contain_text("Its sub-agents will move with it")

        with page.expect_response(
            lambda response: (
                response.request.method == "POST"
                and response.url.endswith(f"/v1/sessions/{child_id}/promote")
            ),
            timeout=30_000,
        ) as promotion:
            dialog.get_by_role("button", name="Promote", exact=True).click()
        assert promotion.value.status == 200, promotion.value.text()

        expect(child_row).to_have_count(0, timeout=30_000)
        sidebar = page.get_by_role("complementary", name="Conversations")
        expect(sidebar.get_by_role("link", name=promoted_title, exact=True)).to_be_visible(
            timeout=30_000
        )

        promoted = httpx.get(f"{base_url}/v1/sessions/{child_id}", timeout=10.0)
        promoted.raise_for_status()
        assert promoted.json()["parent_session_id"] is None
        assert promoted.json()["root_conversation_id"] == child_id
        assert promoted.json()["title"] == promoted_title
    finally:
        httpx.delete(f"{base_url}/v1/sessions/{child_id}", timeout=10.0)
