import { describe, expect, it } from "vitest";

import { reorderStripEntries, type StripReorderEntry } from "./stripReorder";

const tab = (id: string): StripReorderEntry<string> => ({ key: id, tabIds: [id] });
const split = (key: string, ...tabIds: string[]): StripReorderEntry<string> => ({ key, tabIds });

describe("reorderStripEntries", () => {
  const strip = [tab("A"), tab("B"), tab("C")];

  it("moves a tab dragged forward past the one it's dropped on", () => {
    expect(reorderStripEntries(strip, "A", "C")).toEqual(["B", "C", "A"]);
  });

  it("moves a tab dragged back before the one it's dropped on", () => {
    expect(reorderStripEntries(strip, "C", "A")).toEqual(["C", "A", "B"]);
  });

  it("changes nothing for a drop where the drag started or on an unknown entry", () => {
    expect(reorderStripEntries(strip, "B", "B")).toBeNull();
    expect(reorderStripEntries(strip, "B", "gone")).toBeNull();
    expect(reorderStripEntries(strip, "gone", "B")).toBeNull();
  });

  // The split's panes S1 and S2 sit apart in the tab order, but the strip shows them once.
  const withSplit = [tab("A"), split("group:g", "S1", "S2"), tab("B"), tab("C")];

  it("moves a tab across a split as one entry, in both directions", () => {
    expect(reorderStripEntries(withSplit, "A", "group:g")).toEqual(["S1", "S2", "A", "B", "C"]);
    expect(reorderStripEntries(withSplit, "C", "group:g")).toEqual(["A", "C", "S1", "S2", "B"]);
  });

  it("moves a split with its panes together and in order, in both directions", () => {
    expect(reorderStripEntries(withSplit, "group:g", "C")).toEqual(["A", "B", "C", "S1", "S2"]);
    expect(reorderStripEntries(withSplit, "group:g", "A")).toEqual(["S1", "S2", "A", "B", "C"]);
  });

  it("moves a split past another split", () => {
    const twoSplits = [split("group:g", "S1", "S2"), tab("A"), split("group:h", "T1", "T2")];
    expect(reorderStripEntries(twoSplits, "group:g", "group:h")).toEqual([
      "A",
      "T1",
      "T2",
      "S1",
      "S2",
    ]);
  });
});
