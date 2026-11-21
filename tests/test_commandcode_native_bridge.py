"""Unit tests for the commandcode-native pane parser (real captured screens)."""

from __future__ import annotations

from omnigent.harnesses.commandcode_native.bridge import _parse_pane

_TRUST_MODAL = """
────────────────────────────────────────────────────────────────────────────────
Do you trust the files in this folder?
/tmp

Command Code may read files in this folder.

❯ 1. Yes, proceed
  2. No, exit

↑/↓ to navigate · enter to select · esc to exit
"""

_IDLE_EDITOR = """
   ███████ ███████ ███████████
# Command Code v1.88.2
# /tmp
◼ Ran 1 session start hook

────────────────────────────────────────────────────────────────────────────────
❯ Ask your question...
────────────────────────────────────────────────────────────────────────────────
  ? for shortcuts · taste on
"""

_PERMISSION_MENU = """
Bash command
rm -rf dist

❯ 1. Yes
  2. Yes, and don't ask again for `rm` commands in this project
  3. No, and tell Command Code what to do differently

↑/↓ to navigate · enter to select
"""


def test_trust_modal_is_detected_and_not_ready() -> None:
    state = _parse_pane(_TRUST_MODAL)
    assert state.trust_modal_present is True
    assert state.menu_present is True
    # The trust modal must NOT be treated as a blocking permission prompt.
    assert state.approval_menu_present is False
    # The "❯ 1. Yes, proceed" cursor is a menu option, not the editor.
    assert state.editor_present is False
    assert state.ready is False


def test_idle_editor_is_ready() -> None:
    state = _parse_pane(_IDLE_EDITOR)
    assert state.editor_present is True
    assert state.menu_present is False
    assert state.trust_modal_present is False
    assert state.ready is True


def test_permission_menu_blocks_injection() -> None:
    state = _parse_pane(_PERMISSION_MENU)
    assert state.trust_modal_present is False
    assert state.menu_present is True
    assert state.approval_menu_present is True
    assert state.editor_present is False
    assert state.ready is False


def test_empty_pane_is_not_ready() -> None:
    state = _parse_pane("")
    assert state.ready is False
