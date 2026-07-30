"""E2E: the composer shows the bound custom agent's identity and model without controls."""

from __future__ import annotations

from playwright.sync_api import Page, expect


def test_agent_picker_shows_bound_agent(
    page: Page,
    seeded_session: tuple[str, str],
    extra_agent: str,
) -> None:
    """The read-only label shows the bound model; no dropdown, no config gear.

    When a session is bound to an agent, the runner is tied 1:1 to it and
    switching is impossible — so the composer shows a read-only ``<Model>``
    label rather than a dropdown. This custom web agent exposes no switchable
    knob (no model picker, no effort, not routable), so the config gear is
    absent too. ``extra_agent`` confirms global agents do not leak into the
    bound session.

    Starts from ``/c/<id>`` instead of ``/`` because the home route no
    longer renders a composer — see :func:`seeded_session`.
    """
    base_url, session_id = seeded_session
    del extra_agent  # registered for side effect only
    page.goto(f"{base_url}/c/{session_id}")

    # The read-only label names the bound model (gpt-4o-mini). Model/effort
    # switching lives in the config gear modal, not a dropdown here.
    label = page.get_by_test_id("composer-model-effort-label")
    expect(label).to_be_visible()
    expect(label).to_contain_text("gpt-4o-mini")

    # The old dropdown trigger is gone.
    expect(page.get_by_test_id("agent-picker-trigger")).to_have_count(0)

    # No switchable knob → no config gear on this session.
    expect(page.get_by_test_id("composer-config-gear")).to_have_count(0)
