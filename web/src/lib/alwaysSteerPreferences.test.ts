import { afterEach, describe, expect, it, vi } from "vitest";
import { DEFAULT_ALWAYS_STEER, readAlwaysSteer, writeAlwaysSteer } from "./alwaysSteerPreferences";

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

describe("alwaysSteerPreferences", () => {
  it("defaults to off (queue mid-turn follow-ups) when nothing is stored", () => {
    // Opt-in: with no stored preference a busy-session follow-up still parks in
    // the queue strip, so read must report the feature off.
    expect(DEFAULT_ALWAYS_STEER).toBe(false);
    expect(readAlwaysSteer()).toBe(false);
  });

  it("round-trips both boolean values", () => {
    writeAlwaysSteer(true);
    expect(readAlwaysSteer()).toBe(true);

    writeAlwaysSteer(false);
    expect(readAlwaysSteer()).toBe(false);
  });

  it('treats any non-"true" stored value as off (defensive against hand edits)', () => {
    // Only the exact string "true" enables always-steer; garbage or a stale
    // format reads as off rather than silently changing dispatch behavior.
    localStorage.setItem("omnigent:always-steer", "1");
    expect(readAlwaysSteer()).toBe(false);

    localStorage.setItem("omnigent:always-steer", "yes");
    expect(readAlwaysSteer()).toBe(false);

    localStorage.setItem("omnigent:always-steer", "true");
    expect(readAlwaysSteer()).toBe(true);
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
    expect(() => writeAlwaysSteer(true)).not.toThrow();
    expect(readAlwaysSteer()).toBe(false);
  });
});
