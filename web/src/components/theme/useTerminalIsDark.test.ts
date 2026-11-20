import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

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
