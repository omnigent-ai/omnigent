"""E2E: a smart-routed fan-out's routing chips name the task each governed.

With subagent routing on, a 3-way native fan-out persists three
``routing_decision`` items; each must be attributable to the sub-agent/task
it governed. Without that association the three chips render identically
(same ``subagent_type``-derived label, no task description, no dispatch
link), so a fan-out's decisions cannot be individually reviewed.

The journey runs the real product path end to end minus the harness binary:
each request body is built by the actual claude-native hook payload builder
(:func:`omnigent.inner.hook_scripts.subagent_router.build_route_request`) from
a realistic Claude ``Task`` ``tool_input``, POSTed to the real server relay
route (``/v1/sessions/{id}/hooks/route-subagent``), persisted by the real
store persister, and rendered by the real SPA. Only the Claude binary that
would invoke the hook script is skipped (it needs Anthropic credentials the
suite does not have), so a fix that extends the hook payload, the persisted
item, or the chip rendering is picked up by this test automatically.
"""

from __future__ import annotations

import httpx
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import seed_committed_turn

# What the parent session runs on; the fail-open verdict inherits it, so the
# chips render a model pill either way (routed or not).
_PARENT_MODEL = "databricks-claude-sonnet-4-6"

# The fan-out: three Task spawns sharing one subagent_type — the canonical
# Claude orchestrator shape — distinguishable only by the work each carries.
_TASKS: tuple[tuple[str, str], ...] = (
    (
        "Research auth flows",
        "Research auth flows: map every login path and note which issue tokens.",
    ),
    (
        "Implement token refresh",
        "Implement token refresh: add rotation to the session middleware.",
    ),
    (
        "Review session storage",
        "Review session storage: audit cookie flags and persistence of secrets.",
    ),
)


def test_fanout_routing_chips_are_individually_attributable(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Each fan-out routing chip must be attributable to the task it governed.

    Drives the reported journey — subagent routing on, 3-way fan-out, open the
    transcript's routing feed — and asserts the report's expected behavior:
    every routing decision is associated with the sub-agent/task it governed,
    so the three chips are individually reviewable. Today all three chips are
    identical and reference no task, so this fails until the bug is fixed.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a real
        server-backed session.
    :returns: None.
    """
    from omnigent.inner.hook_scripts.subagent_router import build_route_request

    base_url, session_id = seeded_session

    # Smart routing for spawns is the session's two-state switch; sessions
    # started on Smart Routing are stamped "on" at create, and the switch is
    # PATCHable mid-session. Flip it on the same way the settings UI does.
    resp = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"subagent_routing_override": "on"},
        timeout=10.0,
    )
    resp.raise_for_status()

    # The orchestrator turn the fan-out belongs to (seeded, so the transcript
    # shows the user's ask above the routing feed without driving the LLM).
    seed_committed_turn(
        session_id,
        prompt=(
            "Fan out three sub-agents: research auth flows, implement token "
            "refresh, and review session storage."
        ),
        reply="Spawning three sub-agents now.",
    )

    # The fan-out itself: three Task spawns, reported to the routing relay
    # exactly as the claude-native PreToolUse hook reports them — the body is
    # built by the hook's own payload builder from the Task tool_input.
    for description, prompt in _TASKS:
        tool_input = {
            "subagent_type": "general-purpose",
            "description": description,
            "prompt": prompt,
        }
        body = build_route_request(
            tool_input,
            harness="claude-native",
            parent_model=_PARENT_MODEL,
        )
        hook = httpx.post(
            f"{base_url}/v1/sessions/{session_id}/hooks/route-subagent",
            json=body,
            timeout=15.0,
        )
        hook.raise_for_status()

    # All three decisions really are in the transcript — the assertions below
    # are about what the UI can show, not about rows going missing.
    items = httpx.get(f"{base_url}/v1/sessions/{session_id}/items", timeout=10.0)
    items.raise_for_status()
    decisions = [i for i in items.json()["data"] if i["type"] == "routing_decision"]
    assert len(decisions) == 3, decisions

    page.goto(f"{base_url}/c/{session_id}")
    cards = page.get_by_test_id("routing-decision-card")
    expect(cards).to_have_count(3, timeout=15_000)
    cards.first.scroll_into_view_if_needed()
    # Let hydration settle so the chips read from committed history (and a
    # recording of this run shows the settled routing feed, not a repaint).
    page.wait_for_timeout(1_500)

    # Read the chips as a user would, before any are expanded.
    texts = [cards.nth(i).inner_text() for i in range(3)]

    # Show the raw verdict of the first chip (everything the decision
    # carries) so a recording of this run documents the failure state.
    cards.first.get_by_test_id("routing-decision-raw-toggle").click()
    page.wait_for_timeout(2_000)

    # Expected (the bug report's contract): each decision is associated with
    # the sub-agent/task it governed. A chip that names its task — by the
    # Task's description or its task text — satisfies this.
    for description, _prompt in _TASKS:
        assert any(description.lower() in text.lower() for text in texts), (
            f"no routing chip is attributable to the task it governed "
            f"({description!r}); rendered chips: {texts!r}"
        )

    # ...which requires the three chips to be distinguishable at all. Today
    # they render pairwise identical.
    assert len(set(texts)) == 3, (
        f"fan-out routing chips are indistinguishable — a decision cannot be "
        f"tied to the sub-agent it governed: {texts[0]!r}"
    )
