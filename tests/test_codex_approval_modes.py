"""Unit tests for the codex-native ``/permissions`` presets."""

from __future__ import annotations

import pytest

from omnigent.codex_approval_modes import (
    CODEX_NATIVE_PERMISSION_PRESETS,
    CODEX_NATIVE_PERMISSION_VALUES,
    codex_permission_preset,
    codex_permission_preset_from_thread_settings,
    codex_permission_switch_confirmed,
    codex_permissions_menu,
)

# Real ``threadSettings`` payloads captured from codex-cli 0.146.0's
# ``thread/settings/updated`` for each /permissions preset (trimmed to the
# approval fields the mapper reads).
_ASK_FOR_APPROVAL_SETTINGS = {
    "approvalPolicy": "on-request",
    "approvalsReviewer": "user",
    "sandboxPolicy": {"type": "workspaceWrite"},
    "activePermissionProfile": {"id": ":workspace", "extends": None},
}
_APPROVE_FOR_ME_SETTINGS = {
    "approvalPolicy": "on-request",
    "approvalsReviewer": "auto_review",
    "sandboxPolicy": {"type": "workspaceWrite"},
    "activePermissionProfile": {"id": ":workspace", "extends": None},
}
_FULL_ACCESS_SETTINGS = {
    "approvalPolicy": "never",
    "approvalsReviewer": "user",
    "sandboxPolicy": {"type": "dangerFullAccess"},
    "activePermissionProfile": {"id": ":danger-full-access", "extends": None},
}

# Real captured ``/permissions`` popup snapshots (verbatim from codex render
# snapshots). Row order and presence vary by platform/feature/variant, which is
# why the selecting digit is parsed from the popup rather than hardcoded.

# Permission-profiles popup variant: all four rows, `` (current)`` on row 1.
_PROFILE_POPUP = """\
  Update Model Permissions


› 1. Ask for approval (current)  Read and edit workspace files and run commands,
                                 with approval required for internet access or
                                 edits outside the workspace
  2. Approve for me              Only ask for actions detected as potentially
                                 unsafe
  3. Full Access                 Use with caution: Codex can edit files outside
                                 this workspace and access the internet without
                                 approval
  4. Read Only                   Read workspace files, with approval required
                                 for edits or internet access

  enter select · esc back
"""

# Default macOS/Linux popup with Guardian off: two rows, NO Read Only.
_LINUX_DEFAULT_POPUP = """\
  Update Model Permissions


› 1. Ask for approval  Read and edit workspace files and run commands, with
                       approval required for internet access or edits outside
                       the workspace
  2. Full Access       Use with caution: Codex can edit files outside this
                       workspace and access the internet without approval

  enter select · esc back
"""

# Windows popup: Read Only present as row 1, with `` (current)``.
_WINDOWS_POPUP = """\
› 1. Read Only (current)  Read workspace files, with approval required for edits
                          or internet access
  2. Ask for approval     Read and edit workspace files and run commands, with
                          approval required for internet access or edits outside
  3. Full Access          Use with caution: Codex can edit files outside this
                          workspace and access the internet without approval

  enter select · esc back
"""


def test_values_match_the_preset_list() -> None:
    """The value set is exactly the presets' values."""
    assert {p.value for p in CODEX_NATIVE_PERMISSION_PRESETS} == CODEX_NATIVE_PERMISSION_VALUES


def test_only_full_access_needs_confirm() -> None:
    """Full Access is the one preset with a confirm sub-dialog."""
    confirming = [p.value for p in CODEX_NATIVE_PERMISSION_PRESETS if p.needs_confirm]
    assert confirming == ["full-access"]


@pytest.mark.parametrize("value", sorted(CODEX_NATIVE_PERMISSION_VALUES))
def test_lookup_round_trips(value: str) -> None:
    """codex_permission_preset resolves every known value."""
    preset = codex_permission_preset(value)
    assert preset is not None
    assert preset.value == value


def test_lookup_unknown_is_none() -> None:
    """An unknown value resolves to None (rejected upstream)."""
    assert codex_permission_preset("bypass") is None
    assert codex_permission_preset("turbo") is None


@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        (_ASK_FOR_APPROVAL_SETTINGS, "ask-for-approval"),
        (_APPROVE_FOR_ME_SETTINGS, "approve-for-me"),
        (_FULL_ACCESS_SETTINGS, "full-access"),
        # Read-only signature (camelCase sandbox type, as the notification emits).
        (
            {
                "approvalPolicy": "on-request",
                "approvalsReviewer": "user",
                "sandboxPolicy": {"type": "readOnly"},
                "activePermissionProfile": {"id": ":read-only", "extends": None},
            },
            "read-only",
        ),
    ],
)
def test_preset_from_thread_settings_maps_each_popup_choice(settings: dict, expected: str) -> None:
    """Each /permissions choice's threadSettings maps back to its preset value."""
    assert codex_permission_preset_from_thread_settings(settings) == expected


def test_preset_from_thread_settings_none_for_unmapped() -> None:
    """A non-mapping / custom payload resolves to None rather than guessing."""
    assert codex_permission_preset_from_thread_settings(None) is None
    assert codex_permission_preset_from_thread_settings({}) is None


def test_parse_profile_popup_all_four_rows() -> None:
    """The profile-popup variant parses all four rows, stripping `` (current)``."""
    assert codex_permissions_menu(_PROFILE_POPUP) == {
        "Ask for approval": "1",
        "Approve for me": "2",
        "Full Access": "3",
        "Read Only": "4",
    }


def test_parse_linux_default_popup_two_rows() -> None:
    """The default macOS/Linux popup (Guardian off) parses its two rows."""
    assert codex_permissions_menu(_LINUX_DEFAULT_POPUP) == {
        "Ask for approval": "1",
        "Full Access": "2",
    }


@pytest.mark.parametrize("pane", ["", "some unrelated transcript text\nno menu here"])
def test_parse_non_popup_is_empty(pane: str) -> None:
    """A pane without option rows parses to an empty list."""
    assert codex_permissions_menu(pane) == {}


def test_menu_digit_linux_default_read_only_absent() -> None:
    """Read Only is absent from the default macOS/Linux popup → no digit."""
    menu = codex_permissions_menu(_LINUX_DEFAULT_POPUP)
    assert menu.get("Read Only") is None
    assert menu["Full Access"] == "2"


def test_menu_digit_windows_read_only_first() -> None:
    """On Windows, Read Only is row 1 (with a `` (current)`` suffix)."""
    assert codex_permissions_menu(_WINDOWS_POPUP)["Read Only"] == "1"


@pytest.mark.parametrize(
    "message",
    [
        "• Permissions updated to Full Access",
        "• Permission selection requested: Full Access",
    ],
)
def test_permission_switch_confirmation_messages(message: str) -> None:
    """Both Codex confirmation spellings identify the selected label."""
    assert codex_permission_switch_confirmed(message, "Full Access")
    assert not codex_permission_switch_confirmed(message, "Read Only")
