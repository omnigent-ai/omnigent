import type { ReactNode } from "react";
import { cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ThemeProvider } from "./ThemeProvider";

const themeState = vi.hoisted(() => ({
  reportColorScheme: vi.fn(),
  resolvedTheme: "light" as string | undefined,
}));

vi.mock("@/lib/nativeBridge", () => ({
  reportColorScheme: themeState.reportColorScheme,
}));

vi.mock("next-themes", () => ({
  ThemeProvider: ({ children }: { children: ReactNode }) => children,
  useTheme: () => ({ resolvedTheme: themeState.resolvedTheme }),
}));

beforeEach(() => {
  themeState.reportColorScheme.mockClear();
  themeState.resolvedTheme = "light";
});

afterEach(cleanup);

describe("ThemeProvider native theme sync", () => {
  it("reports the resolved scheme on mount and whenever it changes", () => {
    const { rerender } = render(<ThemeProvider>content</ThemeProvider>);
    expect(themeState.reportColorScheme).toHaveBeenLastCalledWith("light");

    themeState.resolvedTheme = "dark";
    rerender(<ThemeProvider>content</ThemeProvider>);

    expect(themeState.reportColorScheme).toHaveBeenNthCalledWith(2, "dark");
  });
});
