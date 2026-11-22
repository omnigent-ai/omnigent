import type { ReactNode } from "react";
import { act, cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { EmbeddedProvider } from "@/lib/embedded";
import { setThemeModePreview, useThemeModePreviewStore } from "@/store/themeModePreview";

import { ThemeProvider } from "./ThemeProvider";

const themeState = vi.hoisted(() => ({
  setThemeSource: vi.fn(),
  theme: "system" as string | undefined,
  forcedTheme: undefined as string | undefined,
}));

vi.mock("@/lib/nativeBridge", () => ({
  setThemeSource: themeState.setThemeSource,
  isElectronShell: () => false,
}));

vi.mock("next-themes", () => ({
  ThemeProvider: ({ children, forcedTheme }: { children: ReactNode; forcedTheme?: string }) => {
    themeState.forcedTheme = forcedTheme;
    return children;
  },
  useTheme: () => ({ theme: themeState.theme }),
}));

/**
 * A desktop-shell stand-in: the native theme source drives
 * `prefers-color-scheme`, and the change reaches the page a moment later.
 */
function installShellColorScheme(initialOsDark: boolean) {
  let osDark = initialOsDark;
  let source = "system";
  const listeners = new Set<() => void>();
  const matches = () => (source === "system" ? osDark : source === "dark");
  vi.stubGlobal("matchMedia", (query: string) => ({
    get matches() {
      return matches();
    },
    media: query,
    addEventListener: (_type: string, listener: () => void) => listeners.add(listener),
    removeEventListener: (_type: string, listener: () => void) => listeners.delete(listener),
  }));
  let pending: string | null = null;
  themeState.setThemeSource.mockImplementation((next: string) => {
    pending = next;
  });
  const notifyIfChanged = (change: () => void) => {
    const before = matches();
    change();
    if (matches() !== before) for (const listener of listeners) listener();
  };
  return {
    /** Changes the OS appearance, as the user switching it in System Settings would. */
    setOsDark(next: boolean) {
      notifyIfChanged(() => {
        osDark = next;
      });
    },
    /** Delivers the last source the page sent, as the shell's IPC round trip would. */
    settle() {
      if (pending === null) return;
      const next = pending;
      pending = null;
      notifyIfChanged(() => {
        source = next;
      });
    },
  };
}

beforeEach(() => {
  themeState.setThemeSource.mockReset();
  themeState.theme = "system";
  themeState.forcedTheme = undefined;
});

afterEach(() => {
  cleanup();
  useThemeModePreviewStore.setState({ mode: null });
  vi.unstubAllGlobals();
});

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

  it("follows a preview and returns to the saved mode when it ends", () => {
    themeState.theme = "light";
    render(<ThemeProvider>content</ThemeProvider>);
    themeState.setThemeSource.mockClear();

    act(() => setThemeModePreview("dark"));
    expect(themeState.setThemeSource).toHaveBeenLastCalledWith("dark");
    act(() => setThemeModePreview(null));
    expect(themeState.setThemeSource).toHaveBeenLastCalledWith("light");
  });

  it("hands the shell the saved mode again when it unmounts mid-preview", () => {
    themeState.theme = "dark";
    const { unmount } = render(<ThemeProvider>content</ThemeProvider>);
    act(() => setThemeModePreview("light"));
    themeState.setThemeSource.mockClear();

    unmount();
    expect(themeState.setThemeSource.mock.calls).toEqual([["dark"]]);
  });

  it("leaves the shell alone on unmount without a preview", () => {
    const { unmount } = render(<ThemeProvider>content</ThemeProvider>);
    themeState.setThemeSource.mockClear();
    unmount();
    expect(themeState.setThemeSource).not.toHaveBeenCalled();
  });
});

describe("ThemeProvider appearance preview", () => {
  it("forces nothing without a preview", () => {
    render(<ThemeProvider>content</ThemeProvider>);
    expect(themeState.forcedTheme).toBeUndefined();
  });

  it("forces an explicit previewed mode", () => {
    render(<ThemeProvider>content</ThemeProvider>);
    act(() => setThemeModePreview("dark"));
    expect(themeState.forcedTheme).toBe("dark");
    act(() => setThemeModePreview(null));
    expect(themeState.forcedTheme).toBeUndefined();
  });

  it("settles a System preview on the OS appearance once the shell drops a pinned source", () => {
    // Saved Dark pins the shell to dark while the OS is light.
    const shell = installShellColorScheme(false);
    themeState.theme = "dark";
    render(<ThemeProvider>content</ThemeProvider>);
    act(() => shell.settle());

    act(() => setThemeModePreview("system"));
    expect(themeState.setThemeSource).toHaveBeenLastCalledWith("system");
    // Until the shell applies "system", the page still reports the pinned dark.
    expect(themeState.forcedTheme).toBe("dark");
    act(() => shell.settle());
    expect(themeState.forcedTheme).toBe("light");

    // While System stays highlighted, it follows the OS as it changes.
    act(() => shell.setOsDark(true));
    expect(themeState.forcedTheme).toBe("dark");
    act(() => shell.setOsDark(false));
    expect(themeState.forcedTheme).toBe("light");

    // Leaving the preview pins the shell to the saved mode again.
    act(() => setThemeModePreview(null));
    expect(themeState.setThemeSource).toHaveBeenLastCalledWith("dark");
    expect(themeState.forcedTheme).toBeUndefined();
  });

  it("keeps a System preview following the OS after a pinned preview", () => {
    const shell = installShellColorScheme(true);
    themeState.theme = "system";
    render(<ThemeProvider>content</ThemeProvider>);
    act(() => setThemeModePreview("light"));
    act(() => shell.settle());
    expect(themeState.forcedTheme).toBe("light");

    act(() => setThemeModePreview("system"));
    act(() => shell.settle());
    expect(themeState.forcedTheme).toBe("dark");
  });

  it("resolves a System preview from the host appearance in a browser embed", () => {
    installShellColorScheme(false);
    themeState.theme = "light";
    const { rerender } = render(
      <EmbeddedProvider isDarkMode>
        <ThemeProvider embedded>content</ThemeProvider>
      </EmbeddedProvider>,
    );
    act(() => setThemeModePreview("system"));
    expect(themeState.forcedTheme).toBe("dark");
    // The workspace switching to light while System stays highlighted follows it.
    rerender(
      <EmbeddedProvider isDarkMode={false}>
        <ThemeProvider embedded>content</ThemeProvider>
      </EmbeddedProvider>,
    );
    expect(themeState.forcedTheme).toBe("light");
  });
});
