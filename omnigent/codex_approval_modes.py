"""Codex-native runtime approval presets — mirrors Codex's ``/permissions`` popup.

The interactive ``codex`` TUI switches approval stance through its own
``/permissions`` popup, NOT through the app-server ``thread/settings/update``
RPC (that path drives model/effort but is a no-op for approval). So Omnigent's
running-session switcher drives the popup by keystroke: type ``/permissions``,
then the option's menu digit (position-independent, unlike arrow navigation),
then confirm the sub-dialog for the ones that ask.

These presets mirror the popup: their ``label`` is what the popup shows and
their order/``menu_key`` is the popup's own.

Version caveat: the popup's contents are codex-version-dependent. 0.146.0 offers
the first three (Ask for approval / Approve for me / Full Access); newer builds
add Read Only as a 4th option. This list is the superset in popup order — the
``menu_key`` digits are position-stable across the versions seen. On a build
that lacks Read Only, selecting it keys a non-existent menu row (a no-op); the
full-bypass launch flag has no ``/permissions`` row and is not represented here.

Kept dependency-free so the server routes, the runner, and the web contract can
all agree on the same list.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class CodexPermissionPreset:
    """One row of Codex's ``/permissions`` popup.

    :param value: Stable slug stored on the label / sent over the wire.
    :param label: Exact popup label, shown in the web picker too.
    :param description: The popup's own one-line explanation.
    :param menu_key: The digit that selects this row directly in the popup.
    :param needs_confirm: Whether the row opens a "Yes, continue anyway"
        sub-dialog that must be accepted (Full Access does).
    """

    value: str
    label: str
    description: str
    menu_key: str
    needs_confirm: bool


# Order and labels match Codex's ``/permissions`` popup (codex-cli 0.146.0).
CODEX_NATIVE_PERMISSION_PRESETS: tuple[CodexPermissionPreset, ...] = (
    CodexPermissionPreset(
        value="ask-for-approval",
        label="Ask for approval",
        description="Read/edit/run in the workspace; approval for the internet or external edits",
        menu_key="1",
        needs_confirm=False,
    ),
    CodexPermissionPreset(
        value="approve-for-me",
        label="Approve for me",
        description="Only asks for actions detected as potentially unsafe",
        menu_key="2",
        needs_confirm=False,
    ),
    CodexPermissionPreset(
        value="full-access",
        label="Full Access",
        description="Edit any file and access the internet without approval",
        menu_key="3",
        needs_confirm=True,
    ),
    # Newer Codex builds add Read Only as a 4th /permissions preset (older ones,
    # e.g. 0.146, omit it — see the version caveat in the module docstring).
    CodexPermissionPreset(
        value="read-only",
        label="Read Only",
        description="Read files only; approval required to edit files or access the internet",
        menu_key="4",
        needs_confirm=False,
    ),
)

CODEX_NATIVE_PERMISSION_VALUES: frozenset[str] = frozenset(
    preset.value for preset in CODEX_NATIVE_PERMISSION_PRESETS
)


def codex_permission_preset(value: str) -> CodexPermissionPreset | None:
    """:returns: The preset for *value*, or ``None`` when it is not a preset."""
    return next((p for p in CODEX_NATIVE_PERMISSION_PRESETS if p.value == value), None)


# Sandbox ``type`` spellings Codex uses (the app-server ``thread/settings/updated``
# notification uses camelCase; other paths hyphenate).
_FULL_ACCESS_SANDBOX_TYPES = frozenset({"dangerFullAccess", "danger-full-access"})
_READ_ONLY_SANDBOX_TYPES = frozenset({"readOnly", "read-only"})


def codex_permission_preset_from_thread_settings(settings: object) -> str | None:
    """Map a Codex ``threadSettings`` payload to a ``/permissions`` preset value.

    Reads the approval fields Codex emits on a ``thread/settings/updated`` (and
    ``thread/resume``) notification. Presets are distinguished by, in order: a
    full-access sandbox/profile/``never`` policy → ``full-access``; a read-only
    sandbox/profile → ``read-only``; an ``auto_review`` reviewer under on-request
    → ``approve-for-me``; a plain on-request policy → ``ask-for-approval``.
    Returns ``None`` when the payload doesn't resolve to a preset (e.g. a custom
    profile).

    :param settings: The ``threadSettings`` mapping (or anything, defensively).
    :returns: A value from :data:`CODEX_NATIVE_PERMISSION_VALUES`, or ``None``.
    """
    if not isinstance(settings, Mapping):
        return None
    approval_policy = settings.get("approvalPolicy")
    reviewer = settings.get("approvalsReviewer")
    sandbox = settings.get("sandboxPolicy")
    sandbox_type = sandbox.get("type") if isinstance(sandbox, Mapping) else None
    profile = settings.get("activePermissionProfile")
    profile_id = profile.get("id") if isinstance(profile, Mapping) else None
    if (
        sandbox_type in _FULL_ACCESS_SANDBOX_TYPES
        or profile_id == ":danger-full-access"
        or approval_policy == "never"
    ):
        return "full-access"
    if sandbox_type in _READ_ONLY_SANDBOX_TYPES or profile_id == ":read-only":
        return "read-only"
    if reviewer == "auto_review":
        return "approve-for-me"
    if approval_policy == "on-request":
        return "ask-for-approval"
    return None
