import { afterEach, describe, expect, it, vi } from "vitest";
import { DEFAULT_ALWAYS_STEER, readAlwaysSteer, writeAlwaysSteer } from "./alwaysSteerPreferences";

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

describe("alwaysSteerPreferences", () => {
  it("defaults to on (steer mid-turn follow-ups) when nothing is stored", () => {
    // With no stored preference a busy-session follow-up is sent immediately
    // instead of parking in the queue strip.
    expect(DEFAULT_ALWAYS_STEER).toBe(true);
    expect(readAlwaysSteer()).toBe(true);
  });

  it("round-trips both boolean values", () => {
    writeAlwaysSteer(true);
    expect(readAlwaysSteer()).toBe(true);

    writeAlwaysSteer(false);
    expect(readAlwaysSteer()).toBe(false);
  });

  it("falls back to the default for a malformed stored value", () => {
    // Only the exact strings "true" and "false" are honored; garbage or a stale
    // format reads as the default.
    localStorage.setItem("omnigent:always-steer", "1");
    expect(readAlwaysSteer()).toBe(DEFAULT_ALWAYS_STEER);

    localStorage.setItem("omnigent:always-steer", "no");
    expect(readAlwaysSteer()).toBe(DEFAULT_ALWAYS_STEER);

    localStorage.setItem("omnigent:always-steer", "false");
    expect(readAlwaysSteer()).toBe(false);
  });

  it("never throws when storage is inaccessible", () => {
    // Private-mode / quota failures surface as throws from the Storage API.
    // Both helpers must swallow them — a broken preference must not break the
    // composer or settings.
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota exceeded");
    });
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("access denied");
    });
    expect(() => writeAlwaysSteer(false)).not.toThrow();
    expect(readAlwaysSteer()).toBe(DEFAULT_ALWAYS_STEER);
  });
});
