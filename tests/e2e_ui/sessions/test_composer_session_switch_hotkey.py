"""E2E: Cmd/Ctrl+Arrow switches sessions, composer focus included.

``useSessionSwitchHotkey`` (window keydown) steps the sidebar's ordered
sessions on Cmd/Ctrl+Up/Down. It used to bail whenever the keydown target sat
in an editable field, which killed the chord in the composer — the one place
users spend their time, and the exact case the hotkey was meant to serve (the
composer already declines modified arrows so this window hook can have them).
It now yields on ``defaultPrevented`` instead, so a widget that genuinely
claimed the chord for its own list navigation still wins.

This exercises the contract through the real chain the unit tests mock out:
live session list -> sidebar render order -> window keydown handler ->
client-side navigation to ``/c/{id}``.

- ``test_ctrl_arrow_switches_session_from_focused_composer``: focus the
  composer, leave a draft, press Ctrl+Down, and assert the route moved and the
  draft survives the round trip. A regression that restored the editable-field
  guard would stay put here.
- ``test_ctrl_arrow_still_switches_session_from_body_focus``: blur the
  composer so the keydown targets the body, press Ctrl+Down, and assert the
  route leaves the current session.

No LLM turn is needed — pure client-side keyboard + routing — so this skips the
nightly/real-agent markers the approval suites carry. Two runner-bound
sessions come from the ``seeded_session_pair`` fixture; both render under the
sidebar's "Sessions" group, so both are in the hotkey's ordered list.
"""

from __future__ import annotations

import httpx
from playwright.sync_api import Page, expect

_COMPOSER = "Ask the agent anything…"


def _set_title(base_url: str, session_id: str, title: str) -> None:
    """Title a session via ``PATCH /v1/sessions/{id}`` so its row is legible."""
    resp = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"title": title},
        timeout=10.0,
    )
    resp.raise_for_status()


def test_ctrl_arrow_switches_session_from_focused_composer(
    page: Page,
    seeded_session_pair: tuple[str, str, str],
) -> None:
    """Typing in the composer, then Ctrl+↓, still moves to another session."""
    base_url, session_a, session_b = seeded_session_pair
    _set_title(base_url, session_a, "e2e-switch-a")
    _set_title(base_url, session_b, "e2e-switch-b")

    page.goto(f"{base_url}/c/{session_a}")

    expect(page.locator(f'a[href="/c/{session_a}"]')).to_be_visible(timeout=30_000)
    expect(page.locator(f'a[href="/c/{session_b}"]')).to_be_visible()

    # Focus the composer and leave an unsent draft — the exact condition the
    # editable-field guard used to suppress.
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible()
    composer.click()
    composer.fill("an unsent draft that must survive the switch")

    # ControlOrMeta maps to the real platform modifier (Cmd on macOS, Ctrl
    # elsewhere); CI runs Linux chromium, so this is Ctrl+Down.
    page.keyboard.press("ControlOrMeta+ArrowDown")

    # Assert "switched away" rather than a hard-coded target: the suite shares
    # one server, so the sidebar may hold sessions beyond this pair.
    expect(page).not_to_have_url(f"{base_url}/c/{session_a}", timeout=10_000)
    assert "/c/" in page.url and session_a not in page.url, (
        f"expected to switch to another session, still at {page.url}"
    )

    # Drafts are per-session, so the composer we landed on is a different one;
    # stepping back restores the draft, proving the chord navigated rather than
    # clobbering composer state.
    page.keyboard.press("ControlOrMeta+ArrowUp")
    expect(page).to_have_url(f"{base_url}/c/{session_a}", timeout=10_000)
    expect(page.get_by_placeholder(_COMPOSER)).to_have_value(
        "an unsent draft that must survive the switch"
    )


def test_ctrl_arrow_still_switches_session_from_body_focus(
    page: Page,
    seeded_session_pair: tuple[str, str, str],
) -> None:
    """With focus outside the composer, Ctrl+↓ still steps to a neighbor."""
    base_url, session_a, session_b = seeded_session_pair
    _set_title(base_url, session_a, "e2e-switch-a")
    _set_title(base_url, session_b, "e2e-switch-b")

    page.goto(f"{base_url}/c/{session_a}")

    expect(page.locator(f'a[href="/c/{session_a}"]')).to_be_visible(timeout=30_000)
    expect(page.locator(f'a[href="/c/{session_b}"]')).to_be_visible()

    # Move focus off the composer (the session page autofocuses it on load) so
    # the keydown targets the body rather than a text field.
    page.evaluate(
        "() => { const el = document.activeElement; "
        "if (el && typeof el.blur === 'function') el.blur(); }"
    )

    # Dispatch the chord at the body; the keydown bubbles to the window hook
    # and we navigate to a neighbor.
    page.locator("body").press("ControlOrMeta+ArrowDown")

    # Assert we left session_a for another /c/ route. We check "switched away"
    # rather than a hard-coded target id: the suite shares one server across
    # tests, so the sidebar may hold sessions beyond this pair.
    expect(page).not_to_have_url(f"{base_url}/c/{session_a}", timeout=10_000)
    assert "/c/" in page.url and session_a not in page.url, (
        f"expected to switch to another session, still at {page.url}"
    )
