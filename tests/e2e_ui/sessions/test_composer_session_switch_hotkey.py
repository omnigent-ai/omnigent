"""E2E: Cmd/Ctrl+Arrow switches sessions from the page body, but not the composer.

``useSessionSwitchHotkey`` (window keydown) steps the sidebar's ordered
sessions on Cmd/Ctrl+Up/Down. It bails when the keydown target is inside an
editable field (``textarea``, ``input``, ``[contenteditable="true"]``) so
typing in the composer keeps its native caret-to-start/end and the user isn't
yanked to another session mid-edit. The chord still navigates when focus is
outside an editable field.

This exercises both halves of that contract through the real chain the unit
tests mock out: live session list -> sidebar render order -> window keydown
handler -> client-side navigation to ``/c/{id}``.

- ``test_ctrl_arrow_does_not_switch_session_from_focused_composer``: focus the
  composer, leave a draft, press Ctrl+Down, and assert the route stays put.
  A regression that drops the editable-field guard would route away here.
- ``test_ctrl_arrow_still_switches_session_from_body_focus``: blur the
  composer so the keydown targets the body, press Ctrl+Down, and assert the
  route leaves the current session — the happy path the guard must preserve.

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


def test_ctrl_arrow_does_not_switch_session_from_focused_composer(
    page: Page,
    seeded_session_pair: tuple[str, str, str],
) -> None:
    """Typing in the composer, then Ctrl+↓, stays on the current session."""
    base_url, session_a, session_b = seeded_session_pair
    _set_title(base_url, session_a, "e2e-switch-a")
    _set_title(base_url, session_b, "e2e-switch-b")

    page.goto(f"{base_url}/c/{session_a}")

    # Both sessions must be present in the sidebar for the hotkey to have a
    # step target — guaranteeing that staying put is the guard's doing, not an
    # empty list.
    expect(page.locator(f'a[href="/c/{session_a}"]')).to_be_visible(timeout=30_000)
    expect(page.locator(f'a[href="/c/{session_b}"]')).to_be_visible()

    # Put focus in the composer and leave an unsent draft — this is the exact
    # condition under which the editable-field guard must suppress the chord.
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible()
    composer.click()
    composer.fill("an unsent draft that must not trigger a session switch")

    # ControlOrMeta maps to the real platform modifier (Cmd on macOS, Ctrl
    # elsewhere); CI runs Linux chromium, so this is Ctrl+Down. The keydown
    # targets the focused composer, so the guard returns early.
    page.keyboard.press("ControlOrMeta+ArrowDown")

    # The SPA navigates synchronously in the keydown handler; with the guard,
    # it never fires. Wait long enough that an unguarded chord would have
    # routed, then confirm we stayed on session_a.
    page.wait_for_timeout(500)
    expect(page).to_have_url(f"{base_url}/c/{session_a}")


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

    # Move focus off the composer so the editable-field guard doesn't swallow
    # the chord (the session page autofocuses the composer on load).
    page.evaluate(
        "() => { const el = document.activeElement; "
        "if (el && typeof el.blur === 'function') el.blur(); }"
    )

    # Dispatch the chord at the body; the keydown bubbles to the window hook,
    # the guard sees a non-editable target, and we navigate to a neighbor.
    page.locator("body").press("ControlOrMeta+ArrowDown")

    # Assert we left session_a for another /c/ route. We check "switched away"
    # rather than a hard-coded target id: the suite shares one server across
    # tests, so the sidebar may hold sessions beyond this pair — but
    # navigating at all from body focus is the behavior the guard preserves.
    expect(page).not_to_have_url(f"{base_url}/c/{session_a}", timeout=10_000)
    assert "/c/" in page.url and session_a not in page.url, (
        f"expected to switch to another session, still at {page.url}"
    )
