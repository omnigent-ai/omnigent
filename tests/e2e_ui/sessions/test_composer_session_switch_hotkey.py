"""E2E: Cmd/Ctrl+Arrow traverses from empty, but not drafted, composers.

``useSessionSwitchHotkey`` (window keydown) steps the sidebar's ordered
sessions on Cmd/Ctrl+Up/Down. It bails when the keydown target is inside an
non-empty editable field (``textarea``, ``input``, or
``[contenteditable="true"]``) so typing in the composer keeps its native
caret-to-start/end and the user isn't yanked to another session mid-edit. An
empty focused editable still allows session traversal.

This exercises both halves of that contract through the real chain the unit
tests mock out: live session list -> sidebar render order -> window keydown
handler -> client-side navigation to ``/c/{id}``.

- ``test_ctrl_arrow_does_not_switch_session_from_focused_composer``: focus the
  composer, leave a draft, press Ctrl+Down, and assert the route stays put.
  A regression that drops the editable-field guard would route away here.
- ``test_empty_focused_composer_does_not_block_session_switching``: focus the
  empty composer once, press Ctrl+Down three times with no intervening focus
  changes, and assert every press navigates.

No LLM turn is needed — pure client-side keyboard + routing — so this skips the
nightly/real-agent markers the approval suites carry. Two runner-bound
sessions come from the ``seeded_session_pair`` fixture; both render under the
sidebar's "Sessions" group, so both are in the hotkey's ordered list.
"""

from __future__ import annotations

from urllib.parse import urlsplit

import httpx
from playwright.sync_api import Page, expect
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

_COMPOSER = "Ask the agent anything…"


def _set_title(base_url: str, session_id: str, title: str) -> None:
    """Title a session via ``PATCH /v1/sessions/{id}`` so its row is legible."""
    resp = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"title": title},
        timeout=10.0,
    )
    resp.raise_for_status()


def _press_down_and_expect_session_change(page: Page) -> None:
    """Press the traversal chord and require a different session URL."""
    previous_url = page.url
    previous_path = urlsplit(previous_url).path
    assert previous_path.startswith("/c/") and previous_path.count("/") == 2

    page.keyboard.press("ControlOrMeta+ArrowDown")
    try:
        page.wait_for_function(
            """previousPath => {
                const current = new URL(window.location.href);
                return current.pathname !== previousPath
                    && /^\\/c\\/[^/]+$/.test(current.pathname);
            }""",
            arg=previous_path,
            timeout=10_000,
        )
    except PlaywrightTimeoutError:
        raise AssertionError(
            f"expected a different /c/<id> after session traversal from {previous_url}"
        ) from None


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


def test_empty_focused_composer_does_not_block_session_switching(
    page: Page,
    seeded_session_pair: tuple[str, str, str],
) -> None:
    """An empty focused composer must not block repeated Ctrl+↓ traversal."""
    base_url, session_a, session_b = seeded_session_pair
    _set_title(base_url, session_a, "e2e-switch-a")
    _set_title(base_url, session_b, "e2e-switch-b")

    page.goto(f"{base_url}/c/{session_a}")

    expect(page.locator(f'a[href="/c/{session_a}"]')).to_be_visible(timeout=30_000)
    expect(page.locator(f'a[href="/c/{session_b}"]')).to_be_visible()

    # Headless Chromium does not retain ChatPage's autofocus after navigation,
    # so it cannot reproduce the full autofocus interaction chain. Focus the
    # empty composer once to test the guard invariant directly; the headed
    # product demo covers the natural flow.
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_have_value("")
    composer.focus()

    # Assert only the traversal invariant. Other sessions may exist in the
    # sidebar, so seeded sessions are not guaranteed to be adjacent.
    for _ in range(3):
        expect(composer).to_have_value("")
        _press_down_and_expect_session_change(page)
        expect(composer).to_have_value("")
