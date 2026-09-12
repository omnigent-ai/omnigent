// Canonical model / effort label formatting for the composer, shared by the
// landing dialog and the in-session chat composer so both surfaces render the
// same label for the same model.
//
// Pure leaf module (no React, no store) so the landing screen, the chat page,
// the harness config controls, and the store can all depend on one source of
// truth without a circular import.

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
 * For a recognized Claude family the label reflects the WIRE model's ` (1M
 * context)` variant, which is load-bearing so a 1M model reads distinctly from
 * its 200k sibling. The advertised displayName carries only the family/version
 * (the harness strips `[1m]` when building it), so the variant suffix is taken
 * from the wire model. A genuinely custom advertised name is kept verbatim. */
export function nativeModelLabel(option: NativeModelLabelFields): string {
  const label = option.displayName ?? option.id;
  const model = option.model ?? option.id;
  const resolved =
    /^(?:system\.ai\.|databricks-)?claude-(opus|sonnet|haiku|fable)-(\d{1,2})(?:[-.](\d{1,2}))?(?:-\d{8})?(\[1m\])?$/i.exec(
      model,
    );
  if (!resolved) return label;
  const [, family, major, minor, context] = resolved;
  const has1M = Boolean(context);
  // Advertised name with any harness prefix / context marker peeled off. The
  // marker has several spellings in the wild (`[1m]`, `(1M)`, `(1M context)`);
  // normalize them all so the recognized suffix is re-applied consistently.
  const advertised = label
    .replace(/\s*(?:\[1m\]|\(1m(?: context)?\))$/i, "")
    .replace(/^Claude /i, "")
    .trim();
  // Whether the advertised name is the recognized family (optionally with a
  // version) rather than a custom name that must be kept as-is.
  const familyShaped =
    advertised
      .replace(/\s*\d{1,2}(?:\.\d{1,2})?$/, "")
      .trim()
      .toLowerCase() === family!.toLowerCase();
  if (!familyShaped && label !== model) return label;
  const familyLabel = family![0]!.toUpperCase() + family!.slice(1).toLowerCase();
  // Keep the advertised version wording when it carries one, else synthesize it
  // from the wire model; then always reflect the wire model's context variant.
  const base =
    familyShaped && /\d/.test(advertised)
      ? advertised
      : `${familyLabel} ${major}${minor ? `.${minor}` : ""}`;
  return has1M ? `${base} (1M context)` : base;
}

/**
 * Label for the Model row's "Default" choice, naming the model it resolves to
 * when the catalog marks one, so the same session can't read "Default" in one
 * place and "Default (GPT-5.6-Luna)" in another.
 *
 * @param options Harness catalog rows; at most one is marked default.
 * @returns ``Default (<name>)``, or plain ``Default`` when unmarked.
 */
export function defaultModelLabel(options: readonly NativeModelLabelFields[]): string {
  const defaultOption = options.find((option) => option.isDefault);
  return defaultOption ? `Default (${nativeModelLabel(defaultOption)})` : "Default";
}

/**
 * The model label for the composer harness trigger.
 *
 * Collapses a `Default (X)` value to just `X` (the trigger names the resolved
 * model, not the "Default" wrapper) while PRESERVING the ` (1M context)`
 * variant suffix, so the trigger and the chat status line read the same for a
 * 1M model.
 */
export function compactModelTriggerLabel(value: string): string {
  return /^Default \((.*)\)$/.exec(value)?.[1] ?? value;
}

/**
 * The display label for a raw model id — the in-session status source.
 *
 * Prefers the session's Codex catalog row, then the shared Claude-id folding
 * (so a full or catalog-prefixed id like ``claude-opus-4-8[1m]`` or
 * ``system.ai.claude-opus-4-8[1m]`` renders as ``Opus 4.8 (1M context)`` even
 * with no catalog), then a version-agnostic friendly form for a bare alias, and
 * finally the raw id. ``null`` when no model is known.
 */
export function formatStatusModelLabel(
  model: string | null,
  codexModelOptions: readonly NativeModelOption[] = [],
): string | null {
  const raw = model?.trim();
  if (!raw) return null;
  const codexOption = findNativeModelOption(codexModelOptions, raw);
  if (codexOption) return nativeModelLabel(codexOption);
  // Fold a full/catalog-prefixed Claude id through the same helper the catalog
  // rows use, so a known model reads identically with or without the catalog.
  const folded = nativeModelLabel({ id: raw, model: raw });
  if (folded !== raw) return folded;
  // A bare alias the catalog doesn't list (e.g. before it resolves): render it
  // mechanically — "sonnet" → "Sonnet", "sonnet_5" → "Sonnet 5", "sonnet[1m]"
  // → "Sonnet (1M context)" — without claiming a version we can't know.
  const alias = /^([a-z]+)(?:_(\d+))?(\[1m\])?$/.exec(raw.toLowerCase());
  if (alias) {
    let label = `${alias[1]!.charAt(0).toUpperCase()}${alias[1]!.slice(1)}`;
    if (alias[2]) label += ` ${alias[2]}`;
    if (alias[3]) label += " (1M context)";
    return label;
  }
  return raw;
}

/** Normalize a reasoning-effort value to its display label — the single place
 *  ``xhigh`` becomes ``xHigh``. Any other value is capitalized. */
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
