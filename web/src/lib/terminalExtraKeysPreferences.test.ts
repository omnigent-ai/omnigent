import { afterEach, describe, expect, it, vi } from "vitest";
import {
  TERMINAL_EXTRA_KEYS_DEFAULT,
  isTerminalExtraKeysMode,
  normalizeTerminalExtraKeysMode,
  readTerminalExtraKeysMode,
  subscribeTerminalExtraKeys,
  writeTerminalExtraKeysMode,
} from "./terminalExtraKeysPreferences";

const KEY = "omnigent:terminal-extra-keys";

afterEach(() => {
  vi.restoreAllMocks();
  // Also clears any write kept in memory after a refused storage write.
  writeTerminalExtraKeysMode("auto");
  localStorage.clear();
});

describe("terminalExtraKeysPreferences", () => {
  it("defaults to auto and validates the three modes", () => {
    expect(TERMINAL_EXTRA_KEYS_DEFAULT).toBe("auto");
    expect(readTerminalExtraKeysMode()).toBe("auto");
    expect(isTerminalExtraKeysMode("on")).toBe(true);
    expect(isTerminalExtraKeysMode("off")).toBe(true);
    expect(isTerminalExtraKeysMode("always")).toBe(false);
    expect(normalizeTerminalExtraKeysMode("garbage")).toBe("auto");
    expect(normalizeTerminalExtraKeysMode(null)).toBe("auto");
  });

  it("persists on/off, clears the key for auto, and reads back what it wrote", () => {
    writeTerminalExtraKeysMode("on");
    expect(localStorage.getItem(KEY)).toBe("on");
    expect(readTerminalExtraKeysMode()).toBe("on");

    writeTerminalExtraKeysMode("off");
    expect(localStorage.getItem(KEY)).toBe("off");
    expect(readTerminalExtraKeysMode()).toBe("off");

    writeTerminalExtraKeysMode("auto");
    expect(localStorage.getItem(KEY)).toBeNull();
    expect(readTerminalExtraKeysMode()).toBe("auto");
  });

  it("falls back to auto for a corrupt stored value", () => {
    localStorage.setItem(KEY, "sometimes");
    expect(readTerminalExtraKeysMode()).toBe("auto");
  });

  it("notifies subscribers with the written mode and stops after unsubscribe", () => {
    // WHY: a mounted terminal shows/hides the row live through this pub/sub.
    const listener = vi.fn();
    const unsubscribe = subscribeTerminalExtraKeys(listener);

    writeTerminalExtraKeysMode("on");
    expect(listener).toHaveBeenCalledWith("on");

    unsubscribe();
    writeTerminalExtraKeysMode("off");
    expect(listener).toHaveBeenCalledTimes(1);
  });

  it("still notifies and reads back the value when the storage write throws", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota");
    });
    const listener = vi.fn();
    subscribeTerminalExtraKeys(listener);
    expect(() => writeTerminalExtraKeysMode("on")).not.toThrow();
    expect(listener).toHaveBeenCalledWith("on");
    // The refused write is still what readers see, until a write succeeds.
    expect(readTerminalExtraKeysMode()).toBe("on");
    vi.restoreAllMocks();
    writeTerminalExtraKeysMode("off");
    expect(readTerminalExtraKeysMode()).toBe("off");
    expect(localStorage.getItem(KEY)).toBe("off");
  });
});
