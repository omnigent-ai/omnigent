"""Unit tests for the codex-native ``/permissions`` presets."""

from __future__ import annotations

import pytest

from omnigent.codex_approval_modes import (
    CODEX_NATIVE_PERMISSION_PRESETS,
    CODEX_NATIVE_PERMISSION_VALUES,
    codex_permission_preset,
    codex_permission_preset_from_thread_settings,
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


def test_values_match_the_preset_list() -> None:
    """The value set is exactly the presets' values."""
    assert {p.value for p in CODEX_NATIVE_PERMISSION_PRESETS} == CODEX_NATIVE_PERMISSION_VALUES


def test_menu_keys_are_the_popup_positions() -> None:
    """menu_key mirrors the /permissions popup's 1-based option order."""
    assert [p.menu_key for p in CODEX_NATIVE_PERMISSION_PRESETS] == ["1", "2", "3", "4"]


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
