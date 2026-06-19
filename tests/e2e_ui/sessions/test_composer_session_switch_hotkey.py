"""E2E: Cmd/Ctrl+↑/↓ switches sessions even while the composer is focused.

Covers the composer fix in ``ap-web/src/pages/ChatPage.tsx``: the composer's
ArrowUp/ArrowDown draft-recall handler used to intercept *any* arrow keydown,
so the global session-switch hotkey (``useSessionSwitchHotkey``, Cmd/Ctrl+↑/↓)
appeared dead while typing — recall swallowed the keystroke and replaced the
draft instead of switching sessions. The fix gates recall to UNmodified arrows
(``!metaKey && !ctrlKey && !altKey``), letting the modified chord bubble to the
window-level hotkey.

The regression is specifically "composer has focus". So this test puts focus
in the composer (types a draft), then presses Ctrl+ArrowDown (the Win/Linux
chord; CI runs Linux chromium — the hook also accepts Cmd via metaKey on
macOS) and asserts the route navigates to a different session. Pre-fix, recall
ate the chord and the route never changed.

No LLM turn is needed — this exercises pure client-side keyboard + routing —
so it skips the nightly/real-agent markers the approval suites carry. Two
runner-bound sessions come from the ``seeded_session_pair`` fixture; both
render under the sidebar's "Recent" group, so both are in the hotkey's ordered
list.
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
    """Typing in the composer, then Ctrl+↓, navigates off the current session."""
    base_url, session_a, session_b = seeded_session_pair
    _set_title(base_url, session_a, "e2e-switch-a")
    _set_title(base_url, session_b, "e2e-switch-b")

    page.goto(f"{base_url}/c/{session_a}")

    # Both sessions must be present in the sidebar for the hotkey to step
    # between them.
    expect(page.locator(f'a[href="/c/{session_a}"]')).to_be_visible(timeout=30_000)
    expect(page.locator(f'a[href="/c/{session_b}"]')).to_be_visible()

    # Put focus in the composer and leave an unsent draft — this is the exact
    # condition under which the recall handler used to swallow the chord.
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible()
    composer.click()
    composer.fill("an unsent draft that recall must not intercept")

    # Cmd/Ctrl+↓ steps to the adjacent sidebar session. With the bug, recall
    # consumes ArrowDown and the route stays put; with the fix, the chord
    # bubbles to the window hotkey and we navigate away from session_a.
    page.keyboard.press("Control+ArrowDown")

    # Assert we left the composing session for another /c/ route. We check
    # "switched away" rather than a hard-coded target id: the suite shares one
    # server across tests, so the sidebar may hold sessions beyond this pair —
    # but navigating at all from a focused composer is precisely the regression.
    expect(page).not_to_have_url(f"{base_url}/c/{session_a}", timeout=10_000)
    assert "/c/" in page.url and session_a not in page.url, (
        f"expected to switch to another session, still at {page.url}"
    )
