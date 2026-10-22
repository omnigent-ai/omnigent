/**
 * Devin Fusion mode: turn the server's flat combo table into dependent
 * Lead / Effort / Sidekick selectors (plus Fast / Priority checkboxes).
 *
 * A Fusion model id is `fusion-<lead>-sidekick-<sidekick>` and only a subset of
 * the cartesian product exists, so every choice list is derived from the real
 * {@link FusionDescriptor.combos} and every change is *repaired* back to a real
 * combo — the UI can never assemble an id Devin would reject.
 */
import type { FusionCombo, FusionDescriptor, NativeModelOption } from "@/lib/types";

/** Effort rungs in ascending order; unknown rungs sort after these, first-seen. */
const EFFORT_ORDER = ["low", "medium", "high", "xhigh", "max"];

/** A resolved selection across all five Fusion facets. */
export interface FusionSelection {
  lead: string;
  effort: string;
  fast: boolean;
  sidekick: string;
  priority: boolean;
}

/** The Fusion option in a native model list, or `undefined` when absent. */
export function fusionOption(options: readonly NativeModelOption[]): NativeModelOption | undefined {
  return options.find((option) => option.fusion !== undefined);
}

/** Whether `modelUid` names a Fusion pairing (a combo id or the bare family). */
export function isFusionModelUid(modelUid: string | null | undefined): boolean {
  return modelUid === "fusion" || (modelUid?.startsWith("fusion-") ?? false);
}

/** A compact display label for a Fusion combo, e.g. `Fusion · Claude Fable 5.1 Medium + SWE-2 Medium`. */
export function fusionModelLabel(descriptor: FusionDescriptor, modelUid: string): string {
  const combo = currentFusionCombo(descriptor, modelUid);
  const effort =
    combo.effort.toLowerCase() === "xhigh"
      ? "xHigh"
      : combo.effort.charAt(0).toUpperCase() + combo.effort.slice(1);
  const lead = `${combo.leadLabel} ${effort}${combo.fast ? " Fast" : ""}`;
  const sidekick = `${combo.sidekickLabel}${combo.priority ? " Priority" : ""}`;
  return `Fusion · ${lead} + ${sidekick}`;
}

/** The combo `modelUid` names, or the descriptor's default when it names none. */
export function currentFusionCombo(descriptor: FusionDescriptor, modelUid: string): FusionCombo {
  const match = descriptor.combos.find((combo) => combo.modelUid === modelUid);
  if (match !== undefined) return match;
  const fallback = descriptor.combos.find((combo) => combo.modelUid === descriptor.default);
  // A descriptor always has at least one combo (the server drops empty ones).
  return fallback ?? descriptor.combos[0];
}

/** Distinct entries of `key`, in first-seen (catalog) order, with their labels. */
function distinct(
  combos: readonly FusionCombo[],
  key: "lead" | "sidekick",
  labelKey: "leadLabel" | "sidekickLabel",
): { value: string; label: string }[] {
  const seen = new Map<string, string>();
  for (const combo of combos) {
    if (!seen.has(combo[key])) seen.set(combo[key], combo[labelKey]);
  }
  return [...seen].map(([value, label]) => ({ value, label }));
}

/** Lead families, first-seen order. */
export function fusionLeadChoices(
  descriptor: FusionDescriptor,
): { value: string; label: string }[] {
  return distinct(descriptor.combos, "lead", "leadLabel");
}

/** Effort rungs available for `lead`, ascending. */
export function fusionEffortChoices(descriptor: FusionDescriptor, lead: string): string[] {
  const efforts = new Set(
    descriptor.combos.filter((combo) => combo.lead === lead).map((combo) => combo.effort),
  );
  const known = EFFORT_ORDER.filter((rung) => efforts.has(rung));
  const extra = [...efforts].filter((rung) => !EFFORT_ORDER.includes(rung));
  return [...known, ...extra];
}

/** Sidekicks available for the given lead/effort/fast, first-seen order. */
export function fusionSidekickChoices(
  descriptor: FusionDescriptor,
  lead: string,
  effort: string,
  fast: boolean,
): { value: string; label: string }[] {
  return distinct(
    descriptor.combos.filter(
      (combo) => combo.lead === lead && combo.effort === effort && combo.fast === fast,
    ),
    "sidekick",
    "sidekickLabel",
  );
}

/** Whether a `-fast` lead pairing exists for this lead+effort. */
export function fusionFastAvailable(
  descriptor: FusionDescriptor,
  lead: string,
  effort: string,
): boolean {
  return descriptor.combos.some(
    (combo) => combo.lead === lead && combo.effort === effort && combo.fast,
  );
}

/** Whether a `-priority` sidekick exists for this lead/effort/fast/sidekick. */
export function fusionPriorityAvailable(
  descriptor: FusionDescriptor,
  lead: string,
  effort: string,
  fast: boolean,
  sidekick: string,
): boolean {
  return descriptor.combos.some(
    (combo) =>
      combo.lead === lead &&
      combo.effort === effort &&
      combo.fast === fast &&
      combo.sidekick === sidekick &&
      combo.priority,
  );
}

/** Pick `wanted` if available, else the first of `order` present, else the first available. */
function pickBest(available: readonly string[], wanted: string, order: readonly string[]): string {
  if (available.includes(wanted)) return wanted;
  const ordered = order.find((value) => available.includes(value));
  return ordered ?? available[0];
}

/**
 * Resolve a desired selection to a real combo's `modelUid`, repairing facets
 * that the chosen lead/effort/sidekick no longer supports (e.g. after switching
 * lead, keep the effort if it exists, otherwise fall to the nearest rung).
 *
 * @returns The `modelUid` of a combo that exists, always.
 */
export function resolveFusionModelUid(descriptor: FusionDescriptor, want: FusionSelection): string {
  const byLead = descriptor.combos.filter((combo) => combo.lead === want.lead);
  if (byLead.length === 0) return descriptor.default;

  const effort = pickBest(
    byLead.map((combo) => combo.effort),
    want.effort,
    EFFORT_ORDER,
  );
  const byEffort = byLead.filter((combo) => combo.effort === effort);

  // Want fast: take it if this lead+effort has a fast pairing. Want non-fast:
  // stay non-fast unless every pairing here is fast (then it is forced).
  const fast = want.fast
    ? byEffort.some((combo) => combo.fast)
    : byEffort.every((combo) => combo.fast);
  const byFast = byEffort.filter((combo) => combo.fast === fast);

  const sidekick = pickBest(
    byFast.map((combo) => combo.sidekick),
    want.sidekick,
    [],
  );
  const bySidekick = byFast.filter((combo) => combo.sidekick === sidekick);

  const priority = want.priority && bySidekick.some((combo) => combo.priority);
  const match = bySidekick.find((combo) => combo.priority === priority) ?? bySidekick[0];
  return match.modelUid;
}

/**
 * Apply a single facet change to the current Fusion selection and resolve the
 * new `modelUid`. Facets not in `change` are carried from the current combo.
 */
export function applyFusionChange(
  descriptor: FusionDescriptor,
  currentModelUid: string,
  change: Partial<FusionSelection>,
): string {
  const combo = currentFusionCombo(descriptor, currentModelUid);
  return resolveFusionModelUid(descriptor, {
    lead: change.lead ?? combo.lead,
    effort: change.effort ?? combo.effort,
    fast: change.fast ?? combo.fast,
    sidekick: change.sidekick ?? combo.sidekick,
    priority: change.priority ?? combo.priority,
  });
}
