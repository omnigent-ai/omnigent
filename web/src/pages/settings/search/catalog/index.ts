// Every searchable Settings row, by section. Sections add their rows here as they move onto
// the catalog; until then the list is empty, so no row is revealed by a link.

import type { SettingsEntry } from "./types";

export const SETTINGS_ENTRIES: readonly SettingsEntry[] = [];

const ENTRIES_BY_ID = new Map(SETTINGS_ENTRIES.map((entry) => [entry.id, entry]));

export function settingsEntryById(id: string): SettingsEntry | undefined {
  return ENTRIES_BY_ID.get(id);
}
