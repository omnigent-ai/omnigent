"""UI journey for the model and reasoning-effort cue on child sessions."""

from __future__ import annotations

import re

import httpx
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import open_right_rail

_SUBAGENT_ROW = '[data-testid="subagent-row"]'
_MODEL_EFFORT_CUE = '[data-testid="subagent-model-effort"]'
_COMPOSER_LABEL = '[data-testid="composer-agent-config-value"]'
_CHILD_MODEL = "databricks-gpt-5-4"
_CHILD_EFFORT = "high"


def test_subagent_model_effort_cue_shows_persisted_values(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A child’s effective model and effort appear in the Agents rail and composer."""
    base_url, parent_id = seeded_session
    parent_response = httpx.get(f"{base_url}/v1/sessions/{parent_id}", timeout=10.0)
    parent_response.raise_for_status()
    parent = parent_response.json()

    create_response = httpx.post(
        f"{base_url}/v1/sessions",
        json={
            "agent_id": parent["agent_id"],
            "parent_session_id": parent_id,
            "model_override": _CHILD_MODEL,
            "reasoning_effort": _CHILD_EFFORT,
            "title": "effort-cue-child",
        },
        timeout=30.0,
    )
    create_response.raise_for_status()
    child_id = create_response.json()["id"]
    assert child_id != parent_id

    try:
        page.goto(f"{base_url}/c/{parent_id}")
        open_right_rail(page)
        rail = page.get_by_role("complementary", name="Workspace")
        rail.get_by_role("tab", name=re.compile("^Agents")).click()
        row = rail.locator(_SUBAGENT_ROW)
        expect(row).to_have_count(1, timeout=30_000)
        cue = row.locator(_MODEL_EFFORT_CUE)
        expect(cue).to_be_visible(timeout=30_000)
        expect(cue).to_contain_text("gpt-5-4")
        expect(cue).to_contain_text(_CHILD_EFFORT)

        row.click()
        page.wait_for_url(re.compile(re.escape(f"/c/{child_id}")))
        composer_label = page.locator(_COMPOSER_LABEL)
        expect(composer_label).to_be_visible(timeout=30_000)
        expect(composer_label).to_contain_text("gpt")
        expect(composer_label).to_contain_text("High")
    finally:
        httpx.delete(f"{base_url}/v1/sessions/{child_id}", timeout=10.0)
