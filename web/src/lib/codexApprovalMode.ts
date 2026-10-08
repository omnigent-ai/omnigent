// Running-session approval switcher for codex-native. Unlike the create-time
// presets in `nativeHarnessModes` (which map to `terminal_launch_args`), the
// live switch drives Codex's own `/permissions` popup by keystroke injection,
// so its options and labels mirror that popup. Read Only isn't offered: the
// popup lists it only for sessions already running a read-only permission
// profile, so users start a new read-only session instead. The full-bypass
// sandbox stance stays launch-only (the server 400s it on a runtime switch).

export interface CodexRuntimeApprovalPreset {
  value: string;
  label: string;
  description: string;
}

/** Conversation-label key the server writes for the live approval mode. */
export const CODEX_NATIVE_APPROVAL_MODE_LABEL_KEY = "omnigent.codex_native.approval_mode";

/**
 * The three runtime approval stances the picker can switch into, in Codex's
 * `/permissions` popup order.
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
];

// A session launched read-only still reports it as its live mode, so it keeps
// a label even though the picker can't switch into it.
const CODEX_NATIVE_READ_ONLY_LABEL = "Read Only";

/** Human label for an approval-mode value, falling back to the raw value. */
export function codexApprovalModeLabel(mode: string | null | undefined): string {
  if (!mode) return "";
  if (mode === "read-only") return CODEX_NATIVE_READ_ONLY_LABEL;
  return CODEX_NATIVE_RUNTIME_APPROVAL_PRESETS.find((m) => m.value === mode)?.label ?? mode;
}

/**
 * The live approval mode of a codex-native session, or null when unknown.
 *
 * Reads only the label the server stamps after a confirmed switch (web PATCH
 * or a `/permissions` change observed in the TUI). It does NOT reconstruct
 * from launch args — the runtime stance no longer rides them — so a session
 * that hasn't switched yet resolves to null and the picker shows an unset
 * state rather than a guessed default.
 */
export function codexApprovalModeFromSession(
  session: { labels?: Record<string, string | null> | null } | null | undefined,
): string | null {
  const labelled = session?.labels?.[CODEX_NATIVE_APPROVAL_MODE_LABEL_KEY];
  return typeof labelled === "string" && labelled ? labelled : null;
}
