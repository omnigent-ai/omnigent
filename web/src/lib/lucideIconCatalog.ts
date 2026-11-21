// The full lucide catalog for project marks. Import it only dynamically (see
// components/LucideProjectIcon): a static import pulls every icon into the
// main bundle.

import { icons, type LucideIcon } from "lucide-react";

const ICONS_BY_NAME = new Map<string, LucideIcon>(Object.entries(icons));

/** "ArrowDown01" → "arrow down 01", "ALargeSmall" → "a large small". */
export function lucideIconLabel(name: string): string {
  return name
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .replace(/([A-Z])([A-Z][a-z])/g, "$1 $2")
    .replace(/([a-zA-Z])([0-9])/g, "$1 $2")
    .toLowerCase();
}

const compact = (text: string) => text.toLowerCase().replace(/[\s-]+/g, "");

/** Every icon name, ordered by label. */
export const LUCIDE_ICON_NAMES: readonly string[] = [...ICONS_BY_NAME.keys()].sort((a, b) =>
  lucideIconLabel(a).localeCompare(lucideIconLabel(b)),
);

const COMPACT_LABELS = new Map(LUCIDE_ICON_NAMES.map((name) => [name, compact(name)]));

/** An unknown name (e.g. one a later lucide release renamed) has no icon. */
export function lucideIcon(name: string): LucideIcon | undefined {
  return ICONS_BY_NAME.get(name);
}

/** Names containing the query, ignoring case, spaces and hyphens; prefix matches first. */
export function searchLucideIcons(query: string): readonly string[] {
  const needle = compact(query);
  if (!needle) return LUCIDE_ICON_NAMES;
  const prefixMatches: string[] = [];
  const otherMatches: string[] = [];
  for (const name of LUCIDE_ICON_NAMES) {
    const label = COMPACT_LABELS.get(name) ?? "";
    if (label.startsWith(needle)) prefixMatches.push(name);
    else if (label.includes(needle)) otherMatches.push(name);
  }
  return [...prefixMatches, ...otherMatches];
}
