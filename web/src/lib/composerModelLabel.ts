// Canonical model / effort label formatting for the composer, shared by the
// landing dialog and the in-session chat composer so the two surfaces render
// the SAME label for the same model and can't diverge or flicker (#7094).
//
// Pure leaf module (no React, no store) so the landing screen, the chat page,
// the harness config controls, and the store can all import one source of
// truth. `nativeModelLabel` / `defaultModelLabel` live here (and are re-exported
// from `HarnessConfigControls.tsx` for callers that import them from there);
// `formatStatusModelLabel` / `formatStatusEffortLabel` / `formatModelEffortStatusLabel`
// moved here from `ChatPage.tsx`.

import { findNativeModelOption } from "@/lib/codexNativeModels";
import type { NativeModelOption } from "@/lib/types";

/** The native-catalog fields a model label is built from. A superset like
 *  {@link NativeModelOption} is assignable to this. */
export interface NativeModelLabelFields {
  id: string;
  model?: string;
  displayName?: string;
  isDefault?: boolean;
}

/** A catalog row's user-facing name: what the harness advertises, else its id.
 *
 * Claude aliases fold to a `Family Major.Minor (1M context)` spelling; the
 * ` (1M context)` variant suffix is load-bearing and always preserved so a 1M
 * model reads distinctly from its 200k sibling. */
export function nativeModelLabel(option: NativeModelLabelFields): string {
  const label = option.displayName ?? option.id;
  const model = option.model ?? option.id;
  const resolved =
    /^(?:system\.ai\.|databricks-)?claude-(opus|sonnet|haiku|fable)-(\d{1,2})(?:[-.](\d{1,2}))?(?:-\d{8})?(\[1m\])?$/i.exec(
      model,
    );
  if (!resolved) return label;
  const [, family, major, minor, context] = resolved;
  const bareLabel = label.replace(/(?:\[1m\]| \(1M context\))$/i, "").replace(/^Claude /i, "");
  if (bareLabel.toLowerCase() !== family!.toLowerCase() && label !== model) return label;
  const familyLabel = family![0]!.toUpperCase() + family!.slice(1).toLowerCase();
  return `${familyLabel} ${major}${minor ? `.${minor}` : ""}${context ? " (1M context)" : ""}`;
}

/**
 * Label for the Model row's "Default" choice, naming the model it resolves to
 * when the catalog marks one.
 *
 * Shared by the landing dialog and the in-session composer: read from one place
 * so the same session can't read "Default" in one gear and
 * "Default (GPT-5.6-Luna)" in the other.
 *
 * @param options Harness catalog rows; at most one is marked default.
 * @returns ``Default (<name>)``, or plain ``Default`` when unmarked.
 */
export function defaultModelLabel(options: readonly NativeModelLabelFields[]): string {
  const dflt = options.find((option) => option.isDefault);
  return dflt ? `Default (${nativeModelLabel(dflt)})` : "Default";
}

/**
 * The canonical model label for the composer harness trigger.
 *
 * Collapses a `Default (X)` value to just `X` (the trigger names the resolved
 * model, not the "Default" wrapper) but — unlike the old landing-only
 * `compactHarnessTriggerValue` — PRESERVES the ` (1M context)` variant suffix.
 * Stripping it (the #7094 bug) made the landing trigger read "Opus 4.8" while
 * the chat status line read "Opus 4.8 (1M context)" for the same model.
 */
export function compactModelTriggerLabel(value: string): string {
  return /^Default \((.*)\)$/.exec(value)?.[1] ?? value;
}

/**
 * The advertised display label for a raw model id — the in-session status
 * source. Prefers the session's Codex catalog row, else a version-agnostic
 * friendly form for an alias-shaped id the catalog doesn't list (e.g. during
 * the pre-catalog window), else the raw id, or ``null`` when no model is known.
 */
export function formatStatusModelLabel(
  model: string | null,
  codexModelOptions: readonly NativeModelOption[] = [],
): string | null {
  const raw = model?.trim();
  if (!raw) return null;
  const lower = raw.toLowerCase();
  const codexOption = findNativeModelOption(codexModelOptions, raw);
  if (codexOption) return nativeModelLabel(codexOption);
  // An alias-shaped id the session's catalog doesn't list: render it friendly
  // mechanically — "sonnet" → "Sonnet", "sonnet_5" → "Sonnet 5", "sonnet[1m]"
  // → "Sonnet (1M context)" — without claiming a version the client can't know.
  const alias = /^([a-z]+)(?:_(\d+))?(\[1m\])?$/.exec(lower);
  if (alias) {
    let label = `${alias[1]!.charAt(0).toUpperCase()}${alias[1]!.slice(1)}`;
    if (alias[2]) label += ` ${alias[2]}`;
    if (alias[3]) label += " (1M context)";
    return label;
  }
  return raw;
}

/** Normalize a reasoning-effort value to its display label — the single place
 *  `xhigh` becomes `xHigh` (#7026). Any other value is capitalized. */
export function normalizeEffortLabel(effort: string): string {
  if (effort.toLowerCase() === "xhigh") return "xHigh";
  return effort.charAt(0).toUpperCase() + effort.slice(1);
}

/** Display label for a reasoning-effort value, or ``null`` when unset. */
export function formatStatusEffortLabel(effort: string | null): string | null {
  if (!effort) return null;
  return normalizeEffortLabel(effort);
}

/**
 * Compose the current model and effort for the composer status tray.
 *
 * @param model - Model override or bound model id.
 * @param effort - Current reasoning effort override, if any.
 * @returns Compact label such as ``"gpt-5.5 xHigh"``, or ``null`` when neither is known.
 */
export function formatModelEffortStatusLabel(
  model: string | null,
  effort: string | null,
  codexModelOptions: readonly NativeModelOption[] = [],
): string | null {
  const modelLabel = formatStatusModelLabel(model, codexModelOptions);
  const effortLabel = formatStatusEffortLabel(effort);
  const parts = [modelLabel, effortLabel].filter((p): p is string => p != null && p.length > 0);
  return parts.length > 0 ? parts.join(" ") : null;
}
