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

# Modes that would run a scheduled fire without a working approval gate. A
# scheduled run has nobody to answer a prompt, so the auto classifier is the
# check that stands in for the user. Claude's ``dontAsk`` denies rather than
# approves, but it skips that classifier and relies only on pre-approved rules
# the server cannot see. Each name belongs to one harness, so the set is
# checked without resolving the agent.
UNATTENDED_DISALLOWED_PERMISSION_MODES: frozenset[str] = frozenset(
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
