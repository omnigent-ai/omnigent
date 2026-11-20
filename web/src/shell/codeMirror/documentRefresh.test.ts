import { describe, expect, it } from "vitest";
import { textChange } from "./documentRefresh";

function apply(previous: string, next: string): string {
  const change = textChange(previous, next);
  if (!change) return previous;
  return previous.slice(0, change.from) + change.insert + previous.slice(change.to);
}

describe("textChange", () => {
  it("returns null for identical snapshots", () => {
    expect(textChange("same", "same")).toBeNull();
  });

  it("replaces only the changed middle", () => {
    expect(textChange("one\ntwo\nthree", "one\nTWO\nthree")).toEqual({
      from: 4,
      to: 7,
      insert: "TWO",
    });
  });

  it.each([
    ["an append", "a\nb", "a\nb\nc"],
    ["a prepend", "b\nc", "a\nb\nc"],
    ["a deletion", "a\nb\nc", "a\nc"],
    ["a repeated character", "aaaa", "aaa"],
    ["an emptied file", "text", ""],
    ["a full rewrite", "abc", "xyz"],
  ])("reproduces the next snapshot after %s", (_label, previous, next) => {
    expect(apply(previous, next)).toBe(next);
  });
});
