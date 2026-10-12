import { readFileSync } from "node:fs";
import { cleanup, render } from "@testing-library/react";
import { createPortal } from "react-dom";
import { MemoryRouter } from "react-router-dom";
import { transform } from "lightningcss";
import { parse } from "postcss";
import { afterEach, describe, expect, it, vi } from "vitest";
import { scopeCss } from "../vite.embed.config";
import { OmnigentApp } from "./embed";
import { getEmbedRoot } from "./lib/host";

// Exercise the embed's real providers and native detection without loading
// routed pages, editors, or terminals unrelated to the window chrome.
vi.mock("./App", () => ({
  default: () => (
    <div className="app-shell">
      <div className="electron-drag-strip" />
      <div className="sidebar-header-row" />
      <div className="traffic-light-clearance" />
      <aside aria-label="Workspace" data-maximized="true">
        <div className="workspace-tab-strip" />
      </aside>
      <button type="button">Header action</button>
    </div>
  ),
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

    for (const className of [
      "electron-drag-strip",
      "sidebar-header-row",
      "traffic-light-clearance",
      "workspace-tab-strip",
    ]) {
      const selector = selectorFor(`.${className}`);
      expect(selector).not.toContain(":has(");
      expect(scope.querySelector(`.${className}`)!.matches(selector), className).toBe(expected);
    }

    const controlsSelector = selectorFor(":is(");
    expect(controlsSelector).not.toContain(":has(");
    expect(getByRole("button").matches(controlsSelector)).toBe(expected);
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
