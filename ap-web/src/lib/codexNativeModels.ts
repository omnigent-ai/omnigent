import type { CodexModelOption } from "./types";

export interface NativeModelPickerOption {
  id: string;
  label: string;
}

function normalizeModelId(model: string): string {
  return model
    .trim()
    .toLowerCase()
    .replace(/^(databricks|openai|anthropic|claude)[-_/]+/, "")
    .replace(/[._/]+/g, "-");
}

function unique(values: readonly string[]): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const value of values) {
    if (seen.has(value)) continue;
    seen.add(value);
    out.push(value);
  }
  return out;
}

/**
 * Convert Codex ``model/list`` options into picker rows.
 *
 * @param options - Codex model options from the session snapshot.
 * @returns Picker rows using Codex's own id and display label.
 */
export function codexModelPickerOptions(
  options: readonly CodexModelOption[],
): NativeModelPickerOption[] {
  return options.map((m) => ({ id: m.id, label: m.displayName || m.id }));
}

/**
 * Find the Codex option matching a model id reported by either Omnigent or Codex.
 *
 * @param options - Codex model options from the session snapshot.
 * @param model - Candidate model id, e.g. ``"gpt-5.5"``.
 * @returns The matching option, or ``null`` when unknown.
 */
export function findCodexModelOption(
  options: readonly CodexModelOption[],
  model: string | null | undefined,
): CodexModelOption | null {
  const raw = model?.trim();
  if (!raw) return null;
  const lower = raw.toLowerCase();
  const normalized = normalizeModelId(raw);
  return (
    options.find((option) => {
      const ids = [option.id, option.model].map((value) => value.toLowerCase());
      return (
        ids.includes(lower) ||
        ids.map(normalizeModelId).includes(normalized)
      );
    }) ?? null
  );
}

/**
 * Whether a sticky model id is one Codex advertised for this session.
 *
 * @param options - Codex model options from the session snapshot.
 * @param model - Candidate model id.
 * @returns True only when the candidate matches a Codex-returned option.
 */
export function isCodexNativeModel(
  options: readonly CodexModelOption[],
  model: string | null | undefined,
): boolean {
  return findCodexModelOption(options, model) !== null;
}

/**
 * Effort levels for the currently selected Codex model.
 *
 * @param options - Codex model options from the session snapshot.
 * @param currentModel - Active override or bound model id.
 * @returns Model-specific effort values from Codex ``model/list``.
 */
export function codexEffortLevelsForModel(
  options: readonly CodexModelOption[],
  currentModel: string | null | undefined,
): readonly string[] {
  if (options.length === 0) return [];
  const selected =
    findCodexModelOption(options, currentModel) ??
    options.find((option) => option.isDefault) ??
    options[0] ??
    null;
  return selected ? unique(selected.supportedReasoningEfforts) : [];
}
