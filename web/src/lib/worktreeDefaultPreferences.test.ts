import { afterEach, describe, expect, it, vi } from "vitest";
import { readAlwaysUseWorktree, writeAlwaysUseWorktree } from "./worktreeDefaultPreferences";

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

describe("worktreeDefaultPreferences", () => {
  it("defaults to off when nothing is stored", () => {
    // Absence of the key is the off state — new sessions start directly in the
    // workspace unless the user opted in.
    expect(readAlwaysUseWorktree()).toBe(false);
  });

  it("round-trips the on state", () => {
    writeAlwaysUseWorktree(true);
    expect(readAlwaysUseWorktree()).toBe(true);
  });

  it("clears the key when written off (absence is off)", () => {
    writeAlwaysUseWorktree(true);
    writeAlwaysUseWorktree(false);
    expect(readAlwaysUseWorktree()).toBe(false);
    expect(localStorage.getItem("omnigent:always-use-worktree")).toBeNull();
  });

  it("only the literal 'true' reads as on (defensive against hand edits)", () => {
    // A stale or hand-edited value that isn't exactly "true" must not enable it.
    localStorage.setItem("omnigent:always-use-worktree", "1");
    expect(readAlwaysUseWorktree()).toBe(false);

    localStorage.setItem("omnigent:always-use-worktree", "true");
    expect(readAlwaysUseWorktree()).toBe(true);
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
    expect(() => writeAlwaysUseWorktree(true)).not.toThrow();
    expect(readAlwaysUseWorktree()).toBe(false);
  });
});
