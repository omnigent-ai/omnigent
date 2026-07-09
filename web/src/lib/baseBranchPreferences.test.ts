import { afterEach, describe, expect, it, vi } from "vitest";
import { readDefaultBaseBranch, writeDefaultBaseBranch } from "./baseBranchPreferences";

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

describe("baseBranchPreferences", () => {
  it("returns null when nothing is stored", () => {
    // No default set — read must say so (null) so the composer leaves the
    // base-branch field blank rather than inventing a branch.
    expect(readDefaultBaseBranch()).toBeNull();
  });

  it("round-trips a written branch name", () => {
    writeDefaultBaseBranch("main");
    // The exact branch written must come back — this is what pre-fills the
    // base-branch field on the next new-branch entry.
    expect(readDefaultBaseBranch()).toBe("main");
  });

  it("trims surrounding whitespace before storing", () => {
    writeDefaultBaseBranch("  develop  ");
    expect(readDefaultBaseBranch()).toBe("develop");
  });

  it("normalizes a raw stored value on read (defensive against hand edits)", () => {
    // A value that bypassed the writer (hand-edited storage, stale entry) must
    // still read back trimmed, and a whitespace-only entry reads as unset.
    localStorage.setItem("omnigent:default-base-branch", "  develop  ");
    expect(readDefaultBaseBranch()).toBe("develop");

    localStorage.setItem("omnigent:default-base-branch", "   ");
    expect(readDefaultBaseBranch()).toBeNull();
  });

  it("clears the preference when written blank", () => {
    writeDefaultBaseBranch("main");
    writeDefaultBaseBranch("   ");
    // A blank value turns auto-fill off — the slot is emptied, not stored as "".
    expect(readDefaultBaseBranch()).toBeNull();
  });

  it("overwrites the previous value", () => {
    writeDefaultBaseBranch("main");
    writeDefaultBaseBranch("develop");
    // Only the latest value matters; the preference is a single slot.
    expect(readDefaultBaseBranch()).toBe("develop");
  });

  it("never throws when storage is inaccessible", () => {
    // Private-mode / quota failures surface as throws from the Storage API.
    // Both helpers must swallow them — a broken preference must not break
    // settings.
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota exceeded");
    });
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("access denied");
    });
    expect(() => writeDefaultBaseBranch("main")).not.toThrow();
    expect(readDefaultBaseBranch()).toBeNull();
  });
});
