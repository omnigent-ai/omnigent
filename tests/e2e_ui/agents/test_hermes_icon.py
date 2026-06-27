"""UI journey: a Hermes agent renders the Hermes glyph in the agent picker.

PR #1441 added the native Hermes coding agent and its caduceus glyph
(``components/icons/HermesIcon.tsx``), wiring it into ``AgentCard.tsx`` so a
``hermes-native`` agent reads as Hermes instead of falling through to the
generic bot. The icon choice is the user-visible behavior: ``iconForAgent``
resolves the Hermes glyph whenever the catalog entry's harness contains
"hermes" (or its native name matches), independent of the prettified display
name.

This test pins that path end-to-end through the real SPA. It registers a
``hermes-native`` agent so ``GET /v1/agents`` surfaces it, opens the
Add-agent picker (which renders one ``AgentCard`` per catalog agent), and
asserts the Hermes card carries the ``hermes-icon`` marker while the
non-Hermes ``hello_world`` card does not — proof the Hermes branch fired and
stayed selective rather than every card defaulting to one glyph.

No LLM turn is involved — the picker is pure catalog + render plumbing — so
this stays a fast, deterministic check that spends no model call. It is the
e2e companion to the ``AgentCard.test.tsx`` unit test, which stubs the icon
modules to assert the same branching under jsdom.
"""

from __future__ import annotations

import re

import httpx
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import open_right_rail

_ADD_AGENT_BUTTON = '[data-testid="add-agent-button"]'
_ADD_AGENT_DIALOG = '[data-testid="add-agent-dialog"]'
_HERMES_ICON = '[data-testid="hermes-icon"]'


def _hello_world_agent_id(base_url: str) -> str:
    """Return the ``hello_world`` built-in agent's id from ``GET /v1/agents``.

    The picker keys each card on the agent id (``agent-card-<id>``), so the
    test resolves the id from the catalog rather than guessing the SPA's
    prettified display name.

    :param base_url: Spawned server base URL.
    :returns: The ``hello_world`` agent id.
    """
    resp = httpx.get(f"{base_url}/v1/agents", timeout=10.0)
    resp.raise_for_status()
    agents = resp.json().get("data", resp.json())
    for agent in agents:
        if agent.get("name") == "hello_world":
            return str(agent["id"])
    raise AssertionError(f"hello_world not in agent catalog: {agents}")


def test_hermes_agent_card_shows_hermes_icon(
    page: Page,
    seeded_session: tuple[str, str],
    hermes_agent: str,
) -> None:
    """The Hermes agent's picker card renders the Hermes glyph, not the bot.

    Opens the Add-agent dialog and asserts the ``hermes-native`` card carries
    the ``hermes-icon`` marker while a non-Hermes (``hello_world``) card does
    not — pinning the harness→glyph branch in ``AgentCard.iconForAgent``.
    """
    base_url, session_id = seeded_session
    hermes_id = hermes_agent
    hello_world_id = _hello_world_agent_id(base_url)

    # The catalog must actually report the native harness, or the icon branch
    # the test pins would never fire (it would be a vacuous pass).
    resp = httpx.get(f"{base_url}/v1/agents", timeout=10.0)
    resp.raise_for_status()
    catalog = {a["id"]: a for a in resp.json().get("data", resp.json())}
    assert catalog[hermes_id]["harness"] == "hermes-native", catalog[hermes_id]

    page.goto(f"{base_url}/c/{session_id}")

    # The Add-agent button lives in the Agents rail panel, so open the rail and
    # select that tab to mount the panel (and its dialog).
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name=re.compile("^Agents")).click()

    # The trigger is a visually-hidden hook; dispatch a DOM click so
    # visibility doesn't gate the test the way ``.click()`` would on a
    # ``hidden`` element (mirrors test_add_subagent_dialog.py).
    add_button = page.locator(_ADD_AGENT_BUTTON)
    expect(add_button).to_be_attached(timeout=30_000)
    add_button.dispatch_event("click")

    dialog = page.locator(_ADD_AGENT_DIALOG)
    expect(dialog).to_be_visible(timeout=15_000)

    # The Hermes card renders the caduceus glyph...
    hermes_card = dialog.locator(f'[data-testid="agent-card-{hermes_id}"]')
    expect(hermes_card).to_be_visible(timeout=15_000)
    expect(hermes_card.locator(_HERMES_ICON)).to_have_count(1)

    # ...while the plain hello_world card does not (it falls back to the bot
    # glyph), so the branch is selective rather than blanketing every card.
    hello_card = dialog.locator(f'[data-testid="agent-card-{hello_world_id}"]')
    expect(hello_card).to_be_visible()
    expect(hello_card.locator(_HERMES_ICON)).to_have_count(0)
