// Running-session approval switcher for codex-native. Unlike the create-time
// presets in `nativeHarnessModes` (which map to `terminal_launch_args`), the
// live switch drives Codex's own `/permissions` popup by keystroke injection,
// so its options and labels mirror that popup. The popup is version-dependent:
// newer builds add a "Read Only" stance that 0.146 lacks, so the list is the
// superset. The full-bypass stance has no popup row of its own — the runner
// delivers it through the Full Access row (same runtime settings) and the
// server arms the bypass label so relaunches keep the real launch flag.

import {
  CODEX_NATIVE_BYPASS_APPROVAL_OPTION,
  CODEX_NATIVE_BYPASS_APPROVAL_VALUE,
  CODEX_NATIVE_BYPASS_SANDBOX_LABEL_KEY,
} from "@/lib/nativeHarnessModes";

export interface CodexRuntimeApprovalPreset {
  value: string;
  label: string;
  description: string;
}

/** Conversation-label key the server writes for the live approval mode. */
export const CODEX_NATIVE_APPROVAL_MODE_LABEL_KEY = "omnigent.codex_native.approval_mode";

/**
 * The runtime approval stances Codex's `/permissions` popup offers, in its
 * order (see the version caveat above).
 */
export const CODEX_NATIVE_RUNTIME_APPROVAL_PRESETS: CodexRuntimeApprovalPreset[] = [
  {
    value: "ask-for-approval",
    label: "Ask for approval",
    description: "Read/edit/run in the workspace; approval for the internet or external edits",
  },
  {
    value: "approve-for-me",
    label: "Approve for me",
    description: "Only asks for actions detected as potentially unsafe",
  },
  {
    value: "full-access",
    label: "Full Access",
    description: "Edit any file and access the internet without approval",
  },
  {
    value: "read-only",
    label: "Read Only",
    description: "Read files only; approval required to edit files or access the internet",
  },
];

/**
 * Everything the running-session picker offers: the `/permissions` presets
 * plus the full-bypass stance, matching the create-time picker.
 */
export const CODEX_NATIVE_RUNTIME_PERMISSION_OPTIONS: CodexRuntimeApprovalPreset[] = [
  ...CODEX_NATIVE_RUNTIME_APPROVAL_PRESETS,
  {
    value: CODEX_NATIVE_BYPASS_APPROVAL_VALUE,
    label: CODEX_NATIVE_BYPASS_APPROVAL_OPTION.label,
    description: CODEX_NATIVE_BYPASS_APPROVAL_OPTION.description,
  },
];

/** Human label for an approval-mode value, falling back to the raw value. */
export function codexApprovalModeLabel(mode: string | null | undefined): string {
  if (!mode) return "";
  return CODEX_NATIVE_RUNTIME_PERMISSION_OPTIONS.find((m) => m.value === mode)?.label ?? mode;
}

/**
 * The live approval mode of a codex-native session, or null when unknown.
 *
 * Prefers the label the server stamps after a confirmed switch (web PATCH or
 * a `/permissions` change observed in the TUI), then the armed bypass label
 * (a bypass-launched session runs in bypass before any switch). It does NOT
 * reconstruct from launch args — the runtime stance no longer rides them — so
 * a session that hasn't switched yet resolves to null and the picker shows an
 * unset state rather than a guessed default.
 */
export function codexApprovalModeFromSession(
  session: { labels?: Record<string, string | null> | null } | null | undefined,
): string | null {
  const labelled = session?.labels?.[CODEX_NATIVE_APPROVAL_MODE_LABEL_KEY];
  if (typeof labelled === "string" && labelled) return labelled;
  if (session?.labels?.[CODEX_NATIVE_BYPASS_SANDBOX_LABEL_KEY] === "1") {
    return CODEX_NATIVE_BYPASS_APPROVAL_VALUE;
  }
  return null;
}
