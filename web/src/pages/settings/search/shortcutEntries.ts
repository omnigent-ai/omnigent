// Keyboard shortcut rows as search entries, generated from the rows the Keyboard shortcuts
// page lists, so the two can't disagree. An entry keeps the binding id, not key strings: the
// keys come from the live page row wherever an entry is shown.

import type { ShortcutPageGroup, ShortcutPageRow } from "@/lib/shortcuts/shortcutPage";

import type { SettingsEntry } from "./catalog/types";

const SHORTCUT_KEYWORDS = ["shortcut", "keyboard", "hotkey", "keys"] as const;

// One object per binding and heading, so rows and effects that depend on an entry stay stable.
const entryCache = new Map<string, SettingsEntry>();

/** A page row the registry binds: its label and keys come from the binding. */
export type BoundShortcutRow = ShortcutPageRow & {
  shortcutId: NonNullable<ShortcutPageRow["shortcutId"]>;
};

export function isBoundShortcutRow(row: ShortcutPageRow): row is BoundShortcutRow {
  return row.shortcutId !== undefined;
}

export function shortcutSettingsEntryId(row: Pick<BoundShortcutRow, "shortcutId">): string {
  return `shortcut.${row.shortcutId}`;
}

/** The entry for one page row, listed under `group`'s heading. */
export function shortcutSettingsEntry(
  row: BoundShortcutRow,
  group: ShortcutPageGroup,
): SettingsEntry {
  const id = shortcutSettingsEntryId(row);
  const cacheKey = `${id}\u0000${row.label}\u0000${group.title}`;
  const cached = entryCache.get(cacheKey);
  if (cached) return cached;
  const entry: SettingsEntry = {
    id,
    section: "shortcuts",
    label: row.label,
    description: group.title,
    keywords: SHORTCUT_KEYWORDS,
    shortcutId: row.shortcutId,
  };
  entryCache.set(cacheKey, entry);
  return entry;
}

/** Every registry-bound row the page lists for these groups, in page order. */
export function shortcutSettingsEntries(groups: readonly ShortcutPageGroup[]): SettingsEntry[] {
  return groups.flatMap((group) =>
    group.items.filter(isBoundShortcutRow).map((row) => shortcutSettingsEntry(row, group)),
  );
}
