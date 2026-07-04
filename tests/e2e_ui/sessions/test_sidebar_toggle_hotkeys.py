"""E2E: ⌘⌥[ / ⌘⌥] toggle the left and right sidebars from the app shell.

Covers ``useSidebarToggleHotkeys`` (``web/src/hooks/useSidebarToggleHotkeys.ts``),
wired in ``AppShell``: a window-level keydown listener flips the left
(Conversations) sidebar on ⌘/Ctrl + ⌥/Alt + ``[`` and the right (Workspace)
rail on ⌘/Ctrl + ⌥/Alt + ``]``. The hook matches the physical ``e.code``
(``BracketLeft`` / ``BracketRight``) rather than the character, because ⌥ on
macOS turns ``[``/``]`` into ``“``/``‘`` — only a code match survives the
modifier. CI runs Linux chromium, so this presses the ``Control+Alt`` chord
(the hook also accepts Cmd via ``metaKey`` on macOS).

Two design points this exercises end-to-end that the unit test cannot:

- the listener is bound at the window, so the chord fires *even while the
  composer is focused* (a sibling of the ⌘↑/↓ session-switch and ⌘↵ approve
  hotkeys). We focus the composer with an unsent draft, then collapse the
  Conversations sidebar with the chord, and assert the draft is untouched —
  proving the chord toggled the panel without stealing the keystroke or
  navigating away.
- the right-rail toggle runs the shared ``toggleRightPanel`` (open-state +
  per-session persistence + URL sync), the same path the header's collapse
  button uses, so the two can't drift.

No LLM turn is needed — this is pure client-side keyboard + layout state — so
it skips the nightly/real-agent markers the approval suites carry. The seeded
session renders both rails on the desktop viewport these tests run at: the
sidebar defaults open (``min-width: 768px``), the Workspace rail defaults open
and always has content (the Agents tab is unconditional).
"""

from __future__ import annotations

from playwright.sync_api import Page, expect

_COMPOSER = "Ask the agent anything…"
_LEFT_CHORD = "Control+Alt+BracketLeft"
_RIGHT_CHORD = "Control+Alt+BracketRight"

# The left sidebar stays mounted when collapsed (it animates to width 0), so
# its open-state reads off ``data-collapsed`` rather than presence. The right
# rail unmounts entirely when closed, so it reads off the complementary role.
_CONVERSATIONS = 'aside[aria-label="Conversations"]'
_DRAFT = "an unsent draft the sidebar chord must not disturb"


def test_sidebar_toggle_hotkeys(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """⌘⌥[ flips the Conversations sidebar; ⌘⌥] flips the Workspace rail."""
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")

    conversations = page.locator(_CONVERSATIONS)
    workspace = page.get_by_role("complementary", name="Workspace")

    # Both rails default open on the desktop viewport. Wait on the Workspace
    # rail (gated on rail-content detection) so the shell is fully settled
    # before we drive the keyboard.
    expect(workspace).to_be_visible(timeout=30_000)
    expect(conversations).not_to_have_attribute("data-collapsed", "true")

    # Focus the composer and leave an unsent draft — the chord must still fire
    # from inside the text field (the whole point of binding at the window),
    # and must not consume the keystroke as composer input.
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible()
    composer.click()
    composer.fill(_DRAFT)

    # ⌘⌥[ collapses the Conversations sidebar even with the composer focused.
    page.keyboard.press(_LEFT_CHORD)
    expect(conversations).to_have_attribute("data-collapsed", "true")
    # The draft survived: the chord toggled the panel, it did not type into or
    # navigate away from the composer.
    expect(composer).to_have_value(_DRAFT)

    # ⌘⌥[ again reopens it — the binding is a plain toggle.
    page.keyboard.press(_LEFT_CHORD)
    expect(conversations).not_to_have_attribute("data-collapsed", "true")

    # ⌘⌥] collapses the Workspace rail (unmounts it via the shared
    # toggleRightPanel path) ...
    page.keyboard.press(_RIGHT_CHORD)
    expect(workspace).to_have_count(0)

    # ... and ⌘⌥] again brings it back.
    page.keyboard.press(_RIGHT_CHORD)
    expect(workspace).to_be_visible()
