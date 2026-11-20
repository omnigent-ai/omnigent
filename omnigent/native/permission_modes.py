"""Native permission modes accepted by automations and their launch arguments."""

# Keep these mappings aligned with the web permission constants in
# claudePermissionMode.ts and nativeHarnessModes.ts.
PERMISSION_LAUNCH_ARGS: dict[str, dict[str, list[str]]] = {
    "claude-native": {
        "default": ["--permission-mode", "default"],
        "auto": ["--permission-mode", "auto"],
        "acceptEdits": ["--permission-mode", "acceptEdits"],
        "plan": ["--permission-mode", "plan"],
        "dontAsk": ["--permission-mode", "dontAsk"],
        "bypassPermissions": ["--permission-mode", "bypassPermissions"],
    },
    "codex-native": {
        "default": [],
        "ask-for-approval": [
            "--sandbox",
            "workspace-write",
            "--ask-for-approval",
            "on-request",
            "-c",
            'approvals_reviewer="user"',
        ],
        "approve-for-me": [
            "--sandbox",
            "workspace-write",
            "--ask-for-approval",
            "on-request",
            "-c",
            'approvals_reviewer="auto_review"',
        ],
        "full-access": ["--sandbox", "danger-full-access", "--ask-for-approval", "never"],
        "read-only": ["--sandbox", "read-only", "--ask-for-approval", "on-request"],
        "bypass": [],
    },
    "cursor-native": {
        "default": [],
        "auto-review": ["--auto-review"],
        "plan": ["--mode", "plan"],
        "ask": ["--mode", "ask"],
        "yolo": ["--yolo"],
    },
    "antigravity-native": {"default": [], "skip": ["--dangerously-skip-permissions"]},
    "devin-native": {
        "normal": [],
        "accept-edits": ["--permission-mode", "accept-edits"],
        "smart": ["--permission-mode", "smart"],
        "dangerous": ["--permission-mode", "dangerous"],
    },
}

# Shared by every rejection of an automation's permission_mode.
AGENT_DEFAULT_PERMISSION_MODE_HINT = "JSON null (not the string 'null') for the agent default"

# Modes that skip the approval prompt (``dontAsk`` denies instead, by rules the server
# cannot see). An automation runs in one only when the task itself selects it, never
# through an agent default or a reused session's live mode.
APPROVAL_SKIPPING_PERMISSION_MODES: frozenset[str] = frozenset(
    {
        "bypassPermissions",  # claude-native, claude-sdk, acp
        "dontAsk",  # claude-native
        "full-access",  # codex-native
        "bypass",  # codex-native
        "yolo",  # cursor-native
        "skip",  # antigravity-native
        "dangerous",  # devin-native
    }
)


def created_session_launch_args(harness: str | None, mode: str) -> list[str] | None:
    """
    Launch args for a session created with ``permission_mode``, e.g. by ``sys_session_create``.

    The creating agent chooses the mode, and what it reads (a ticket, a message)
    can try to steer that choice, so a mode that skips approval is refused: the
    new session keeps an approval prompt or the harness's own reviewer.

    :param harness: The new session's canonical harness, e.g. ``"codex-native"``.
    :param mode: A mode in that harness's vocabulary, e.g. ``"approve-for-me"``.
    :returns: The CLI args, or ``None`` when the mode is the harness's own default.
    :raises ValueError: The mode skips approval, or the harness doesn't offer it.
    """
    if mode in APPROVAL_SKIPPING_PERMISSION_MODES:
        raise ValueError(
            f"permission_mode {mode!r} skips approval, which a created session can't use; "
            "choose a mode that asks for approval or uses the harness's reviewer"
        )
    modes = PERMISSION_LAUNCH_ARGS.get(harness or "", {})
    if mode not in modes:
        choices = [name for name in modes if name not in APPROVAL_SKIPPING_PERMISSION_MODES]
        supported = f"use one of {', '.join(map(repr, choices))}" if choices else "omit it"
        raise ValueError(
            f"permission_mode {mode!r} is not a mode of {harness or 'this agent'!r}; {supported}"
        )
    return list(modes[mode]) or None
