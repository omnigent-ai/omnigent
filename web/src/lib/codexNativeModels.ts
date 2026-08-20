import type { NativeModelOption } from "./types";

/**
 * Find a native picker option by its UI alias or provider-facing model id.
 *
 * @param options - Native model options from the session snapshot.
 * @param model - Candidate model id, e.g. ``"gpt-5.5"``.
 * @returns The matching option, or ``null`` when unknown.
 */
export function findNativeModelOption(
  options: readonly NativeModelOption[],
  model: string | null | undefined,
): NativeModelOption | null {
  const raw = model?.trim();
  if (!raw) return null;
  return options.find((option) => option.id === raw || option.model === raw) ?? null;
}

/**
 * Whether a sticky model id is one Codex advertised for this session.
 *
 * @param options - Codex model options from the session snapshot.
 * @param model - Candidate model id.
 * @returns True only when the candidate matches a Codex-returned option.
 */
export function isCodexNativeModel(
  options: readonly NativeModelOption[],
  model: string | null | undefined,
): boolean {
  return findNativeModelOption(options, model) !== null;
}

/**
 * Effort levels for the currently selected Codex model.
 *
 * @param options - Codex model options from the session snapshot.
 * @param currentModel - Active override or bound model id.
 * @returns Model-specific effort values from Codex ``model/list``.
 */
export function codexEffortLevelsForModel(
  options: readonly NativeModelOption[],
  currentModel: string | null | undefined,
): readonly string[] {
  if (options.length === 0) return [];
  const selected =
    findNativeModelOption(options, currentModel) ??
    options.find((option) => option.isDefault === true) ??
    options[0] ??
    null;
  const efforts = selected?.supportedReasoningEfforts ?? [];
  return Array.from(
    new Set(
      efforts
        .map((option) => option.reasoningEffort)
        .filter((effort): effort is string => typeof effort === "string" && effort.length > 0),
    ),
  );
}
