import type { ReactNode } from "react";
import { cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ThemeProvider } from "./ThemeProvider";

const themeState = vi.hoisted(() => ({
  setThemeSource: vi.fn(),
  theme: "system" as string | undefined,
}));

vi.mock("@/lib/nativeBridge", () => ({
  setThemeSource: themeState.setThemeSource,
}));

vi.mock("next-themes", () => ({
  ThemeProvider: ({ children }: { children: ReactNode }) => children,
  useTheme: () => ({ theme: themeState.theme }),
}));

beforeEach(() => {
  themeState.setThemeSource.mockClear();
  themeState.theme = "system";
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
});
