"""E2E: the composer permission picker must mark the session's current mode.

A claude-native session in ``auto`` permission mode shows "Auto" on
the composer's permission chip, but opening the chip's dropdown renders every
switchable mode as an unmarked row -- nothing tells the user which mode the
session is currently in. The current mode's row must carry a checked/selected
marker, like the composer's other current-value menus (the model picker's rows
are ``menuitemcheckbox`` entries whose current row is ``aria-checked="true"``).

Journey (mirrors the report): open a claude-native session whose permission
mode is Auto -> the composer permission chip reads "Auto" -> open the picker
-> the "Auto" row must be marked as the current mode. On the buggy build the
menu renders plain ``menuitem`` rows with no checked state at all, so the
``aria-checked`` expectation on the current row is the failing assertion.

The claude-native precondition is established with real server data: the
session row gets the same ``omnigent.wrapper`` / permission-mode labels the
``omnigent claude`` wrapper stamps (via the public ``PATCH /v1/sessions/{id}``
labels upsert), so the SPA renders the picker from an unpatched snapshot --
no browser route interception.
"""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import Page, expect

# The four shift+tab-reachable modes the picker offers (see
# CLAUDE_NATIVE_SWITCHABLE_PERMISSION_MODES in
# web/src/lib/claudePermissionMode.ts).
_SWITCHABLE_MODES = ["default", "auto", "acceptEdits", "plan"]
_CURRENT_MODE = "auto"
_CURRENT_MODE_LABEL = "Auto"
# The menu rows render together with the menu itself, so once the menu is open
# a short window is enough for a checked marker to appear; this keeps the
# failing run (and its recording) tight when the marker is missing.
_CHECKED_TIMEOUT_MS = 8_000


def _stamp_claude_native_labels(base_url: str, session_id: str, mode: str) -> None:
    """Label the session as a claude-native wrapper session in *mode*.

    Uses the sessions API's labels upsert to stamp the same two labels the
    ``omnigent claude`` wrapper writes: the wrapper marker that makes the SPA
    treat the session as claude-native, and the permission-mode label the
    server re-stamps after every confirmed mode switch.

    :param base_url: Live server base URL.
    :param session_id: Session to label.
    :param mode: Wire-format permission mode, e.g. ``"auto"``.
    """
    resp = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={
            "labels": {
                "omnigent.wrapper": "claude-code-native-ui",
                "omnigent.claude_native.permission_mode": mode,
            }
        },
        timeout=10.0,
    )
    resp.raise_for_status()


@pytest.mark.timeout(240)
def test_permission_picker_marks_current_mode(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The open permission picker marks the session's current mode as selected.

    Control legs first: the chip itself knows the current mode (it reads
    "Auto") and the open menu lists all four switchable modes, proving the
    labels -> snapshot -> picker journey works in this session. Then the row
    for the current mode must expose a checked/selected state; on the buggy
    build no row carries any, and that expectation is the failing assertion.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a real
        server-backed session; its row is labelled claude-native in ``auto``
        mode before the page loads.
    """
    base_url, session_id = seeded_session
    _stamp_claude_native_labels(base_url, session_id, _CURRENT_MODE)

    page.goto(f"{base_url}/c/{session_id}")

    # Control: the composer chip resolves and shows the current mode.
    chip = page.get_by_test_id("composer-permission-chip")
    expect(chip).to_be_visible(timeout=15_000)
    expect(chip).to_contain_text(_CURRENT_MODE_LABEL)

    # Open the picker.
    chip.click()
    menu = page.get_by_test_id("composer-permission-menu")
    expect(menu).to_be_visible()

    # Control: every switchable mode is offered.
    for mode in _SWITCHABLE_MODES:
        expect(page.get_by_test_id(f"composer-permission-option-{mode}")).to_be_visible()

    # The bug: the row for the mode the session is *currently* in must be
    # marked selected. The composer's other current-value menus expose this
    # as a checked menu item (aria-checked="true"); on the buggy build every
    # row is a plain menuitem with no checked state at all.
    current_row = page.get_by_test_id(f"composer-permission-option-{_CURRENT_MODE}")
    expect(current_row).to_have_attribute("aria-checked", "true", timeout=_CHECKED_TIMEOUT_MS)

    # And only that row: the other modes must not read as current.
    for mode in _SWITCHABLE_MODES:
        if mode == _CURRENT_MODE:
            continue
        row = page.get_by_test_id(f"composer-permission-option-{mode}")
        expect(row).not_to_have_attribute("aria-checked", "true")
