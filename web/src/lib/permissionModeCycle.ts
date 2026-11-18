// Shift+Tab steps through every option in the permission chip's menu order.

import {
  CLAUDE_NATIVE_SWITCHABLE_PERMISSION_MODES,
  claudePermissionModeLabel,
  isClaudeNativeSession,
} from "@/lib/claudePermissionMode";
import {
  CODEX_NATIVE_RUNTIME_APPROVAL_PRESETS,
  codexApprovalModeLabel,
} from "@/lib/codexApprovalMode";
import { isCodexNativeSession } from "@/lib/codexPlanMode";
import { modelPickerKindForConv } from "@/lib/composerCapabilities";
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

/** Which vocabulary a session's permission chip uses. */
export type PermissionChipKind = "claude" | "devin" | "codex";

/**
 * The vocabulary of a session's permission chip, or null when its harness has
 * none. Same precedence as the main composer: Devin, then Claude, then Codex.
 */
export function permissionChipKind(
  session: Parameters<typeof modelPickerKindForConv>[0],
): PermissionChipKind | null {
  if (modelPickerKindForConv(session) === "devin") return "devin";
  if (isClaudeNativeSession(session)) return "claude";
  if (isCodexNativeSession(session)) return "codex";
  return null;
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
