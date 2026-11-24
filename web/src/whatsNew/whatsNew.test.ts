import { describe, expect, it } from "vitest";

import { parseEntries, unseenEntries, WHATS_NEW_ENTRIES } from "./whatsNew";

describe("parseEntries", () => {
  it("takes the date from the file name and the title from the first heading, newest first", () => {
    const entries = parseEntries({
      "./entries/2026-11-20.md": "# Older day\n\n- one",
      "./entries/2026-11-22.md": "﻿# Newer day\n\n- two\n- three\n",
      "./entries/notes.md": "# Not an entry",
    });
    expect(entries).toEqual([
      { date: "2026-11-22", title: "Newer day", body: "- two\n- three" },
      { date: "2026-11-20", title: "Older day", body: "- one" },
    ]);
  });

  it("parses the bundled entries", () => {
    expect(WHATS_NEW_ENTRIES.length).toBeGreaterThan(0);
    for (const entry of WHATS_NEW_ENTRIES) {
      expect(entry.date).toMatch(/^\d{4}-\d{2}-\d{2}$/);
      expect(entry.title).not.toBe("");
      expect(entry.body).not.toBe("");
    }
  });
});

describe("unseenEntries", () => {
  const entries = parseEntries({
    "./entries/2026-11-23.md": "# C\n\nc",
    "./entries/2026-11-20.md": "# B\n\nb",
    "./entries/2026-10-01.md": "# A\n\na",
  });
  const now = new Date("2026-11-24T12:00:00Z");

  it("returns entries after the last-seen date, across several days", () => {
    expect(unseenEntries(entries, "2026-11-19", now).map((e) => e.date)).toEqual([
      "2026-11-23",
      "2026-11-20",
    ]);
  });

  it("returns nothing once the newest entry is seen", () => {
    expect(unseenEntries(entries, "2026-11-23", now)).toEqual([]);
  });

  it("limits a first visit to the recent window", () => {
    expect(unseenEntries(entries, null, now).map((e) => e.date)).toEqual([
      "2026-11-23",
      "2026-11-20",
    ]);
  });
});
