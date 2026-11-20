import { describe, expect, it } from "vitest";

import { CUSTOM_AGENT_CREATE_ENTRY } from "./customAgents";
import { SECTION_CATALOGS, SETTINGS_ENTRIES, settingsEntryById } from "./index";
import { settingsEntryProblems } from "./types";

describe("Settings catalog", () => {
  it("indexes the custom agent create page so Settings search can find it", () => {
    expect(SETTINGS_ENTRIES).toContain(CUSTOM_AGENT_CREATE_ENTRY);
  });

  it("files each entry under the section it names", () => {
    for (const [section, entries] of Object.entries(SECTION_CATALOGS)) {
      for (const entry of entries) expect(entry.section).toBe(section);
    }
  });

  it("gives every entry a unique id that resolves back to it", () => {
    const ids = SETTINGS_ENTRIES.map((entry) => entry.id);
    expect(new Set(ids).size).toBe(ids.length);
    for (const entry of SETTINGS_ENTRIES) expect(settingsEntryById(entry.id)).toBe(entry);
  });

  it("passes the entry checks for every entry", () => {
    for (const entry of SETTINGS_ENTRIES) expect(settingsEntryProblems(entry)).toEqual([]);
  });
});
