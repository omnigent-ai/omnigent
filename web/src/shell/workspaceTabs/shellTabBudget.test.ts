import { describe, expect, it } from "vitest";

import { MOUNTED_SHELL_TAB_LIMIT, mountedShellKeys, noteShownShell } from "./shellTabBudget";

describe("noteShownShell", () => {
  it("moves the shell just shown to the front, once", () => {
    expect(noteShownShell(["a", "b", "c"], "c")).toEqual(["c", "a", "b"]);
    expect(noteShownShell(["a"], "b")).toEqual(["b", "a"]);
    const recent = ["a", "b"];
    expect(noteShownShell(recent, "a")).toBe(recent);
  });
});

describe("mountedShellKeys", () => {
  it("keeps the selected shell, then the most recently shown open ones, up to the limit", () => {
    expect(
      mountedShellKeys({
        recent: ["b", "c", "d"],
        open: ["a", "b", "c", "d"],
        selected: "a",
        shown: true,
        limit: 3,
      }),
    ).toEqual(["a", "b", "c"]);
  });

  it("mounts all of a selected split larger than the limit, and no hidden tab beside it", () => {
    const members = ["a", "b", "c", "d"];
    expect(
      mountedShellKeys({
        recent: ["e"],
        open: [...members, "e"],
        selected: "a",
        shown: true,
        unitOf: (key) => (members.includes(key) ? members : [key]),
        limit: 3,
      }),
    ).toEqual(members);
  });

  it("leaves out shells whose tabs closed, and the selected one counts only once", () => {
    expect(
      mountedShellKeys({
        recent: ["a", "gone", "b"],
        open: ["a", "b"],
        selected: "a",
        shown: true,
      }),
    ).toEqual(["a", "b"]);
  });

  it("keeps only the selected shell while the sidebar is closed, or none", () => {
    expect(
      mountedShellKeys({ recent: ["b"], open: ["a", "b"], selected: "a", shown: false }),
    ).toEqual(["a"]);
    expect(
      mountedShellKeys({ recent: ["b"], open: ["a", "b"], selected: null, shown: false }),
    ).toEqual([]);
  });

  it("keeps hidden shells with no shell tab selected, up to the limit", () => {
    const open = Array.from({ length: MOUNTED_SHELL_TAB_LIMIT + 1 }, (_, index) => `s${index}`);
    expect(mountedShellKeys({ recent: open, open, selected: null, shown: true })).toEqual(
      open.slice(0, MOUNTED_SHELL_TAB_LIMIT),
    );
  });
});

describe("mountedShellKeys with splits", () => {
  const pairs: Record<string, string[]> = { a: ["a", "b"], b: ["a", "b"], p: ["p", "q", "r"] };
  const unitOf = (key: string) => pairs[key] ?? [key];

  it("mounts the selected tab's whole split, each member counted against the limit", () => {
    expect(
      mountedShellKeys({
        recent: ["c", "d"],
        open: ["a", "c", "d"],
        selected: "a",
        shown: true,
        unitOf,
        limit: 3,
      }),
    ).toEqual(["a", "b", "c"]);
  });

  it("keeps a recent split whole or not at all, and fills the room left with later shells", () => {
    const open = ["x", "p", "s"];
    expect(
      mountedShellKeys({ recent: ["p", "s"], open, selected: "x", shown: true, unitOf, limit: 4 }),
    ).toEqual(["x", "p", "q", "r"]);
    expect(
      mountedShellKeys({ recent: ["p", "s"], open, selected: "x", shown: true, unitOf, limit: 3 }),
    ).toEqual(["x", "s"]);
  });

  it("keeps only the selected tab's split while the sidebar is closed", () => {
    expect(
      mountedShellKeys({ recent: ["c"], open: ["a", "c"], selected: "a", shown: false, unitOf }),
    ).toEqual(["a", "b"]);
  });

  it("stays within the limit when the selected split joins the newest shell and an evicted one", () => {
    const open = Array.from({ length: 9 }, (_, index) => `s${index}`);
    // s8 was shown last; s0, shown first, fell out; now they're one split.
    const recent = [...open].reverse();
    const joined = (key: string) => (key === "s8" || key === "s0" ? ["s8", "s0"] : [key]);
    const mounted = mountedShellKeys({ recent, open, selected: "s8", shown: true, unitOf: joined });
    expect(mounted).toHaveLength(MOUNTED_SHELL_TAB_LIMIT);
    expect(mounted.slice(0, 2)).toEqual(["s8", "s0"]);
  });
});
