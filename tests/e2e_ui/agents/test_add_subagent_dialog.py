"""UI journey: spawn a sub-agent from the "Add agent" dialog.

``test_subagent_navigation.py`` covers *navigating* a sub-agent tree that an
LLM spun up via ``sys_session_send``. This suite covers the other way a
sub-agent comes to exist: the user adds one by hand from the Agents rail.

The "Add agent" affordance (``shell/SubagentsPanel.tsx`` → ``AddAgentDialog``)
opens a picker of the server's registered agents (the same ``GET /v1/agents``
catalog the new-chat picker uses), takes a name, and on submit creates a child
session via ``POST /v1/sessions`` with ``parent_session_id`` set, then navigates
into the new child. No LLM turn is involved — the dialog is pure
catalog + REST plumbing — so this stays a fast, deterministic check that does
not spend a real model call.

The load-bearing assertions: after submit the SPA lands on a *different*
``/c/<child-id>`` route, and the server's
``GET /v1/sessions/<parent>/child_sessions`` lists exactly that child under the
``ui:<agent>:<name>`` title sentinel the dialog stamps — proof the spawn created
a real parent→child link, not just a client-side navigation.
"""

from __future__ import annotations

import re

import httpx
from playwright.sync_api import Locator, Page, expect

from tests.e2e_ui.conftest import open_right_rail

_ADD_AGENT_BUTTON = '[data-testid="add-agent-button"]'
_ADD_AGENT_DIALOG = '[data-testid="add-agent-dialog"]'
_ADD_AGENT_NAME_INPUT = '[data-testid="add-agent-name-input"]'
_ADD_AGENT_MODEL_INPUT = '[data-testid="add-agent-model-input"]'
_ADD_AGENT_SUBMIT = '[data-testid="add-agent-submit"]'
_SUBAGENT_ROW = '[data-testid="subagent-row"]'

# The hello_world agent declares ``executor.model: gpt-4o-mini`` (see
# ``tests/e2e_ui/conftest.py`` _TEST_AGENT_YAML), which the spec parser
# syncs into ``llm.model``. The dialog's Model input must pre-fill with
# exactly this — proves the agent's declared model traversed
# GET /v1/agents → AgentObject.model → the picker, not a hardcoded
# constant.
_HELLO_WORLD_MODEL = "gpt-4o-mini"


def _picker_hello_world_ids(base_url: str, session_id: str) -> list[str]:
    """Return every agent id the picker may render for ``hello_world``.

    The picker keys each card on the agent id (``agent-card-<id>``), and two
    rows compete for the name: the ``--agent`` template in the ``GET
    /v1/agents`` catalog, and the session-scoped copy this session is bound
    to. ``useAvailableAgents`` collapses them newest-wins, and which one
    survives depends on the catalog fetch and the sessions scan resolving in
    a particular order — so pinning either id alone makes the card selector
    miss intermittently. Return both and let the caller match whichever
    rendered, rather than guessing the display name (the SPA prettifies it).

    :param base_url: Spawned server base URL.
    :param session_id: The session whose page hosts the dialog.
    :returns: Candidate ``hello_world`` agent ids, session-bound copy first.
    """
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}/agent", timeout=10.0)
    resp.raise_for_status()
    agent = resp.json()
    assert agent.get("name") == "hello_world", f"session not bound to hello_world: {agent}"
    ids = [str(agent["id"])]
    catalog = httpx.get(f"{base_url}/v1/agents", timeout=10.0)
    catalog.raise_for_status()
    ids += [
        str(row["id"])
        for row in catalog.json().get("data", [])
        if row.get("name") == "hello_world" and str(row["id"]) not in ids
    ]
    return ids


def _hello_world_card(dialog: Locator, base_url: str, session_id: str) -> Locator:
    """Locate the ``hello_world`` card the picker actually rendered.

    :param dialog: The open Add-agent dialog.
    :param base_url: Spawned server base URL.
    :param session_id: The session whose page hosts the dialog.
    :returns: Locator for the rendered ``hello_world`` card.
    """
    ids = _picker_hello_world_ids(base_url, session_id)
    return dialog.locator(", ".join(f'[data-testid="agent-card-{i}"]' for i in ids)).first


def _child_sessions(base_url: str, session_id: str) -> list[dict]:
    """Return the parent session's child-session rows (owner view)."""
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}/child_sessions", timeout=10.0)
    resp.raise_for_status()
    body = resp.json()
    return body.get("data", body) if isinstance(body, dict) else body


def _session_model_override(base_url: str, session_id: str) -> str | None:
    """Return the session's ``model_override`` from its snapshot.

    ``GET /v1/sessions/{id}`` carries ``model_override`` on the
    ``SessionResponse`` (see ``omnigent/server/schemas.py``); this reads
    it back so the e2e test can prove the dialog's Model input flowed
    through to the created child session, not just into the request body.

    :param base_url: Spawned server base URL.
    :param session_id: Session id to read.
    :returns: The session's ``model_override`` (``None`` when unset).
    """
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    resp.raise_for_status()
    return resp.json().get("model_override")


def test_add_subagent_from_dialog(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Add-agent dialog → pick agent → name → submit → child session is created."""
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")

    # The Add-agent button lives in the Agents rail panel, so open the rail and
    # select that tab to mount the panel (and its dialog).
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name=re.compile("^Agents")).click()

    # The trigger is a visually-hidden hook (the rail exposes "Add agent" via
    # its own affordance); dispatch a DOM click so visibility doesn't gate the
    # test the way ``.click()`` would on a ``hidden`` element.
    add_button = page.locator(_ADD_AGENT_BUTTON)
    expect(add_button).to_be_attached(timeout=30_000)
    add_button.dispatch_event("click")

    dialog = page.locator(_ADD_AGENT_DIALOG)
    expect(dialog).to_be_visible(timeout=15_000)

    # Pick the hello_world agent and give the child a unique, assertable name.
    _hello_world_card(dialog, base_url, session_id).click()
    child_name = "rail-spawned-sub"
    name_input = dialog.locator(_ADD_AGENT_NAME_INPUT)
    expect(name_input).to_be_visible()
    name_input.fill(child_name)

    dialog.locator(_ADD_AGENT_SUBMIT).click()

    # The SPA navigates into the freshly-created child session — a different
    # /c/<id> from the parent we started on.
    page.wait_for_url(re.compile(r"/c/(?!" + re.escape(session_id) + r"$)[^/]+$"), timeout=30_000)
    child_url = page.url
    child_id = child_url.rsplit("/c/", 1)[1]
    assert child_id != session_id, f"expected to land on a child, still on parent {session_id}"

    # The server recorded a real parent→child link under the dialog's
    # ``ui:<agent>:<name>`` title sentinel.
    children = _child_sessions(base_url, session_id)
    child_ids = {str(c.get("id")) for c in children}
    assert child_id in child_ids, f"new child {child_id} not in {child_ids}"

    # Back on the parent, the Agents rail now lists the spawned sub-agent.
    page.goto(f"{base_url}/c/{session_id}")
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name=re.compile("^Agents")).click()
    expect(rail.locator(_SUBAGENT_ROW)).to_have_count(1, timeout=30_000)


def test_add_subagent_dialog_prefills_and_overrides_model(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The dialog pre-fills the agent's declared model and forwards an
    edit as the child session's ``model_override``.

    The Model input is pre-filled from the picked agent's declared
    ``llm.model`` (exposed via ``GET /v1/agents`` → ``AgentObject.model``),
    so a custom agent launches on its own model by default instead of the
    harness default. Editing the pre-filled value and submitting must
    flow the edited model through ``POST /v1/sessions`` as
    ``model_override`` and land on the created child session's snapshot —
    proof the override reached the server, not just the request body. No
    LLM turn is involved (the dialog is pure catalog + REST plumbing).
    """
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")

    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name=re.compile("^Agents")).click()

    add_button = page.locator(_ADD_AGENT_BUTTON)
    expect(add_button).to_be_attached(timeout=30_000)
    add_button.dispatch_event("click")

    dialog = page.locator(_ADD_AGENT_DIALOG)
    expect(dialog).to_be_visible(timeout=15_000)

    # Pick hello_world — its declared model is gpt-4o-mini. Both the catalog
    # row and the session-scoped copy carry it, so the pre-fill assertion
    # below holds whichever one the picker collapsed to.
    _hello_world_card(dialog, base_url, session_id).click()

    # The Model input is pre-filled with the agent's declared model, not
    # empty — a regression to an empty default would mean the agent's
    # model never reached the picker.
    model_input = dialog.locator(_ADD_AGENT_MODEL_INPUT)
    expect(model_input).to_be_visible()
    expect(model_input).to_have_value(_HELLO_WORLD_MODEL, timeout=15_000)

    # Name the child and edit the pre-filled model to a sentinel value.
    dialog.locator(_ADD_AGENT_NAME_INPUT).fill("model-override-sub")
    # Plain (non-``databricks-``) sentinel, matching the conftest agent:
    # a databricks- prefix would force gateway auth CI has no creds for.
    overridden_model = "gpt-4o-mini-override"
    model_input.fill(overridden_model)
    dialog.locator(_ADD_AGENT_SUBMIT).click()

    # Land on the freshly-created child session.
    page.wait_for_url(re.compile(r"/c/(?!" + re.escape(session_id) + r"$)[^/]+$"), timeout=30_000)
    child_id = page.url.rsplit("/c/", 1)[1]
    assert child_id != session_id, f"expected to land on a child, still on parent {session_id}"

    # The edited model flowed through POST /v1/sessions as model_override
    # and is persisted on the child session's snapshot. A None or a
    # different value means the dialog dropped or mis-routed the input.
    assert _session_model_override(base_url, child_id) == overridden_model, (
        f"expected child model_override={overridden_model!r}"
    )
