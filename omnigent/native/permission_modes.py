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

# Shared by the permission_mode value rejections. Agents that call the tool before
# loading its schema send the string "null", so the hint names JSON null exactly.
AGENT_DEFAULT_PERMISSION_HINT = "JSON null (not the string 'null') for the agent default"


def describe_harness_permission_modes(harness: str) -> str:
    """Name the task permission modes ``harness`` accepts, for a rejection message."""
    modes = PERMISSION_LAUNCH_ARGS.get(harness)
    if not modes:
        return f"{harness!r} has no task permission modes; send {AGENT_DEFAULT_PERMISSION_HINT}"
    names = ", ".join(repr(mode) for mode in modes)
    return f"{harness!r} accepts {names}, or {AGENT_DEFAULT_PERMISSION_HINT}"


def describe_permission_modes() -> str:
    """Every harness's task permission modes, for a rejection made before the harness is known."""
    known = "; ".join(
        f"{harness}: {', '.join(modes)}" for harness, modes in PERMISSION_LAUNCH_ARGS.items()
    )
    return f"{known}; other harnesses, including claude-sdk, have none"
