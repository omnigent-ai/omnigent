import { afterEach, describe, expect, it, vi } from "vitest";
import { readLastHarness, writeLastHarness } from "./harnessPreferences";

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

describe("harnessPreferences", () => {
  it("returns null when nothing is stored", () => {
    expect(readLastHarness("ag_polly")).toBeNull();
  });

  it("returns null for null/undefined agent id", () => {
    expect(readLastHarness(null)).toBeNull();
    expect(readLastHarness(undefined)).toBeNull();
  });

  it("round-trips a written harness override", () => {
    writeLastHarness("ag_polly", "openai-agents");
    expect(readLastHarness("ag_polly")).toBe("openai-agents");
  });

  it("stores per-agent preferences independently", () => {
    writeLastHarness("ag_polly", "openai-agents");
    writeLastHarness("ag_debby", "claude-sdk");
    expect(readLastHarness("ag_polly")).toBe("openai-agents");
    expect(readLastHarness("ag_debby")).toBe("claude-sdk");
  });

  it("overwrites the previous pick for the same agent", () => {
    writeLastHarness("ag_polly", "openai-agents");
    writeLastHarness("ag_polly", "claude-sdk");
    expect(readLastHarness("ag_polly")).toBe("claude-sdk");
  });

  it("clears the override when null is written", () => {
    writeLastHarness("ag_polly", "openai-agents");
    writeLastHarness("ag_polly", null);
    expect(readLastHarness("ag_polly")).toBeNull();
  });

  it("never throws when storage is inaccessible", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota exceeded");
    });
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("access denied");
    });
    expect(() => writeLastHarness("ag_x", "claude-sdk")).not.toThrow();
    expect(readLastHarness("ag_x")).toBeNull();
  });
});
