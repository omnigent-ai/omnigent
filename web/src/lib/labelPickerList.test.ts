// What the label picker lists: recent labels first, then the repository's, then GitHub's matches
// for typed text, each label once; and which recent labels need checking with GitHub.

import { describe, expect, it } from "vitest";

import { labelPickerGroups, recentLabelsToCheck } from "./labelPickerList";

const label = (name: string, color: string | null = "ededed") => ({
  name,
  color,
  description: null,
});

describe("labelPickerGroups", () => {
  it("lists recent labels first, with the repository's current colors, then the rest", () => {
    const groups = labelPickerGroups({
      listed: [label("automerge", "c2e0c6"), label("bug"), label("docs")],
      recent: [label("Automerge", "000000"), label("past-the-list")],
    });
    expect(groups.recent).toEqual([label("automerge", "c2e0c6"), label("past-the-list")]);
    expect(groups.listed.map((entry) => entry.name)).toEqual(["bug", "docs"]);
    expect(groups.found).toEqual([]);
  });

  it("adds GitHub's matches that neither group holds, once each", () => {
    const groups = labelPickerGroups({
      listed: [label("bug")],
      recent: [label("automerge")],
      matches: [label("Bug"), label("automerge"), label("auto-revert"), label("AUTO-REVERT")],
    });
    expect(groups.found.map((entry) => entry.name)).toEqual(["auto-revert"]);
  });

  it("keeps the labels checked while open, after the matches, once each", () => {
    const groups = labelPickerGroups({
      listed: [label("bug")],
      recent: [label("automerge")],
      matches: [label("auto-revert")],
      chosen: [label("BUG"), label("Auto-Revert"), label("far-past-the-list")],
    });
    expect(groups.listed.map((entry) => entry.name)).toEqual(["bug"]);
    expect(groups.found.map((entry) => entry.name)).toEqual(["auto-revert", "far-past-the-list"]);
  });
});

describe("recentLabelsToCheck", () => {
  it("checks recent labels an incomplete list lacks, and none when the list is complete", () => {
    const listed = [label("bug")];
    const recent = [label("BUG"), label("past-the-list")];
    expect(recentLabelsToCheck(listed, true, recent)).toEqual(["past-the-list"]);
    expect(recentLabelsToCheck(listed, false, recent)).toEqual([]);
  });
});
