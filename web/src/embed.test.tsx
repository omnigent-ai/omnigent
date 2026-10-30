import { readFileSync } from "node:fs";
import { act, cleanup, fireEvent, render } from "@testing-library/react";
import { createPortal } from "react-dom";
import { useTheme } from "next-themes";
import { useResolvedThemeMode } from "./components/theme/useResolvedThemeMode";
import { MemoryRouter } from "react-router-dom";
import { transform } from "lightningcss";
import { parse, type Rule } from "postcss";
import { afterEach, describe, expect, it, vi } from "vitest";
import { scopeCss } from "../vite.embed.config";
import { OmnigentApp } from "./embed";
import { getEmbedRoot } from "./lib/host";

// Exercise the embed's real providers and native detection without loading
// routed pages, editors, or terminals unrelated to the window chrome.
vi.mock("./App", () => ({
  default: function ThemeTestApp() {
    const { theme, setTheme } = useTheme();
    const resolved = useResolvedThemeMode();
    return (
      <div className="app-shell">
        <div className="electron-drag-strip" />
        <div className="sidebar-header-row" />
        <div className="traffic-light-clearance" />
        <div className="canvas-workspace" />
        <aside aria-label="Workspace" data-maximized="true">
          <div className="workspace-tab-strip" />
        </aside>
        <button type="button">Header action</button>
        <output aria-label="App theme">
          {theme}:{resolved}
        </output>
        {["light", "dark", "system"].map((mode) => (
          <button type="button" key={mode} onClick={() => setTheme(mode)}>
            Select {mode}
          </button>
        ))}
      </div>
    );
  },
}));

// Keep the backend discovery pending: the root must be marked before it resolves.
const fetcher = () => new Promise<Response>(() => {});

const compiledCss = transform({
  filename: "index.css",
  code: readFileSync("src/index.css"),
  minify: true,
}).code.toString();
const css = parse(scopeCss(compiledCss));
function selectorFor(fragment: string): string {
  let selector = "";
  css.walkRules((rule) => {
    if (
      !selector &&
      rule.selector.includes("[data-electron-mac]") &&
      rule.selector.includes(fragment)
    ) {
      selector = rule.selector;
    }
  });
  expect(selector).not.toBe("");
  return selector;
}

afterEach(() => {
  cleanup();
  localStorage.clear();
  document.documentElement.className = "";
  document.documentElement.style.colorScheme = "";
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("macOS Electron embed", () => {
  it.each([
    ["macOS Electron", "Macintosh", true, true],
    ["macOS browser", "Macintosh", false, false],
    ["Windows Electron", "Windows NT 10.0", true, false],
  ])("scopes window chrome correctly in %s", (_name, platform, electron, expected) => {
    vi.spyOn(window.navigator, "userAgent", "get").mockReturnValue(`Mozilla/5.0 (${platform})`);
    vi.stubGlobal("omnigentDesktop", electron ? { kind: "electron" } : undefined);
    const { container, getByRole } = render(
      <MemoryRouter>
        <OmnigentApp fetcher={fetcher} />
      </MemoryRouter>,
    );
    const scope = container.querySelector(".omnigent-app")!;
    expect(scope.getAttribute("data-electron-mac")).toBe(expected ? "true" : null);
    expect(document.documentElement).not.toHaveAttribute("data-electron-mac");

    const keyboardScope = getEmbedRoot()!;
    expect(keyboardScope.getAttribute("tabindex")).toBe(electron ? null : "-1");
    if (!electron) {
      keyboardScope.focus();
      expect(document.activeElement).toBe(keyboardScope);
    }

    for (const className of [
      "electron-drag-strip",
      "sidebar-header-row",
      "traffic-light-clearance",
      "canvas-workspace",
      "workspace-tab-strip",
    ]) {
      const selector = selectorFor(`.${className}`);
      expect(selector).not.toContain(":has(");
      expect(scope.querySelector(`.${className}`)!.matches(selector), className).toBe(expected);
    }

    const controlsSelector = selectorFor(":is(");
    expect(controlsSelector).not.toContain(":has(");
    expect(getByRole("button", { name: "Header action" }).matches(controlsSelector)).toBe(expected);
    render(createPortal(<div role="menu">Menu</div>, getEmbedRoot()!));
    expect(getByRole("menu").matches(controlsSelector)).toBe(expected);

    scope.querySelector(".app-shell")!.setAttribute("data-sidebar-open", "true");
    expect(
      scope.querySelector(".workspace-tab-strip")!.matches(selectorFor(".workspace-tab-strip")),
    ).toBe(false);
    const outside = document.createElement("button");
    document.body.appendChild(outside);
    expect(outside.matches(controlsSelector)).toBe(false);
    outside.remove();
  });
});

describe("embed color scheme", () => {
  // The scope root resolves `color` from the light tokens, so the portal root
  // must re-apply the foreground color for overlays to read dark-mode text.
  it.each([
    ["dark", true],
    ["light", false],
  ])("re-applies the foreground color on the %s portal root", (_name, isDarkMode) => {
    render(
      <MemoryRouter>
        <OmnigentApp fetcher={fetcher} isDarkMode={isDarkMode} />
      </MemoryRouter>,
    );

    const portalRoot = getEmbedRoot()!;
    expect(portalRoot).toHaveClass("text-foreground");
    expect(portalRoot.classList.contains("dark")).toBe(isDarkMode);
  });
});

describe("embedded theme selection", () => {
  function app(isDarkMode: boolean) {
    return (
      <MemoryRouter>
        <OmnigentApp fetcher={fetcher} isDarkMode={isDarkMode} />
      </MemoryRouter>
    );
  }

  it("follows host changes until overridden and restores the stored choice", () => {
    const { getByRole, rerender, unmount } = render(app(true));
    expect(getByRole("status", { name: "App theme" })).toHaveTextContent("system:dark");
    rerender(app(false));
    expect(getEmbedRoot()).not.toHaveClass("dark");
    fireEvent.click(getByRole("button", { name: "Select dark" }));
    expect(getByRole("status", { name: "App theme" })).toHaveTextContent("dark:dark");
    expect(getEmbedRoot()).toHaveClass("dark");
    expect(localStorage.getItem("web-theme")).toBe("dark");
    unmount();

    const restored = render(app(false));
    expect(getEmbedRoot()).toHaveClass("dark");
    fireEvent.click(restored.getByRole("button", { name: "Select light" }));
    restored.rerender(app(true));
    expect(getEmbedRoot()).not.toHaveClass("dark");
    fireEvent.click(restored.getByRole("button", { name: "Select system" }));
    expect(getEmbedRoot()).toHaveClass("dark");
    expect(restored.getByRole("status", { name: "App theme" })).toHaveTextContent("system:dark");
  });

  it("keeps overrides inside the embed while the host document stays dark", () => {
    document.documentElement.className = "dark host-page";
    document.documentElement.style.colorScheme = "dark";
    const { getByRole } = render(app(true));
    fireEvent.click(getByRole("button", { name: "Select light" }));
    expect(getEmbedRoot()).not.toHaveClass("dark");
    expect(getEmbedRoot()).toHaveStyle({ colorScheme: "light" });
    expect(document.documentElement.className).toBe("dark host-page");
    expect(document.documentElement.style.colorScheme).toBe("dark");

    const selector = parse(scopeCss(".control:is(.dark *) { color: white; }")).nodes[0] as Rule;
    const control = getByRole("button", { name: "Select dark" });
    control.classList.add("control");
    expect(control.matches(selector.selector)).toBe(false);
    fireEvent.click(control);
    expect(control.matches(selector.selector)).toBe(true);
  });

  it("follows OS changes in Electron and syncs explicit choices to native chrome", () => {
    const setColorScheme = vi.fn();
    vi.stubGlobal("omnigentDesktop", { kind: "electron", setColorScheme });
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const listeners = new Set<NonNullable<Parameters<MediaQueryList["addListener"]>[0]>>();
    let systemDark = true;
    vi.spyOn(window, "matchMedia").mockReturnValue({
      ...media,
      get matches() {
        return systemDark;
      },
      addListener: (listener) => {
        if (listener) listeners.add(listener);
      },
      removeListener: (listener) => {
        if (listener) listeners.delete(listener);
      },
    });
    const { getByRole, rerender } = render(app(false));
    expect(getByRole("status", { name: "App theme" })).toHaveTextContent("system:dark");
    expect(setColorScheme).toHaveBeenLastCalledWith("system");
    rerender(app(true));
    act(() => {
      systemDark = false;
      listeners.forEach((listener) =>
        listener.call(media, { matches: false } as MediaQueryListEvent),
      );
    });
    expect(getEmbedRoot()).not.toHaveClass("dark");
    fireEvent.click(getByRole("button", { name: "Select dark" }));
    expect(getEmbedRoot()).toHaveClass("dark");
    expect(setColorScheme).toHaveBeenLastCalledWith("dark");
    fireEvent.click(getByRole("button", { name: "Select system" }));
    expect(getEmbedRoot()).not.toHaveClass("dark");
    expect(setColorScheme).toHaveBeenLastCalledWith("system");
  });
});
