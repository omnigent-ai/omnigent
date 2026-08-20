import { afterEach, describe, expect, it, vi } from "vitest";
import {
  DEFAULT_HIDE_UNCONFIGURED_HARNESSES,
  readHideUnconfiguredHarnesses,
  writeHideUnconfiguredHarnesses,
} from "./harnessVisibilityPreferences";

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

describe("harnessVisibilityPreferences", () => {
  it("defaults to off (show every harness) when nothing is stored", () => {
    // Opt-in: with no stored preference the picker keeps surfacing harnesses to
    // set up, so read must report the feature off.
    expect(DEFAULT_HIDE_UNCONFIGURED_HARNESSES).toBe(false);
    expect(readHideUnconfiguredHarnesses()).toBe(false);
  });

  it("round-trips both boolean values", () => {
    writeHideUnconfiguredHarnesses(true);
    expect(readHideUnconfiguredHarnesses()).toBe(true);

    writeHideUnconfiguredHarnesses(false);
    expect(readHideUnconfiguredHarnesses()).toBe(false);
  });

  it('treats any non-"true" stored value as off (defensive against hand edits)', () => {
    // Only the exact string "true" enables the filter; garbage or a stale
    // format reads as off rather than silently hiding harnesses.
    localStorage.setItem("omnigent:hide-unconfigured-harnesses", "1");
    expect(readHideUnconfiguredHarnesses()).toBe(false);

    localStorage.setItem("omnigent:hide-unconfigured-harnesses", "yes");
    expect(readHideUnconfiguredHarnesses()).toBe(false);

    localStorage.setItem("omnigent:hide-unconfigured-harnesses", "true");
    expect(readHideUnconfiguredHarnesses()).toBe(true);
  });

  it("never throws when storage is inaccessible", () => {
    // Private-mode / quota failures surface as throws from the Storage API.
    // Both helpers must swallow them — a broken preference must not break the
    // picker or settings.
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota exceeded");
    });
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("access denied");
    });
    expect(() => writeHideUnconfiguredHarnesses(true)).not.toThrow();
    expect(readHideUnconfiguredHarnesses()).toBe(false);
  });
});
