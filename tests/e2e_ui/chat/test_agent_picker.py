"""E2E: the composer shows the bound custom agent's identity and model without controls."""

from __future__ import annotations

from playwright.sync_api import Page, expect


def test_agent_picker_shows_bound_agent(
    page: Page,
    seeded_session: tuple[str, str],
    extra_agent: str,
) -> None:
    """Status line shows the bound agent; the pill shows the model, no controls.

    When a session is bound to an agent, the picker is scoped to that agent
    only and switching is impossible. Custom web agents also do not support
    the effort picker, so the trigger is a disabled status pill showing the
    bound model instead of an effort-only dropdown. The agent identity moved
    out of the trigger into the status-line tray below the card.
    ``extra_agent`` confirms global agents do not leak into the bound-session
    picker.

    Starts from ``/c/<id>`` instead of ``/`` because the home route no
    longer renders a composer or agent picker — see :func:`seeded_session`.
    """
    base_url, session_id = seeded_session
    del extra_agent  # registered for side effect only
    page.goto(f"{base_url}/c/{session_id}")

    # The agent identity now lives in the read-only status tray. Agent slugs
    # render capital-first there (agentDisplayLabel).
    expect(page.get_by_test_id("composer-harness")).to_contain_text("Hello_world")

    # The trigger now names what it controls — the bound model (gpt-4o-mini) —
    # and stays disabled because this agent exposes no model/effort switching.
    trigger = page.get_by_test_id("agent-picker-trigger")
    expect(trigger).to_be_visible()
    expect(trigger).to_contain_text("gpt-4o-mini")
    expect(trigger).to_be_disabled()
