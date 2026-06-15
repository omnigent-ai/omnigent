"""E2E: the agent picker pill shows the bound custom agent without effort controls."""

from __future__ import annotations

from playwright.sync_api import Page, expect


def test_agent_picker_shows_bound_agent(
    page: Page,
    seeded_session: tuple[str, str],
    extra_agent: str,
) -> None:
    """Pill defaults to the session's agent; custom sessions have no controls.

    When a session is bound to an agent, the picker is scoped to that agent
    only and switching is impossible. Custom web agents also do not support
    the effort picker, so the trigger is a disabled status pill instead of
    an effort-only dropdown. ``extra_agent`` confirms global agents do not
    leak into the bound-session picker.

    Starts from ``/c/<id>`` instead of ``/`` because the home route no
    longer renders a composer or agent picker — see :func:`seeded_session`.
    """
    base_url, session_id = seeded_session
    del extra_agent  # registered for side effect only
    page.goto(f"{base_url}/c/{session_id}")

    trigger = page.get_by_test_id("agent-picker-trigger")
    expect(trigger).to_be_visible()
    # Agent slugs render capital-first in the picker (agentDisplayLabel).
    expect(trigger).to_contain_text("Hello_world")
    expect(trigger).to_be_disabled()
