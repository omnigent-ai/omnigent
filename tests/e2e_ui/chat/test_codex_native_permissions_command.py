"""E2E: ``/permissions`` in a real codex-native session opens the composer's picker.

Sent as text, the command would open Codex's own permissions popup in the
terminal. The composer must send nothing, open its approval-mode picker, and
apply the pick through the live switch, which the runner confirms with Codex.
"""

from __future__ import annotations

import json
import os
import shutil
import time

import pytest
from playwright.sync_api import Page, expect

from omnigent.codex_approval_modes import codex_permission_switch_confirmed
from tests.e2e_ui.chat.test_claude_native_permission_switch_from_dont_ask import (
    _render_terminal,
    _terminal_id,
    _wait_for_footer,
)

_TARGET_MODE = "approve-for-me"
_TARGET_LABEL = "Approve for me"
_COMPOSER_PLACEHOLDER = "Send a message…"
# Codex CLI boot + first TUI paint.
_TUI_TIMEOUT_S = 120.0
# The runner drives Codex's popup and waits for its confirmation.
_SWITCH_TIMEOUT_MS = 60_000


def _wait_for_codex_confirmation(base_url: str, session_id: str, label: str) -> None:
    """Poll Codex's rendered screen until it confirms the switch to *label*."""
    deadline = time.monotonic() + 30.0
    screen = ""
    while time.monotonic() < deadline:
        terminal_id = _terminal_id(base_url, session_id)
        if terminal_id is not None:
            screen = _render_terminal(base_url, session_id, terminal_id)
            if codex_permission_switch_confirmed(screen, label):
                return
        time.sleep(1.0)
    raise AssertionError(f"Codex never confirmed {label!r}; last screen:\n{screen}")


@pytest.mark.nightly
@pytest.mark.timeout(420)
@pytest.mark.skipif(
    not shutil.which("codex") or not shutil.which("tmux"), reason="codex and tmux are required"
)
def test_permissions_command_opens_codex_approval_picker(
    request: pytest.FixtureRequest,
    native_codex_mock_session: tuple[str, str],
) -> None:
    """Pick an approval mode from ``/permissions``; nothing is sent to Codex."""
    base_url, session_id = native_codex_mock_session
    _wait_for_footer(base_url, session_id, "codex", timeout_s=_TUI_TIMEOUT_S)

    page: Page = request.getfixturevalue("page")
    recording = bool(os.environ.get("OMNIGENT_E2E_RECORD_DIR"))
    session_posts: list[str] = []
    page.on(
        "request",
        lambda r: (
            session_posts.append(r.url)
            if r.method == "POST" and f"/v1/sessions/{session_id}" in r.url
            else None
        ),
    )
    page.goto(f"{base_url}/c/{session_id}")
    chip = page.get_by_test_id("composer-permission-chip")
    expect(chip).to_be_visible(timeout=60_000)
    composer = page.get_by_placeholder(_COMPOSER_PLACEHOLDER)
    composer.click()
    composer.press_sequentially("/permissions", delay=60)
    expect(page.get_by_test_id("slash-menu-item-permissions")).to_be_visible()
    if recording:
        page.wait_for_timeout(1_500)
    composer.press("Enter")

    expect(page.get_by_test_id("composer-permission-menu")).to_be_visible()
    expect(composer).to_have_value("")
    if recording:
        page.wait_for_timeout(1_500)
    with page.expect_response(
        lambda r: r.request.method == "PATCH" and r.url.endswith(f"/v1/sessions/{session_id}"),
        timeout=_SWITCH_TIMEOUT_MS,
    ) as switched:
        page.get_by_test_id(f"composer-permission-option-{_TARGET_MODE}").click()
    response = switched.value
    if response.status != 200:
        pytest.fail(f"/permissions switch rejected: {response.text()}")
    assert json.loads(response.request.post_data or "{}") == {"approval_mode": _TARGET_MODE}
    expect(chip).to_contain_text(_TARGET_LABEL)
    assert session_posts == []
    # The runner types its own /permissions into Codex to apply the switch, so
    # check Codex's confirmation rather than the command's absence.
    _wait_for_codex_confirmation(base_url, session_id, _TARGET_LABEL)

    # End on Codex's own pane: the switch applied without a stray command.
    page.get_by_test_id("view-mode-terminal").click()
    expect(page.get_by_test_id("terminal-view").last).to_be_visible(timeout=30_000)
    if recording:
        page.wait_for_timeout(2_500)
