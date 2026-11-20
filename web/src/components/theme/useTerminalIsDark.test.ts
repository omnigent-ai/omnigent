import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { applyImportedSettings } from "@/lib/settingsPortability";
import { writeTerminalThemeMode } from "@/lib/terminalThemePreferences";

import { useTerminalThemeMode } from "./useTerminalIsDark";

afterEach(() => {
  vi.restoreAllMocks();
  localStorage.clear();
});

describe("useTerminalThemeMode", () => {
  it("follows a write from anywhere, such as ⌘K", () => {
    const { result } = renderHook(() => useTerminalThemeMode());
    expect(result.current).toBe("auto");
    act(() => writeTerminalThemeMode("dark"));
    expect(result.current).toBe("dark");
  });

  it("follows a settings import, which writes storage directly", () => {
    const { result } = renderHook(() => useTerminalThemeMode());
    act(() => writeTerminalThemeMode("dark"));
    act(() => {
      applyImportedSettings({ version: 1, settings: { "omnigent:terminal-theme": "light" } });
    });
    expect(result.current).toBe("light");
    // A reader mounted after the import, such as a reopened Settings page, agrees.
    expect(renderHook(() => useTerminalThemeMode()).result.current).toBe("light");
    // An import without the key clears it back to the default.
    act(() => {
      applyImportedSettings({ version: 1, settings: {} });
    });
    expect(result.current).toBe("auto");
  });

  it("follows the mode written even when storage refused it", () => {
    const { result, unmount } = renderHook(() => useTerminalThemeMode());
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota exceeded");
    });
    act(() => writeTerminalThemeMode("light"));
    expect(result.current).toBe("light");
    // Once nothing follows it, a new reader starts from storage again.
    unmount();
    expect(renderHook(() => useTerminalThemeMode()).result.current).toBe("auto");
  });
});
