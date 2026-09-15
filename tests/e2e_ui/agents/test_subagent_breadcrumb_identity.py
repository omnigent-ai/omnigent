"""E2E: a sub-agent session's header breadcrumb identifies the sub-agent.

A child session dispatched from a parent bundle (``sys_session_send``, or a
direct create with ``sub_agent_name``) is bound to the PARENT bundle's agent
row, so the bound agent's name is the parent's (e.g. ``joke_director``).
The header breadcrumb's identity segment must show the child's own
``sub_agent_name`` (e.g. ``comic_one``) — before the fix it rendered the
bound agent's name, so every sub-agent masqueraded as its parent and the
generic "Sub-agent" caption was gone too.

This is the lightweight half of the nightly journey in
``test_subagent_navigation.py``: instead of a full dispatch + relay turn, the
child is created directly through the API with ``parent_session_id`` +
``sub_agent_name`` set (the same shape the runner's spawn path posts), then
the page navigates straight into it.
"""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.agents.conftest import JokeSubagentsSession


@pytest.mark.timeout(300)
def test_child_breadcrumb_shows_subagent_name_not_parent_agent(
    page: Page,
    joke_subagents_session: JokeSubagentsSession,
) -> None:
    """The child header names the sub-agent, not the parent bundle's agent.

    :param page: Playwright page fixture.
    :param joke_subagents_session: Parent session bound to the two-comedian
        joke-director bundle (its declared sub-agents make ``sub_agent_name``
        creates valid); no LLM turn is dispatched.
    :returns: None.
    """
    chat = joke_subagents_session

    # The child binds to the parent's agent row — fetch its id.
    agent_resp = httpx.get(
        f"{chat.base_url}/v1/sessions/{chat.session_id}/agent",
        timeout=10.0,
    )
    agent_resp.raise_for_status()
    agent_id = agent_resp.json()["id"]

    # Create the sub-agent child the way a dispatch does: bound to the parent
    # bundle's agent, with the child's own identity in ``sub_agent_name``.
    child_resp = httpx.post(
        f"{chat.base_url}/v1/sessions",
        json={
            "agent_id": agent_id,
            "parent_session_id": chat.session_id,
            "sub_agent_name": "comic_one",
            "title": "comic_one",
        },
        timeout=10.0,
    )
    child_resp.raise_for_status()
    child_id = child_resp.json()["id"]

    page.goto(f"{chat.base_url}/c/{child_id}")

    # The child still climbs out via the parent link…
    expect(page.get_by_role("link", name="Back to parent session")).to_be_visible(timeout=30_000)

    # …and its identity segment names the SUB-AGENT. Scoped to the breadcrumb
    # nav so a stray "comic_one" elsewhere on the page can't satisfy it.
    breadcrumb = page.get_by_role("navigation", name="Conversation")
    expect(breadcrumb.get_by_text("comic_one", exact=True)).to_be_visible()
    # The parent bundle's agent name must NOT be shown as the child's identity.
    expect(breadcrumb.get_by_text("joke_director", exact=True)).not_to_be_visible()
