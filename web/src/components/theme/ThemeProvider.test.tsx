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
  it("reports the resolved Android scheme and selected Electron system theme", () => {
    render(<ThemeProvider>content</ThemeProvider>);
    expect(themeState.reportColorScheme).toHaveBeenCalledWith("light", "system");
  });

  it("updates Android while Electron remains on system when the OS scheme changes", () => {
    const { rerender } = render(<ThemeProvider>content</ThemeProvider>);
    themeState.reportColorScheme.mockClear();

    themeState.resolvedTheme = "dark";
    rerender(<ThemeProvider>content</ThemeProvider>);

    expect(themeState.reportColorScheme).toHaveBeenCalledWith("dark", "system");
  });

  it("reports an explicit Electron theme when system is deselected without a resolved change", () => {
    const { rerender } = render(<ThemeProvider>content</ThemeProvider>);
    themeState.reportColorScheme.mockClear();

    themeState.theme = "light";
    rerender(<ThemeProvider>content</ThemeProvider>);

    expect(themeState.reportColorScheme).toHaveBeenCalledWith("light", "light");
  });

  it("keeps the resolved Android scheme separate from the explicit Electron theme", () => {
    themeState.theme = "dark";
    themeState.resolvedTheme = "light";
    render(<ThemeProvider>content</ThemeProvider>);

    expect(themeState.reportColorScheme).toHaveBeenCalledWith("light", "dark");
  });
});
