import { afterEach, describe, expect, it } from "vitest";
import { inboxProjectOptions, readInboxProject, writeInboxProject } from "./inboxProjectFilter";

const names: Record<string, string> = { a: "alpha", b: "Beta", c: "gamma" };
const describeProject = (id: string) => ({ name: names[id] ?? "Unknown project" });

describe("inboxProjectOptions", () => {
  it("lists only projects with items, counted and sorted by name", () => {
    const entries = ["b", "a", null, "b", "c", null];
    expect(inboxProjectOptions(entries, (id) => id, describeProject, null)).toEqual([
      { id: "a", name: "alpha", count: 1 },
      { id: "b", name: "Beta", count: 2 },
      { id: "c", name: "gamma", count: 1 },
    ]);
  });

  it("keeps the selected project listed with a zero count", () => {
    expect(inboxProjectOptions(["a"], (id) => id, describeProject, "c")).toEqual([
      { id: "a", name: "alpha", count: 1 },
      { id: "c", name: "gamma", count: 0 },
    ]);
  });
});

describe("inbox project memory", () => {
  afterEach(() => window.localStorage.clear());

  it("round-trips and forgets the pick", () => {
    expect(readInboxProject()).toBeNull();
    writeInboxProject("a");
    expect(readInboxProject()).toBe("a");
    writeInboxProject(null);
    expect(readInboxProject()).toBeNull();
  });
});
