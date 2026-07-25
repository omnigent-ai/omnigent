import { afterEach, describe, expect, it, vi } from "vitest";
import {
  normalizeTerminalThemeMode,
  readTerminalThemeMode,
  resolveTerminalIsDark,
  subscribeTerminalTheme,
  TERMINAL_THEME_DEFAULT,
  writeTerminalThemeMode,
} from "./terminalThemePreferences";

const STORAGE_KEY = "omnigent:terminal-theme";

afterEach(() => {
  localStorage.clear();
});

describe("terminalThemePreferences — read/write", () => {
  it("returns auto when nothing is stored", () => {
    expect(readTerminalThemeMode()).toBe(TERMINAL_THEME_DEFAULT);
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it("stores raw light and dark strings", () => {
    writeTerminalThemeMode("light");
    expect(readTerminalThemeMode()).toBe("light");
    expect(localStorage.getItem(STORAGE_KEY)).toBe("light");

    writeTerminalThemeMode("dark");
    expect(readTerminalThemeMode()).toBe("dark");
    expect(localStorage.getItem(STORAGE_KEY)).toBe("dark");
  });

  it("removes the key when written auto", () => {
    writeTerminalThemeMode("dark");
    expect(localStorage.getItem(STORAGE_KEY)).not.toBeNull();
    writeTerminalThemeMode("auto");
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
    expect(readTerminalThemeMode()).toBe("auto");
  });
});

describe("normalizeTerminalThemeMode", () => {
  it("passes through valid modes", () => {
    expect(normalizeTerminalThemeMode("auto")).toBe("auto");
    expect(normalizeTerminalThemeMode("light")).toBe("light");
    expect(normalizeTerminalThemeMode("dark")).toBe("dark");
  });

  it("maps unknown, null, and garbage to auto", () => {
    expect(normalizeTerminalThemeMode("system")).toBe("auto");
    expect(normalizeTerminalThemeMode("bogus")).toBe("auto");
    expect(normalizeTerminalThemeMode(null)).toBe("auto");
    expect(normalizeTerminalThemeMode(undefined)).toBe("auto");
  });
});

describe("resolveTerminalIsDark", () => {
  it("follows the app when mode is auto", () => {
    expect(resolveTerminalIsDark("auto", true)).toBe(true);
    expect(resolveTerminalIsDark("auto", false)).toBe(false);
  });

  it("pins light regardless of app theme", () => {
    expect(resolveTerminalIsDark("light", true)).toBe(false);
    expect(resolveTerminalIsDark("light", false)).toBe(false);
  });

  it("pins dark regardless of app theme", () => {
    expect(resolveTerminalIsDark("dark", true)).toBe(true);
    expect(resolveTerminalIsDark("dark", false)).toBe(true);
  });
});

describe("terminalThemePreferences — pub/sub", () => {
  it("notifies subscribers with the written mode", () => {
    const cb = vi.fn();
    const unsubscribe = subscribeTerminalTheme(cb);

    writeTerminalThemeMode("dark");
    expect(cb).toHaveBeenCalledWith("dark");

    unsubscribe();
  });

  it("stops notifying after unsubscribe", () => {
    const cb = vi.fn();
    const unsubscribe = subscribeTerminalTheme(cb);
    unsubscribe();

    writeTerminalThemeMode("light");
    expect(cb).not.toHaveBeenCalled();
  });
});
