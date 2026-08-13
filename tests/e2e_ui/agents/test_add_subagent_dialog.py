"""UI journey: spawn a sub-agent from the "Add agent" dialog.

``test_subagent_navigation.py`` covers *navigating* a sub-agent tree that an
LLM spun up via ``sys_session_send``. This suite covers the other way a
sub-agent comes to exist: the user adds one by hand from the Agents rail.

The "Add agent" affordance (``shell/SubagentsPanel.tsx`` → ``AddAgentDialog``)
opens a picker of the server's registered agents (the same ``GET /v1/agents``
catalog the new-chat picker uses), takes a name and a required first task, and
on submit creates a child session via ``POST /v1/sessions`` with
``parent_session_id`` set, queues the task, and navigates into the new child.

The load-bearing assertions: after submit the SPA lands on a *different*
``/c/<child-id>`` route, the server's
``GET /v1/sessions/<parent>/child_sessions`` lists exactly that child under the
``ui:<agent>:<name>`` title sentinel the dialog stamps (proof the spawn created
a real parent→child link), and the child's transcript carries the typed task
exactly once — proof the queued first prompt was delivered after the child
bound (the child inherits the parent's runner, so the auto-send dispatches).
"""

from __future__ import annotations

import re
import time

import httpx
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import open_right_rail

_ADD_AGENT_BUTTON = '[data-testid="add-agent-button"]'
_ADD_AGENT_DIALOG = '[data-testid="add-agent-dialog"]'
_ADD_AGENT_NAME_INPUT = '[data-testid="add-agent-name-input"]'
_ADD_AGENT_TASK_INPUT = '[data-testid="add-agent-initial-prompt-input"]'
_ADD_AGENT_SUBMIT = '[data-testid="add-agent-submit"]'
_SUBAGENT_ROW = '[data-testid="subagent-row"]'

_INITIAL_TASK = "Review the current diff and report correctness issues."


def _picker_hello_world_id(base_url: str, session_id: str) -> str:
    """Return the ``hello_world`` agent id the Add-agent picker surfaces.

    The picker keys each card on the agent id (``agent-card-<id>``). Its hook
    (``useAvailableAgents``) lets a newer same-named session upload supersede the
    ``--agent`` template — and this session is already bound to a session-scoped
    ``hello_world`` (created after the template), so the picker surfaces THAT
    copy, not the template. Resolve it from the session's bound agent so the card
    selector matches what the picker renders (rather than guessing the display
    name, which the SPA prettifies).

    :param base_url: Spawned server base URL.
    :param session_id: The session whose page hosts the dialog; its bound
        ``hello_world`` is what the picker shows.
    :returns: The ``hello_world`` agent id the picker renders.
    """
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}/agent", timeout=10.0)
    resp.raise_for_status()
    agent = resp.json()
    assert agent.get("name") == "hello_world", f"session not bound to hello_world: {agent}"
    return str(agent["id"])


def _child_sessions(base_url: str, session_id: str) -> list[dict]:
    """Return the parent session's child-session rows (owner view)."""
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}/child_sessions", timeout=10.0)
    resp.raise_for_status()
    body = resp.json()
    return body.get("data", body) if isinstance(body, dict) else body


def _child_user_prompts(base_url: str, child_id: str) -> list[str]:
    """Return the child's user-authored message texts.

    The task is delivered as a normal user message (posted to
    ``/v1/sessions/<child>/events`` by the SPA's first-prompt handoff), so it
    persists in the child's committed items regardless of the agent's reply.
    Only user-role message items are collected so an assistant echo can't
    inflate the count.

    :param base_url: Spawned server base URL.
    :param child_id: The child session id.
    :returns: The text of each user message in the child's transcript.
    """
    resp = httpx.get(
        f"{base_url}/v1/sessions/{child_id}/items",
        params={"limit": 100, "order": "desc"},
        timeout=10.0,
    )
    resp.raise_for_status()
    items = resp.json().get("data", [])
    texts: list[str] = []
    for item in items:
        if item.get("type") != "message" or item.get("role") != "user":
            continue
        for block in item.get("content", []) or []:
            text = block.get("text") if isinstance(block, dict) else None
            if text:
                texts.append(text)
    return texts


def test_add_subagent_from_dialog(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Add-agent dialog → pick agent → name → task → submit → tasked child."""
    base_url, session_id = seeded_session
    agent_id = _picker_hello_world_id(base_url, session_id)
    page.goto(f"{base_url}/c/{session_id}")

    # The Add-agent button lives in the Agents rail panel, so open the rail and
    # select that tab to mount the panel (and its dialog).
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name=re.compile("^Agents")).click()

    # The action is a visible, permission-gated button now (the seeded owner has
    # edit access), so click it directly rather than dispatching a DOM event.
    add_button = page.locator(_ADD_AGENT_BUTTON)
    expect(add_button).to_be_visible(timeout=30_000)
    add_button.click()

    dialog = page.locator(_ADD_AGENT_DIALOG)
    expect(dialog).to_be_visible(timeout=15_000)

    # Pick the hello_world agent, give the child a unique name, and enter the
    # required first task.
    dialog.locator(f'[data-testid="agent-card-{agent_id}"]').click()
    child_name = "rail-spawned-sub"
    name_input = dialog.locator(_ADD_AGENT_NAME_INPUT)
    expect(name_input).to_be_visible()
    name_input.fill(child_name)
    task_input = dialog.locator(_ADD_AGENT_TASK_INPUT)
    expect(task_input).to_be_visible()
    task_input.fill(_INITIAL_TASK)

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

    # The queued task is delivered to the child after it binds (the child
    # inherits the parent's runner, so the auto-send dispatches). Poll until it
    # first appears in the child's transcript.
    appear_deadline = time.time() + 30.0
    while time.time() < appear_deadline:
        if any(_INITIAL_TASK in p for p in _child_user_prompts(base_url, child_id)):
            break
        time.sleep(0.5)
    else:
        raise AssertionError(
            f"task never reached child {child_id}; saw {_child_user_prompts(base_url, child_id)!r}"
        )

    # Exactly-once, not just at-least-once: keep watching for a short
    # stabilization window and fail if a SECOND copy of the task shows up (a
    # duplicate auto-send from the pending-prompt handoff).
    stabilize_deadline = time.time() + 5.0
    while time.time() < stabilize_deadline:
        matching = [p for p in _child_user_prompts(base_url, child_id) if _INITIAL_TASK in p]
        assert len(matching) == 1, f"task delivered more than once: {matching!r}"
        time.sleep(0.5)

    # Back on the parent, the Agents rail now lists the spawned sub-agent.
    page.goto(f"{base_url}/c/{session_id}")
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name=re.compile("^Agents")).click()
    expect(rail.locator(_SUBAGENT_ROW)).to_have_count(1, timeout=30_000)
