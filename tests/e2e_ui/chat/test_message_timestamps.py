"""E2E: wall-clock timestamps render on prompt and response bubbles.

Message timestamps ride the server's ``created_at`` clock end to end:
the live path stamps the user bubble from ``session.input.consumed``
and the assistant bubble from ``response.output_item.done``, while a
reload re-derives both from history hydration (``itemsToBlocks``). The
Settings → Appearance toggle ``ShowMessageTimestampsControl``
(``pages/SettingsPage.tsx``) writes
``localStorage["omnigent:show-message-timestamps"]`` — on by default,
and turning it off removes the stamps.

This drives all three surfaces against the rendered UI: a real turn
round-trips and both bubbles show a stamp (live path), a reload shows
the same stamps (history path), and flipping the real Switch off hides
them.
"""

from __future__ import annotations

import re

from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import configure_mock_llm

_COMPOSER = "Ask the agent anything…"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_WORKING = '[data-testid="working-indicator"]'
_USER_STAMP = '[data-role="user"] [data-testid="message-timestamp"]'
_ASSISTANT_STAMP = '[data-role="assistant"] [data-testid="message-timestamp"]'

_TOGGLE_KEY = "omnigent:show-message-timestamps"

# Locale-agnostic "looks like a clock time" check (e.g. "3:42 PM", "15:42").
_TIME_RE = re.compile(r"\d{1,2}:\d{2}")


def _send(page: Page, text: str) -> None:
    """Type *text* into the composer and click Send."""
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible()
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def test_message_timestamps_render_and_toggle_off(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """Stamps show by default on both bubbles, survive reload, and toggle off.

    1. **live turn** — after a prompt round-trips, the user bubble carries a
       stamp (from ``session.input.consumed``'s ``created_at``) and the
       assistant bubble carries one (from the finalized item's stamp).
    2. **reload** — the same stamps re-derive from history hydration.
    3. **toggle off** — flipping the real Settings → Appearance Switch
       persists the preference and removes every stamp from the transcript.
    """
    base_url, session_id = seeded_session
    configure_mock_llm(
        mock_llm_server_url, [{"text": "stamped reply"}], key="ts", match="timestamp check"
    )

    page.goto(f"{base_url}/c/{session_id}")
    _send(page, "timestamp check")

    # Turn terminal: the reply rendered and the working shimmer is gone.
    expect(page.locator(_ASSISTANT).first).to_be_visible(timeout=60_000)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=60_000)

    # 1. Live path: both bubbles show a clock-shaped stamp.
    expect(page.locator(_USER_STAMP).first).to_be_visible(timeout=15_000)
    expect(page.locator(_USER_STAMP).first).to_contain_text(_TIME_RE)
    expect(page.locator(_ASSISTANT_STAMP).first).to_be_visible(timeout=15_000)
    expect(page.locator(_ASSISTANT_STAMP).first).to_contain_text(_TIME_RE)

    # 2. History path: a cold reload re-derives the stamps from the
    #    persisted items' created_at.
    page.reload()
    expect(page.locator(_ASSISTANT).first).to_be_visible(timeout=30_000)
    expect(page.locator(_USER_STAMP).first).to_be_visible(timeout=15_000)
    expect(page.locator(_ASSISTANT_STAMP).first).to_be_visible(timeout=15_000)

    # 3. Flip the real Settings → Appearance Switch off and confirm it
    #    persists to localStorage.
    page.goto(f"{base_url}/settings/appearance")
    toggle = page.get_by_test_id("show-message-timestamps-toggle")
    expect(toggle).to_be_visible(timeout=30_000)
    expect(toggle).to_have_attribute("aria-checked", "true")
    toggle.click()
    expect(toggle).to_have_attribute("aria-checked", "false")
    stored = page.evaluate(f"() => window.localStorage.getItem('{_TOGGLE_KEY}')")
    assert stored == "false", f"toggle did not persist (got {stored!r})"

    # Back on the transcript, every stamp is gone (conditionally rendered,
    # never just hidden) while the bubbles themselves remain.
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.locator(_ASSISTANT).first).to_be_visible(timeout=30_000)
    expect(page.locator('[data-testid="message-timestamp"]')).to_have_count(0)
