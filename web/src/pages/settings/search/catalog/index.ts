// Every searchable Settings row, by section. A new section is a type error here until it
// lists its rows, even if it has none yet.

import type { SettingsSectionId } from "@/shell/settingsNav";

import { ACCOUNT_SETTINGS } from "./account";
import { APPEARANCE_SETTINGS } from "./appearance";
import { ARCHIVED_SETTINGS } from "./archived";
import { CLI_SETTINGS } from "./cli";
import { CUSTOM_AGENTS_SETTINGS } from "./customAgents";
import { GENERAL_SETTINGS } from "./general";
import { GIT_SETTINGS } from "./git";
import { HARNESS_SETTINGS } from "./harnesses";
import { IMPORT_SETTINGS } from "./import";
import { INTEGRATION_SETTINGS } from "./integrations";
import { MCP_SETTINGS } from "./mcp";
import { MEMBERS_SETTINGS } from "./members";
import { POLICIES_SETTINGS } from "./policies";
import { SHARING_SETTINGS } from "./sharing";
import type { SettingsEntry } from "./types";
import { UPDATES_SETTINGS } from "./updates";

export const SECTION_CATALOGS: Readonly<Record<SettingsSectionId, readonly SettingsEntry[]>> = {
  appearance: APPEARANCE_SETTINGS,
  harnesses: HARNESS_SETTINGS,
  "custom-agents": CUSTOM_AGENTS_SETTINGS,
  general: GENERAL_SETTINGS,
  git: GIT_SETTINGS,
  integrations: INTEGRATION_SETTINGS,
  mcp: MCP_SETTINGS,
  shortcuts: [],
  import: IMPORT_SETTINGS,
  account: ACCOUNT_SETTINGS,
  admin: [],
  members: MEMBERS_SETTINGS,
  policies: POLICIES_SETTINGS,
  sharing: SHARING_SETTINGS,
  archived: ARCHIVED_SETTINGS,
  cli: CLI_SETTINGS,
  updates: UPDATES_SETTINGS,
};

export const SETTINGS_ENTRIES: readonly SettingsEntry[] = Object.values(SECTION_CATALOGS).flat();

const ENTRIES_BY_ID = new Map(SETTINGS_ENTRIES.map((entry) => [entry.id, entry]));

export function settingsEntryById(id: string): SettingsEntry | undefined {
  return ENTRIES_BY_ID.get(id);
}
