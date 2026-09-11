// Create-time Codex approval option source, shared by the composer permission
// picker on landing (and available to the fork dialog). Always includes the
// DANGEROUS full-bypass entry as a 4th option so the quick menu offers the same
// explicit opt-in the Edit config modal already does (#7070).
//
// SCOPE: create-time only. A RUNNING codex session switches approval through
// Codex's `/permissions` popup, which has no bypass row (bypass is a launch
// flag) — see `omnigent/codex_approval_modes.py` and `@/lib/codexApprovalMode`.
// So the running-session picker keeps the runtime presets (no bypass); this
// module never widens that surface, and nothing here auto-enables bypass.

import {
  CODEX_NATIVE_APPROVAL_MODES,
  CODEX_NATIVE_BYPASS_APPROVAL_OPTION,
  CODEX_NATIVE_BYPASS_APPROVAL_VALUE,
  CODEX_NATIVE_DEFAULT_APPROVAL_MODE,
} from "@/lib/nativeHarnessModes";

/** A `{ value, label }` option, matching the composer permission picker's prop. */
export interface CodexApprovalOption {
  value: string;
  label: string;
}

/**
 * The Codex approval options for a CREATE-time picker: the standard presets
 * plus the bypass entry. `{ value, label }` only — the picker renders them and
 * has no need for the CLI-flag `args`.
 */
export function codexCreateApprovalOptions(): CodexApprovalOption[] {
  return [...CODEX_NATIVE_APPROVAL_MODES, CODEX_NATIVE_BYPASS_APPROVAL_OPTION].map(
    ({ value, label }) => ({ value, label }),
  );
}

/**
 * The picker's current value from the two pieces of composer state: when bypass
 * is armed it wins over the approval preset (matching how the create request
 * treats the bypass label as overriding the preset flags).
 */
export function codexApprovalSelectionValue(approvalMode: string, bypass: boolean): string {
  return bypass ? CODEX_NATIVE_BYPASS_APPROVAL_VALUE : approvalMode;
}

/**
 * Reduce a picker selection to the `{ approvalMode, bypass }` the composer
 * stores. Selecting bypass sets the flag and LEAVES the underlying approval
 * preset untouched (so unchecking bypass restores it); selecting any preset
 * clears bypass. Pure — the caller writes the result through its own setters.
 *
 * @param value The selected option value.
 * @param prevApprovalMode The approval preset currently in state, kept when
 *   bypass is selected.
 */
export function applyCodexApprovalSelection(
  value: string,
  prevApprovalMode: string = CODEX_NATIVE_DEFAULT_APPROVAL_MODE,
): { approvalMode: string; bypass: boolean } {
  if (value === CODEX_NATIVE_BYPASS_APPROVAL_VALUE) {
    return { approvalMode: prevApprovalMode, bypass: true };
  }
  return { approvalMode: value, bypass: false };
}
