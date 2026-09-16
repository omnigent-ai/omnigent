import type { ReactNode } from "react";
import { cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { NativeThemeSync, ThemeProvider } from "./ThemeProvider";

const themeState = vi.hoisted(() => ({
  setThemeSource: vi.fn(),
  theme: "system" as string | undefined,
  forcedTheme: undefined as string | undefined,
}));

vi.mock("@/lib/nativeBridge", () => ({
  setThemeSource: themeState.setThemeSource,
}));

vi.mock("next-themes", () => ({
  ThemeProvider: ({ children }: { children: ReactNode }) => children,
  useTheme: () => ({ theme: themeState.theme, forcedTheme: themeState.forcedTheme }),
}));

beforeEach(() => {
  themeState.setThemeSource.mockClear();
  themeState.theme = "system";
  themeState.forcedTheme = undefined;
});

afterEach(cleanup);

describe("ThemeProvider native theme sync", () => {
  it("tells the native shell to follow the system theme by default", () => {
    render(<ThemeProvider>content</ThemeProvider>);
    expect(themeState.setThemeSource).toHaveBeenCalledWith("system");
  });

  it("updates the native shell when the user selects an explicit theme", () => {
    const { rerender } = render(<ThemeProvider>content</ThemeProvider>);
    themeState.setThemeSource.mockClear();

    themeState.theme = "light";
    rerender(<ThemeProvider>content</ThemeProvider>);

    expect(themeState.setThemeSource).toHaveBeenCalledWith("light");
  });

  it("uses the managed host's theme instead of the saved standalone preference", () => {
    themeState.theme = "light";
    themeState.forcedTheme = "dark";
    const { rerender } = render(<NativeThemeSync />);

    expect(themeState.setThemeSource).toHaveBeenLastCalledWith("dark");

    themeState.theme = "dark";
    themeState.forcedTheme = "light";
    rerender(<NativeThemeSync />);

    expect(themeState.setThemeSource).toHaveBeenLastCalledWith("light");
  });

  it("syncs the managed theme before the saved preference loads", () => {
    themeState.theme = undefined;
    themeState.forcedTheme = "dark";
    render(<NativeThemeSync />);

    expect(themeState.setThemeSource).toHaveBeenCalledWith("dark");
  });
});
