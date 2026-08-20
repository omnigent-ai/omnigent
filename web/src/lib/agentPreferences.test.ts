import { afterEach, describe, expect, it, vi } from "vitest";
import { readLastAgentId, writeLastAgentId } from "./agentPreferences";

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

describe("agentPreferences", () => {
  it("returns null when nothing is stored", () => {
    // A first-time visitor has no pick on record — read must say so (null)
    // rather than invent an id, so the composer falls back to its default.
    expect(readLastAgentId()).toBeNull();
  });

  it("round-trips a written agent id", () => {
    writeLastAgentId("ag_polly");
    // The exact id written must come back — this is what seeds the picker
    // on the next visit.
    expect(readLastAgentId()).toBe("ag_polly");
  });

  it("overwrites the previous pick", () => {
    writeLastAgentId("ag_one");
    writeLastAgentId("ag_two");
    // Only the latest pick matters; the preference is a single slot.
    expect(readLastAgentId()).toBe("ag_two");
  });

  it("never throws when storage is inaccessible", () => {
    // Private-mode / quota failures surface as throws from the Storage API.
    // Both helpers must swallow them — a broken preference must not break
    // session creation.
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota exceeded");
    });
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("access denied");
    });
    expect(() => writeLastAgentId("ag_x")).not.toThrow();
    expect(readLastAgentId()).toBeNull();
  });
});
