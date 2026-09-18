// Canonical model / effort label formatting for the composer, shared by the
// landing dialog and the in-session chat composer so both surfaces render the
// same label for the same model.
//
// Pure leaf module (no React, no store) so the landing screen, the chat page,
// the harness config controls, and the store can all depend on one source of
// truth without a circular import.

import { fusionModelLabel, isFusionModelUid } from "@/lib/devinFusion";
import type { NativeModelOption } from "@/lib/types";

const DISPLAY_ONLY_CATALOG_PREFIXES = ["databricks-", "system.ai."] as const;

/** The native-catalog fields a model label is built from. A superset like
 *  {@link NativeModelOption} is assignable to this. */
export interface NativeModelLabelFields {
  id: string;
  model?: string;
  displayName?: string;
  isDefault?: boolean;
}

export function nativeModelLabel(option: NativeModelLabelFields): string {
  const label = option.displayName ?? option.model ?? option.id;
  // Some provider catalogs repeat the transport id as their display name.
  // Hide its mechanical namespace while preserving real advertised labels.
  const isTransportLabel = [option.id, option.model].some(
    (id) => id != null && (label === id || label === id.slice(id.indexOf("/") + 1)),
  );
  if (option.displayName != null && !isTransportLabel) return label;
  for (const prefix of DISPLAY_ONLY_CATALOG_PREFIXES) {
    if (label.startsWith(prefix)) return label.slice(prefix.length);
  }
  return label;
}

export function defaultModelLabel(options: readonly NativeModelLabelFields[]): string {
  const defaultOption = options.find((option) => option.isDefault);
  return defaultOption ? `Default (${nativeModelLabel(defaultOption)})` : "Default";
}

export function compactModelTriggerLabel(value: string): string {
  return /^Default \((.*)\)$/.exec(value)?.[1] ?? value;
}

export function formatStatusModelLabel(
  model: string | null,
  codexModelOptions: readonly NativeModelOption[] = [],
): string | null {
  const raw = model?.trim();
  if (!raw) return null;
  if (isFusionModelUid(raw)) {
    const descriptor = codexModelOptions.find((candidate) => candidate.fusion)?.fusion;
    if (descriptor) return fusionModelLabel(descriptor, raw);
  }
  const option =
    codexModelOptions.find((candidate) => candidate.id === raw) ??
    codexModelOptions.find((candidate) => candidate.model === raw);
  return option ? nativeModelLabel(option) : raw;
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
