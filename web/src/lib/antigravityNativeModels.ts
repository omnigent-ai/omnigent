import type { ModelConfigurationSource, NativeModelOption } from "./types";

/** Effort suffixes which agy encodes into otherwise-flat launch model ids. */
const AGY_EFFORTS = ["low", "medium", "high"] as const;

type AntigravityEffort = (typeof AGY_EFFORTS)[number];

export interface AntigravityModelEffort {
  value: AntigravityEffort;
  label: string;
  /** Exact agy model id to send as `model_override`. */
  modelId: string;
}

/** One model row shown by the Antigravity launch picker. */
export interface AntigravityModelGroup {
  id: string;
  displayName: string;
  isDefault: boolean;
  source?: ModelConfigurationSource;
  /** Exact launch id to use when the picker has no retained effort choice. */
  defaultModelId: string;
  efforts: readonly AntigravityModelEffort[];
}

function splitEffortSuffix(id: string): { familyId: string; effort: AntigravityEffort } | null {
  const suffix = id.slice(id.lastIndexOf("-") + 1).toLowerCase();
  if (!AGY_EFFORTS.includes(suffix as AntigravityEffort)) return null;
  const familyId = id.slice(0, -(suffix.length + 1));
  return familyId.startsWith("gemini-") ? { familyId, effort: suffix as AntigravityEffort } : null;
}

function groupLabel(option: NativeModelOption, effort: AntigravityEffort): string {
  const label = option.displayName ?? option.id;
  return label.replace(new RegExp(`(?:[\\s_-]+\\(?${effort}\\)?)$`, "i"), "");
}

/**
 * Group only advertised Gemini effort siblings. Non-Gemini ids and lone
 * siblings remain standalone choices because they offer no supported effort
 * choice to split out.
 */
export function antigravityModelGroups(
  options: readonly NativeModelOption[],
): readonly AntigravityModelGroup[] {
  const siblings = new Map<string, NativeModelOption[]>();
  for (const option of options) {
    const parsed = splitEffortSuffix(option.id);
    if (parsed === null) continue;
    const entries = siblings.get(parsed.familyId) ?? [];
    entries.push(option);
    siblings.set(parsed.familyId, entries);
  }
  const groupedFamilies = new Set(
    Array.from(siblings, ([familyId, entries]) => (entries.length > 1 ? familyId : null)).filter(
      (familyId): familyId is string => familyId !== null,
    ),
  );
  const seenFamilies = new Set<string>();

  return options.flatMap((option): AntigravityModelGroup[] => {
    const parsed = splitEffortSuffix(option.id);
    if (parsed === null || !groupedFamilies.has(parsed.familyId)) {
      return [
        {
          id: option.id,
          displayName: option.displayName ?? option.id,
          isDefault: option.isDefault === true,
          source: option.source,
          defaultModelId: option.id,
          efforts: [],
        },
      ];
    }
    if (seenFamilies.has(parsed.familyId)) return [];
    seenFamilies.add(parsed.familyId);
    const entries = siblings.get(parsed.familyId) ?? [];
    const byEffort = new Map(
      entries.flatMap((entry) => {
        const candidate = splitEffortSuffix(entry.id);
        return candidate === null ? [] : [[candidate.effort, entry] as const];
      }),
    );
    const representative = entries[0]!;
    return [
      {
        id: parsed.familyId,
        displayName: groupLabel(representative, parsed.effort),
        isDefault: entries.some((entry) => entry.isDefault === true),
        source: representative.source,
        defaultModelId: entries.find((entry) => entry.isDefault === true)?.id ?? representative.id,
        efforts: AGY_EFFORTS.flatMap((effort) => {
          const entry = byEffort.get(effort);
          return entry === undefined
            ? []
            : [
                {
                  value: effort,
                  label: effort.charAt(0).toUpperCase() + effort.slice(1),
                  modelId: entry.id,
                },
              ];
        }),
      },
    ];
  });
}

/** Find the picker row which owns an exact agy launch id. */
export function antigravityModelGroupForModel(
  groups: readonly AntigravityModelGroup[],
  modelId: string | null | undefined,
): AntigravityModelGroup | null {
  if (!modelId) return null;
  return (
    groups.find(
      (group) => group.id === modelId || group.efforts.some((effort) => effort.modelId === modelId),
    ) ?? null
  );
}
