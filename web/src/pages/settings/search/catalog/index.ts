// Every searchable Settings row, by section. A new section is a type error here until it
// lists its rows, even if it has none yet.

import type { SettingsSectionId } from "@/shell/settingsNav";

import { ARCHIVED_SETTINGS } from "./archived";
import { CLI_SETTINGS } from "./cli";
import { IMPORT_SETTINGS } from "./import";
import type { SettingsEntry } from "./types";
import { UPDATES_SETTINGS } from "./updates";

export const SECTION_CATALOGS: Readonly<Record<SettingsSectionId, readonly SettingsEntry[]>> = {
  appearance: [],
  harnesses: [],
  "custom-agents": [],
  general: [],
  git: [],
  integrations: [],
  mcp: [],
  shortcuts: [],
  import: IMPORT_SETTINGS,
  account: [],
  admin: [],
  members: [],
  policies: [],
  sharing: [],
  archived: ARCHIVED_SETTINGS,
  cli: CLI_SETTINGS,
  updates: UPDATES_SETTINGS,
};

export const SETTINGS_ENTRIES: readonly SettingsEntry[] = Object.values(SECTION_CATALOGS).flat();

const ENTRIES_BY_ID = new Map(SETTINGS_ENTRIES.map((entry) => [entry.id, entry]));

export function settingsEntryById(id: string): SettingsEntry | undefined {
  return ENTRIES_BY_ID.get(id);
}
