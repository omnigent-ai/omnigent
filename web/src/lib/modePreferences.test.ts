import { afterEach, describe, expect, it, vi } from "vitest";
import { readLastModeForHarness, writeLastModeForHarness } from "./modePreferences";

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

describe("modePreferences", () => {
  it("returns null when nothing is stored for a harness", () => {
    // A first-time visitor has no pick on record — read must say so (null)
    // so the composer seeds the harness default.
    expect(readLastModeForHarness("claude-native")).toBeNull();
  });

  it("returns null for a null/empty harness", () => {
    writeLastModeForHarness("claude-native", "auto");
    expect(readLastModeForHarness(null)).toBeNull();
    expect(readLastModeForHarness(undefined)).toBeNull();
    expect(readLastModeForHarness("")).toBeNull();
  });

  it("round-trips a written mode", () => {
    writeLastModeForHarness("claude-native", "plan");
    expect(readLastModeForHarness("claude-native")).toBe("plan");
  });

  it("keeps each harness's pick independent", () => {
    // The whole point: a Codex pick must not leak into Claude Code's slot.
    writeLastModeForHarness("claude-native", "auto");
    writeLastModeForHarness("codex-native", "full-access");
    writeLastModeForHarness("cursor-native", "yolo");
    expect(readLastModeForHarness("claude-native")).toBe("auto");
    expect(readLastModeForHarness("codex-native")).toBe("full-access");
    expect(readLastModeForHarness("cursor-native")).toBe("yolo");
  });

  it("overwrites the previous pick for the same harness", () => {
    writeLastModeForHarness("claude-native", "auto");
    writeLastModeForHarness("claude-native", "plan");
    expect(readLastModeForHarness("claude-native")).toBe("plan");
  });

  it("ignores a null/empty harness on write", () => {
    writeLastModeForHarness(null, "auto");
    writeLastModeForHarness("", "auto");
    expect(localStorage.getItem("omnigent:last-mode-by-harness")).toBeNull();
  });

  it("tolerates a corrupted blob", () => {
    localStorage.setItem("omnigent:last-mode-by-harness", "not json{");
    expect(readLastModeForHarness("claude-native")).toBeNull();
    // A later write recovers — it doesn't propagate the corruption.
    writeLastModeForHarness("claude-native", "plan");
    expect(readLastModeForHarness("claude-native")).toBe("plan");
  });

  it("never throws when storage is inaccessible", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota exceeded");
    });
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("access denied");
    });
    expect(() => writeLastModeForHarness("claude-native", "auto")).not.toThrow();
    expect(readLastModeForHarness("claude-native")).toBeNull();
  });
});
