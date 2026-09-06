"""E2E: the Settings → Appearance "Reset to defaults" button keeps breathing room.

In ``AppearanceSection`` (``pages/SettingsPage.tsx``) the appearance controls
live in a ``flex flex-col gap-8`` column — a 32px vertical rhythm between rows
— while the "Reset to defaults" button sits in a *sibling* wrapper below that
column. When that wrapper only carries ``mt-4`` (16px), the button reads as
cramped against the last control above it: half the spacing of the rest of the
section.

This test measures the *rendered* vertical gap between the bottom of the controls
column and the top of the reset-button wrapper — the exact whitespace a user sees
— and asserts it is at least a comfortable margin. With the buggy ``mt-4`` the
gap is 16px and the test FAILS; a fix that gives the button room comparable to
the section rhythm (``mt-6`` = 24px or ``mt-8`` = 32px) makes it PASS.

No LLM turn is involved — this is a pure layout journey.
"""

from __future__ import annotations

from playwright.sync_api import Page, expect

# The reset wrapper must clear the last control by at least this much. The bug
# ships 16px (``mt-4``); a comfortable margin is >= 24px (``mt-6``) — so this
# threshold fails on the bug and passes on a reasonable fix without pinning the
# exact spacing token.
MIN_RESET_TOP_GAP_PX = 24


def _open_appearance(page: Page, base_url: str) -> None:
    """Navigate to Settings → Appearance and wait for the reset button to mount."""
    page.goto(f"{base_url}/settings/appearance")
    expect(page.get_by_test_id("reset-appearance-button")).to_be_visible(timeout=30_000)


def _reset_top_gap_px(page: Page) -> float:
    """Rendered vertical gap (px) between the controls column and the reset wrapper.

    The reset ``<Button>`` is rendered as the ``DialogTrigger`` child, so its
    nearest ``div`` ancestor is the spacing wrapper (``mt-4`` today). That
    wrapper's previous element sibling is the ``flex flex-col gap-8`` controls
    column. The gap between them is the whitespace a user perceives above the
    button.
    """
    return page.get_by_test_id("reset-appearance-button").evaluate(
        "(button) => {"
        "  const wrapper = button.closest('div');"
        "  const controls = wrapper.previousElementSibling;"
        "  if (!controls) throw new Error('controls column sibling not found');"
        "  return wrapper.getBoundingClientRect().top"
        "       - controls.getBoundingClientRect().bottom;"
        "}"
    )


def test_appearance_reset_button_has_top_margin(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """The "Reset to defaults" button is not crammed against the controls above it.

    The reset wrapper's top margin must give the button breathing room comparable
    to the ``gap-8`` control rhythm. The assertion requires a comfortable margin
    (>= 24px), which the cramped layout does not meet.
    """
    base_url, _session_id = seeded_session
    _open_appearance(page, base_url)

    gap = _reset_top_gap_px(page)
    assert gap >= MIN_RESET_TOP_GAP_PX, (
        "The 'Reset to defaults' button is cramped against the appearance controls: "
        f"only {gap:.0f}px of top margin, expected >= {MIN_RESET_TOP_GAP_PX}px "
        "of breathing room."
    )
