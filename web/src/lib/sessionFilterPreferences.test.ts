import { afterEach, describe, expect, it, vi } from "vitest";
import {
  DEFAULT_SESSION_FILTER,
  readSessionFilter,
  writeSessionFilter,
} from "./sessionFilterPreferences";

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

describe("sessionFilterPreferences", () => {
  it('defaults to "all" when nothing is stored', () => {
    // A first-time viewer sees the whole list, as before persistence existed.
    expect(DEFAULT_SESSION_FILTER).toBe("all");
    expect(readSessionFilter(true)).toBe("all");
  });

  it("round-trips every filter value", () => {
    for (const filter of ["all", "mine", "shared", "archived"] as const) {
      writeSessionFilter(filter);
      expect(readSessionFilter(true)).toBe(filter);
    }
  });

  it("falls back to the default for an unknown stored value", () => {
    // Guards against a hand-edited entry or a filter this build dropped:
    // scoping the list to a slice with no menu entry would strand the viewer.
    localStorage.setItem("omnigent:session-filter", "starred");
    expect(readSessionFilter(true)).toBe("all");

    localStorage.setItem("omnigent:session-filter", "");
    expect(readSessionFilter(true)).toBe("all");
  });

  it('ignores a stored "shared" on a single-user server', () => {
    // A loopback-only server drops "Shared sessions" from the menu, so honoring
    // it would show an empty list the viewer has no control to leave.
    writeSessionFilter("shared");
    expect(readSessionFilter(false)).toBe("all");
    // Still honored where the option exists.
    expect(readSessionFilter(true)).toBe("shared");
  });

  it("keeps the other filters on a single-user server", () => {
    writeSessionFilter("archived");
    expect(readSessionFilter(false)).toBe("archived");
  });

  it("never throws when storage is inaccessible", () => {
    // Private-mode / quota failures surface as throws from the Storage API.
    // Both helpers must swallow them — a broken preference must not break the
    // sidebar.
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota exceeded");
    });
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("access denied");
    });
    expect(() => writeSessionFilter("mine")).not.toThrow();
    expect(readSessionFilter(true)).toBe("all");
  });
});
