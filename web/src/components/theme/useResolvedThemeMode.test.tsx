import { renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useResolvedThemeMode } from "./useResolvedThemeMode";

// Controllable next-themes stub: each test sets what useTheme() returns.
let themeReturn: { resolvedTheme?: string; forcedTheme?: string } = {};
vi.mock("next-themes", () => ({ useTheme: () => themeReturn }));

afterEach(() => {
  themeReturn = {};
});

describe("useResolvedThemeMode", () => {
  it("uses forcedTheme when resolvedTheme is unset (the embed case)", () => {
    // The embed sets forcedTheme but next-themes leaves resolvedTheme unset —
    // the exact managed regression: without the hook this normalized to light.
    themeReturn = { forcedTheme: "dark", resolvedTheme: undefined };
    const { result } = renderHook(() => useResolvedThemeMode());
    expect(result.current).toBe("dark");
  });

  it("prefers forcedTheme over a conflicting resolvedTheme", () => {
    themeReturn = { forcedTheme: "dark", resolvedTheme: "light" };
    const { result } = renderHook(() => useResolvedThemeMode());
    expect(result.current).toBe("dark");
  });

  it("falls back to resolvedTheme when no theme is forced (standalone)", () => {
    themeReturn = { forcedTheme: undefined, resolvedTheme: "dark" };
    const { result } = renderHook(() => useResolvedThemeMode());
    expect(result.current).toBe("dark");
  });

  it("normalizes to light when neither is set", () => {
    themeReturn = {};
    const { result } = renderHook(() => useResolvedThemeMode());
    expect(result.current).toBe("light");
  });
});
