"""UI journey: sub-agent rows and the child composer read-out show the
persisted effective model and reasoning effort.

``test_add_subagent_dialog.py`` covers *spawning* a sub-agent; this suite
covers *seeing* what it runs. The parent session (seeded hello_world) gets
a child created directly via the REST API with an explicit
``model_override`` and ``reasoning_effort`` — no LLM turn is involved, so
the check stays fast and deterministic. The test then asserts both UI
surfaces render the persisted values:

1. The parent's Agents rail lists the child with a combined model+effort
   cue (``subagent-model-effort`` badge) showing the short model name and
   the effort level.
2. Clicking the child row lands on the child session, whose composer
   model/effort read-out shows the same persisted model and effort even
   though the read-only child has no picker controls of its own.
"""

from __future__ import annotations

import json
import re

import httpx
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import _build_hello_world_bundle, open_right_rail

_SUBAGENT_ROW = '[data-testid="subagent-row"]'
_MODEL_EFFORT_CUE = '[data-testid="subagent-model-effort"]'
_COMPOSER_LABEL = '[data-testid="composer-model-effort-label"]'

# Override persisted on the child: dashed GPT spelling that the UI renders
# as "gpt-5.4" (badge) / "databricks-gpt-5.4" (composer), with effort "high".
_CHILD_MODEL = "databricks-gpt-5-4"
_CHILD_EFFORT = "high"


def _child_sessions(base_url: str, session_id: str) -> list[dict]:
    """Return the parent session's child-session rows (owner view)."""
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}/child_sessions", timeout=10.0)
    resp.raise_for_status()
    body = resp.json()
    return body.get("data", body) if isinstance(body, dict) else body


def test_subagent_model_effort_cue_shows_persisted_values(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Child created with model+effort renders both in the rail and composer."""
    base_url, parent_id = seeded_session

    bundle = _build_hello_world_bundle()
    metadata = json.dumps(
        {
            "parent_session_id": parent_id,
            "model_override": _CHILD_MODEL,
            "reasoning_effort": _CHILD_EFFORT,
            "title": "effort-cue-child",
        }
    )
    create_resp = httpx.post(
        f"{base_url}/v1/sessions",
        data={"metadata": metadata},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        timeout=30.0,
    )
    create_resp.raise_for_status()
    child_id = create_resp.json()["session_id"]
    assert child_id != parent_id

    try:
        # Server recorded the persisted metadata on the child summary.
        children = _child_sessions(base_url, parent_id)
        child_rows = {str(c.get("id")): c for c in children}
        assert child_id in child_rows, f"child {child_id} not in {list(child_rows)}"
        child = child_rows[child_id]
        assert child.get("model_override") == _CHILD_MODEL, child
        assert child.get("reasoning_effort") == _CHILD_EFFORT, child

        # (1) The parent's Agents rail shows the combined model+effort cue.
        page.goto(f"{base_url}/c/{parent_id}")
        open_right_rail(page)
        rail = page.get_by_role("complementary", name="Workspace")
        rail.get_by_role("tab", name=re.compile("^Agents")).click()
        row = rail.locator(_SUBAGENT_ROW)
        expect(row).to_have_count(1, timeout=30_000)
        cue = row.locator(_MODEL_EFFORT_CUE)
        expect(cue).to_be_visible(timeout=30_000)
        expect(cue).to_contain_text("gpt-5.4")
        expect(cue).to_contain_text(_CHILD_EFFORT)

        # (2) The child's composer read-out renders the same persisted
        # model and effort without picker controls.
        row.click()
        page.wait_for_url(re.compile(re.escape(f"/c/{child_id}")))
        label = page.get_by_test_id("composer-model-effort-label")
        expect(label).to_be_visible(timeout=30_000)
        expect(label).to_contain_text("databricks-gpt-5.4")
        expect(label).to_contain_text("High")
    finally:
        httpx.delete(f"{base_url}/v1/sessions/{child_id}", timeout=10.0)
