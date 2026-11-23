// Shift+Tab steps through every option in the permission chip's menu order.

import {
  CLAUDE_NATIVE_SWITCHABLE_PERMISSION_MODES,
  claudePermissionModeLabel,
} from "@/lib/claudePermissionMode";
import {
  CODEX_NATIVE_RUNTIME_APPROVAL_PRESETS,
  codexApprovalModeLabel,
} from "@/lib/codexApprovalMode";
import { effectiveNativeWrapperLabel, type NativeSessionSource } from "@/lib/nativeCodingAgents";
import { isImeCompositionKeyEvent } from "@/lib/ime";
import { DEVIN_NATIVE_PERMISSION_MODES } from "@/lib/nativeHarnessModes";

/** The key fields the cycle check reads from a React keyboard event. */
export interface PermissionModeCycleKeyEvent {
  key: string;
  shiftKey: boolean;
  ctrlKey: boolean;
  metaKey: boolean;
  altKey: boolean;
  nativeEvent: { isComposing?: boolean; keyCode?: number };
}

/** Bare Shift+Tab (no Ctrl, Cmd or Alt), outside IME composition. */
export function isPermissionModeCycleKey(event: PermissionModeCycleKeyEvent): boolean {
  return (
    event.key === "Tab" &&
    event.shiftKey &&
    !event.ctrlKey &&
    !event.metaKey &&
    !event.altKey &&
    !isImeCompositionKeyEvent(event)
  );
}

/**
 * Next option after *current* in menu order, wrapping; an unknown
 * *current* starts from the top. Skips *unavailable*. Null when none is reachable.
 */
export function nextCycledPermissionMode(
  options: readonly { value: string }[],
  current: string | null | undefined,
  unavailable: ReadonlySet<string> = new Set(),
): string | null {
  const start = options.findIndex((option) => option.value === current);
  for (let step = 1; step <= options.length; step += 1) {
    const candidate = options[(start + step) % options.length]!.value;
    if (candidate === current) continue;
    if (unavailable.has(candidate)) continue;
    return candidate;
  }
  return null;
}

/** A Shift+Tab press that didn't land: the mode it started from and the one it tried. */
export interface PermissionModeCycleMiss {
  sessionId: string | null;
  from: string;
  past: string;
}

/**
 * The mode a Shift+Tab press moves to from *selected*. After a press that missed,
 * the next one continues past the mode it tried instead of retrying it.
 */
export function nextPermissionModePress(
  options: readonly { value: string }[],
  selected: string,
  sessionId: string | null,
  miss: PermissionModeCycleMiss | null,
): string | null {
  const resume = miss?.sessionId === sessionId && miss.from === selected ? miss.past : null;
  const resumed =
    resume === null ? null : nextCycledPermissionMode(options, resume, new Set([selected]));
  // With nothing else to move to, retry the failed mode.
  return resumed ?? nextCycledPermissionMode(options, selected);
}

/** Which vocabulary a session's permission chip uses. */
export type PermissionChipKind = "claude" | "devin" | "codex";

/**
 * The vocabulary of a session's permission chip, or null when its harness has
 * none. Shared by the full-session, project and grid composers.
 */
export function permissionChipKind(
  session: NativeSessionSource | null | undefined,
): PermissionChipKind | null {
  switch (effectiveNativeWrapperLabel(session)) {
    case "claude-code-native-ui":
      return "claude";
    case "codex-native-ui":
      return "codex";
    case "devin-native-ui":
      return "devin";
    default:
      return null;
  }
}

/** The chip's menu options, selected value and label for a session's current modes. */
export function permissionChip(
  kind: PermissionChipKind,
  claudePermissionMode: string,
  codexApprovalMode: string,
): { options: readonly { value: string; label: string }[]; value: string; label: string } {
  if (kind === "codex")
    return {
      options: CODEX_NATIVE_RUNTIME_APPROVAL_PRESETS,
      value: codexApprovalMode,
      label: codexApprovalModeLabel(codexApprovalMode),
    };
  if (kind === "devin")
    return {
      options: DEVIN_NATIVE_PERMISSION_MODES,
      value: claudePermissionMode,
      label:
        DEVIN_NATIVE_PERMISSION_MODES.find((mode) => mode.value === claudePermissionMode)?.label ??
        claudePermissionMode,
    };
  return {
    options: CLAUDE_NATIVE_SWITCHABLE_PERMISSION_MODES,
    value: claudePermissionMode,
    label: claudePermissionModeLabel(claudePermissionMode),
  };
}
