import type { ReactNode } from "react";
import { cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ThemeProvider } from "./ThemeProvider";

const themeState = vi.hoisted(() => ({
  reportColorScheme: vi.fn(),
  theme: "system" as string | undefined,
  resolvedTheme: "light" as string | undefined,
}));

vi.mock("@/lib/nativeBridge", () => ({
  reportColorScheme: themeState.reportColorScheme,
}));

vi.mock("next-themes", () => ({
  ThemeProvider: ({ children }: { children: ReactNode }) => children,
  useTheme: () => ({ theme: themeState.theme, resolvedTheme: themeState.resolvedTheme }),
}));

beforeEach(() => {
  themeState.reportColorScheme.mockClear();
  themeState.theme = "system";
  themeState.resolvedTheme = "light";
});

afterEach(cleanup);

describe("ThemeProvider native theme sync", () => {
  it("reports the resolved scheme with a trailing system report while in system mode", () => {
    // Android reads the concrete scheme; Electron keeps the last report, so
    // "system" must come after it for native OS tracking.
    render(<ThemeProvider>content</ThemeProvider>);
    expect(themeState.reportColorScheme.mock.calls).toEqual([["light"], ["system"]]);
  });

  it("re-reports on a resolved change in system mode, still ending on system", () => {
    const { rerender } = render(<ThemeProvider>content</ThemeProvider>);
    themeState.reportColorScheme.mockClear();

    themeState.resolvedTheme = "dark";
    rerender(<ThemeProvider>content</ThemeProvider>);

    expect(themeState.reportColorScheme.mock.calls).toEqual([["dark"], ["system"]]);
  });

  it("reports the forced scheme when system is deselected without a resolved change", () => {
    // System with a light OS → explicit Light: resolvedTheme stays "light",
    // but Electron must still be told so themeSource stops tracking the OS.
    const { rerender } = render(<ThemeProvider>content</ThemeProvider>);
    themeState.reportColorScheme.mockClear();

    themeState.theme = "light";
    rerender(<ThemeProvider>content</ThemeProvider>);

    expect(themeState.reportColorScheme.mock.calls).toEqual([["light"]]);
  });

  it("reports only the concrete scheme for an explicit selection", () => {
    themeState.theme = "dark";
    themeState.resolvedTheme = "dark";
    render(<ThemeProvider>content</ThemeProvider>);

    expect(themeState.reportColorScheme.mock.calls).toEqual([["dark"]]);
  });
});
