// Claude Code's permission modes: every mode `--permission-mode` accepts
// when starting a session, the subset a running session can switch to, and
// the New Chat-only no-flag default (see the lists below).

import { nativeCodingAgentForHarness, WRAPPER_LABEL_KEY } from "@/lib/nativeCodingAgents";

const CLAUDE_NATIVE_PERMISSION_MODE_LABEL_KEY = "omnigent.claude_native.permission_mode";
const CLAUDE_NATIVE_WRAPPER = nativeCodingAgentForHarness("claude-native")?.wrapperLabel;

/** Whether a session runs the claude-native wrapper. Fails closed. */
export function isClaudeNativeSession(
  session: { labels?: Record<string, string | null> | null } | null | undefined,
): boolean {
  const wrapper = session?.labels?.[WRAPPER_LABEL_KEY];
  // Both sides must be set: an undefined registry lookup must not match a
  // session with no wrapper label.
  return !!wrapper && wrapper === CLAUDE_NATIVE_WRAPPER;
}

export interface ClaudePermissionModeOption {
  value: string;
  label: string;
  description: string;
}

// "inherit" is a web-UI sentinel: no --permission-mode flag is passed, so
// Claude Code uses whatever the user has configured in their settings file.
export const CLAUDE_NATIVE_DEFAULT_PERMISSION_MODE = "inherit";
export const CLAUDE_NATIVE_INHERIT_PERMISSION_MODE = CLAUDE_NATIVE_DEFAULT_PERMISSION_MODE;

// Claude Code's `claude --permission-mode` choices (v2.1). Keep in sync
// with `claude --help`. Every value here maps 1:1 to a launch flag; the
// frontend-only "inherit" sentinel is deliberately excluded.
export const CLAUDE_NATIVE_PERMISSION_MODES: ClaudePermissionModeOption[] = [
  { value: "default", label: "Manual", description: "Prompts before edits and commands" },
  {
    value: "auto",
    label: "Auto",
    description: "Auto-runs; a classifier blocks risky actions",
  },
  {
    value: "acceptEdits",
    label: "Accept edits",
    description: "Auto-applies file edits; commands still prompt",
  },
  { value: "plan", label: "Plan", description: "Plans only; makes no edits" },
  { value: "dontAsk", label: "Don't ask", description: "Auto-denies anything not pre-approved" },
  {
    value: "bypassPermissions",
    label: "Bypass permissions",
    description: "Runs everything; no prompts or safety checks",
  },
];

const CLAUDE_NATIVE_INHERIT_PERMISSION_MODE_OPTION: ClaudePermissionModeOption = {
  value: CLAUDE_NATIVE_INHERIT_PERMISSION_MODE,
  label: "Default",
  description: "Uses your configured Claude Code permission mode",
};

// The New Chat and fork pickers offer the no-flag Default entry first, then
// every real launch mode. Scheduled tasks keep their own "Default" sentinel
// and use only CLAUDE_NATIVE_PERMISSION_MODES.
export const CLAUDE_NATIVE_NEW_CHAT_PERMISSION_MODES: ClaudePermissionModeOption[] = [
  CLAUDE_NATIVE_INHERIT_PERMISSION_MODE_OPTION,
  ...CLAUDE_NATIVE_PERMISSION_MODES,
];

/** Modes a running session can be switched to (shift+tab-reachable). */
export const CLAUDE_NATIVE_SWITCHABLE_PERMISSION_MODES: ClaudePermissionModeOption[] =
  CLAUDE_NATIVE_PERMISSION_MODES.filter((mode) =>
    ["default", "acceptEdits", "plan", "auto"].includes(mode.value),
  );

export function isSwitchableClaudePermissionMode(mode: string | null | undefined): boolean {
  return CLAUDE_NATIVE_SWITCHABLE_PERMISSION_MODES.some((m) => m.value === mode);
}

/** Human label for a mode value, falling back to the raw value. */
export function claudePermissionModeLabel(mode: string | null | undefined): string {
  if (!mode) return "";
  return CLAUDE_NATIVE_PERMISSION_MODES.find((m) => m.value === mode)?.label ?? mode;
}

/**
 * The permission mode a running claude-native session is in, or `null` when
 * it cannot be determined.
 *
 * Prefers the label the server stamps after a confirmed switch, then the
 * launch flag. Returns `null` rather than assuming Claude's default: a
 * `permissions.defaultMode` in a settings file boots the session into a mode
 * that never appears in `terminal_launch_args`, so guessing would display a
 * mode the session isn't in. Callers hide the picker on `null`.
 */
export function claudePermissionModeFromSession(
  session:
    | {
        labels?: Record<string, string | null> | null;
        terminalLaunchArgs?: string[] | null;
      }
    | null
    | undefined,
): string | null {
  const labelled = session?.labels?.[CLAUDE_NATIVE_PERMISSION_MODE_LABEL_KEY];
  if (typeof labelled === "string" && labelled) return labelled;
  const args = session?.terminalLaunchArgs ?? [];
  const flagIndex = args.indexOf("--permission-mode");
  if (flagIndex >= 0 && flagIndex + 1 < args.length) return args[flagIndex + 1];
  return null;
}
