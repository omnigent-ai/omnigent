// What Settings search finds: one target per catalog row and one per named part, ranked by
// a list of matchers. Targets hold only the catalog's own strings, never a current value.

import {
  compareItemMatches,
  type ItemMatch,
  itemQueryTokens,
  matchItem,
} from "@/components/ui/quick-pick/itemRanking";
import { matchText, type MatchRange } from "@/components/ui/quick-pick/match";
import type { SettingsSectionId } from "@/shell/settingsNav";

import type { SettingsAvailability, SettingsEntry } from "./catalog/types";
import { settingsControlId, settingsItemPath, settingsRevealPart } from "./settingsControl";

export interface SettingsSearchTarget {
  /** The row (or part) this result reveals: `entry.id`, or `entry.id:part`. */
  key: string;
  entry: SettingsEntry;
  part?: string;
  /** What the result is called: the row's label, or the part's name. */
  label: string;
  /** Matched after the label: the row's description, or the row's label for a part. */
  description: string;
  /** Matched last: a row's keywords, choices and section name; a part has none of its own. */
  keywords: readonly string[];
  /** Shown under the label. */
  place: string;
}

export interface SettingsSectionInfo {
  id: SettingsSectionId;
  label: string;
}

/**
 * Search targets for the catalog's `entries`, limited to rows in a section listed in `sections`
 * and to rows `availability` shows. A row's parts are its targets too, each listed once.
 */
export function settingsSearchTargets(
  entries: readonly SettingsEntry[],
  sections: readonly SettingsSectionInfo[],
  availability: SettingsAvailability,
): SettingsSearchTarget[] {
  const sectionLabels = new Map(sections.map((section) => [section.id, section.label]));
  const shown = entries.filter((entry) => entry.available?.(availability) ?? true);
  return shown.flatMap((entry) => {
    const sectionLabel = sectionLabels.get(entry.section);
    if (sectionLabel === undefined) return [];
    const keywords = entry.keywords ?? [];
    const row: SettingsSearchTarget = {
      key: entry.id,
      entry,
      label: entry.label,
      description: entry.description,
      keywords: [...keywords, ...(entry.choices ?? []), sectionLabel],
      place: sectionLabel,
    };
    const parts = Object.entries(entry.parts ?? {}).map(([part, name]): SettingsSearchTarget => {
      // A part reached through another control is placed under that control, by its catalog name.
      const reveal = settingsRevealPart(entry, part);
      const opener = reveal === part || reveal === "" ? undefined : entry.parts?.[reveal ?? ""];
      return {
        key: settingsControlId(entry, part),
        entry,
        part,
        // A part is found by its own name, its row's, or its static choices, such as a tab's;
        // not by the row's keywords, and never by a counted choice's count.
        label: name,
        description: entry.label,
        keywords: [...(entry.partChoices?.[part] ?? [])],
        place: opener
          ? `${opener} · ${entry.label} · ${sectionLabel}`
          : `${entry.label} · ${sectionLabel}`,
      };
    });
    return [row, ...parts];
  });
}

/** Where choosing `target` goes: its row, its part, or the control that leads to that part. */
export function settingsSearchPath(target: SettingsSearchTarget): string {
  return settingsItemPath(target.entry, settingsRevealPart(target.entry, target.part));
}

/** How well one matcher matched a target: lower tiers rank first. */
export interface SettingsSearchMatch {
  tier: number;
  /** Orders matches within a tier, for matchers that measure quality. */
  quality?: ItemMatch;
}

/** Prepares a query once, then matches each target against it; null when it doesn't apply. */
export type SettingsSearchMatcher = (
  query: string,
) => ((target: SettingsSearchTarget) => SettingsSearchMatch | null) | null;

/** Text tiers start here, leaving tier 0 for any matcher that ranks above text. */
export const TEXT_TIER_BASE = 1;

/** The label, then the description, then keywords, as ⌘K ranks its rows. */
export const textMatcher: SettingsSearchMatcher = (query) => {
  const tokens = itemQueryTokens(query);
  if (tokens.length === 0) return null;
  return (target) => {
    const match = matchItem(
      tokens,
      {
        id: target.key,
        label: target.label,
        description: target.description,
        keywords: target.keywords,
        value: null,
      },
      "words",
    );
    return match ? { tier: TEXT_TIER_BASE + match.tier, quality: match } : null;
  };
};

export const SETTINGS_SEARCH_MATCHERS: readonly SettingsSearchMatcher[] = [textMatcher];

export interface SettingsSearchResult {
  target: SettingsSearchTarget;
  labelRanges: readonly MatchRange[];
  placeRanges: readonly MatchRange[];
}

function compareMatches(first: SettingsSearchMatch, second: SettingsSearchMatch): number {
  if (first.tier !== second.tier) return first.tier - second.tier;
  return first.quality && second.quality ? compareItemMatches(first.quality, second.quality) : 0;
}

/**
 * The targets that match `query`, best first. Each target appears once, at its best match
 * across matchers; ties keep catalog order.
 */
export function searchSettings(
  query: string,
  targets: readonly SettingsSearchTarget[],
  matchers: readonly SettingsSearchMatcher[] = SETTINGS_SEARCH_MATCHERS,
): SettingsSearchResult[] {
  if (query.trim() === "") return [];
  const prepared = matchers.flatMap((matcher) => {
    const match = matcher(query);
    return match ? [match] : [];
  });
  const best = new Map<
    string,
    { target: SettingsSearchTarget; match: SettingsSearchMatch; order: number }
  >();
  targets.forEach((target, order) => {
    for (const match of prepared) {
      const result = match(target);
      if (!result) continue;
      const current = best.get(target.key);
      if (!current || compareMatches(result, current.match) < 0) {
        best.set(target.key, { target, match: result, order: current?.order ?? order });
      }
    }
  });
  return [...best.values()]
    .sort(
      (first, second) => compareMatches(first.match, second.match) || first.order - second.order,
    )
    .map(({ target }) => ({
      target,
      labelRanges: matchText(query, target.label) ?? [],
      placeRanges: matchText(query, target.place) ?? [],
    }));
}
