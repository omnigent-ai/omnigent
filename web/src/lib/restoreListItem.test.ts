import { describe, expect, it } from "vitest";
import { restoreListItem } from "./restoreListItem";

const id = (entry: { id: string }) => entry.id;
const items = (...ids: string[]) => ids.map((value) => ({ id: value }));

describe("restoreListItem", () => {
  it("puts the item back before its next surviving neighbour", () => {
    expect(restoreListItem(items("a", "c"), items("a", "b", "c"), id, "b")).toEqual(
      items("a", "b", "c"),
    );
  });

  it("appends when nothing that followed it is still present", () => {
    // b's snapshot was taken after a left; a has since come back.
    expect(restoreListItem(items("a"), items("b"), id, "b")).toEqual(items("a", "b"));
  });

  it("keeps removals made since the snapshot", () => {
    expect(restoreListItem(items("c"), items("a", "b", "c"), id, "a")).toEqual(items("a", "c"));
  });

  it("returns the list unchanged when the item is already back or unknown", () => {
    const current = items("a", "b");
    expect(restoreListItem(current, items("a", "b"), id, "b")).toBe(current);
    expect(restoreListItem(current, items("a", "b"), id, "z")).toBe(current);
  });
});
