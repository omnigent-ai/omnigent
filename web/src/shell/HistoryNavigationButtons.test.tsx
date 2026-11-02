import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";
import { type RoutingApi, RoutingProvider, basenamedRouting } from "@/lib/routing";

import { HistoryNavigationButtons } from "./HistoryNavigationButtons";

interface ShellWindow {
  omnigentDesktop?: unknown;
  omnigentNative?: unknown;
  navigation?: unknown;
  __OMNIGENT_BASE_PATH__?: string;
}

/** Stand-in for the Navigation API, which jsdom does not implement. */
class FakeNavigation extends EventTarget {
  readonly paths: string[];
  index: number;

  constructor(paths: string[], index: number) {
    super();
    this.paths = paths;
    this.index = index;
  }

  entries() {
    return this.paths.map((path, index) => ({
      index,
      url: new URL(path, window.location.origin).href,
    }));
  }

  get currentEntry() {
    return this.entries()[this.index];
  }

  moveTo(index: number) {
    this.index = index;
    this.dispatchEvent(new Event("currententrychange"));
  }
}

function installNavigation(paths: string[], index = paths.length - 1) {
  const navigation = new FakeNavigation(paths, index);
  Object.defineProperty(window, "navigation", { configurable: true, value: navigation });
  return navigation;
}

/** A phone shell bridge with the in-sidebar server picker. */
function phoneShell(kind: "ios" | "android") {
  return {
    kind,
    getServerPicker: () => Promise.resolve(null),
    switchServer: () => Promise.resolve(),
    openServerSetup: () => {},
  };
}

function CurrentPath() {
  return <output aria-label="Current path">{useLocation().pathname}</output>;
}

function renderButtons(entries = ["/"], routing?: RoutingApi) {
  const buttons = (
    <TooltipProvider>
      <HistoryNavigationButtons />
      <CurrentPath />
    </TooltipProvider>
  );
  return render(
    <MemoryRouter initialEntries={entries} initialIndex={entries.length - 1}>
      {routing ? <RoutingProvider value={routing}>{buttons}</RoutingProvider> : buttons}
    </MemoryRouter>,
  );
}

afterEach(() => {
  cleanup();
  const shellWindow = window as ShellWindow;
  delete shellWindow.omnigentDesktop;
  delete shellWindow.omnigentNative;
  delete shellWindow.navigation;
  delete shellWindow.__OMNIGENT_BASE_PATH__;
});

describe("HistoryNavigationButtons", () => {
  it("renders nothing in a browser tab, which has its own Back/Forward", () => {
    installNavigation(["/c/first", "/c/second"]);
    renderButtons();
    expect(screen.queryByRole("button", { name: "Back" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Forward" })).toBeNull();
  });

  it("renders nothing in a shell whose WebView lacks the Navigation API", () => {
    (window as ShellWindow).omnigentNative = phoneShell("ios");
    renderButtons();
    expect(screen.queryByTestId("history-navigation")).toBeNull();
  });

  it.each(["ios", "android"] as const)(
    "renders nothing in an older %s shell whose server switcher floats over the header",
    (kind) => {
      (window as ShellWindow).omnigentNative = { kind };
      installNavigation(["/c/first", "/c/second"]);
      renderButtons();
      expect(screen.queryByTestId("history-navigation")).toBeNull();
    },
  );

  it.each([
    ["Electron", { omnigentDesktop: { kind: "electron" } }],
    ["iOS", { omnigentNative: phoneShell("ios") }],
    ["Android", { omnigentNative: phoneShell("android") }],
  ])("shows both buttons in the %s shell", (_shell, bridge) => {
    Object.assign(window, bridge);
    installNavigation(["/c/first", "/c/second"]);
    renderButtons();
    expect(screen.getByRole("button", { name: "Back" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Forward" })).toBeDisabled();
  });

  it("follows history changes and steps through the router", () => {
    (window as ShellWindow).omnigentNative = phoneShell("android");
    const navigation = installNavigation(["/c/first", "/c/second"]);
    renderButtons(["/c/first", "/c/second"]);

    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    expect(screen.getByRole("status", { name: "Current path" })).toHaveTextContent("/c/first");

    act(() => navigation.moveTo(0));
    expect(screen.getByRole("button", { name: "Back" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Forward" })).toBeEnabled();

    fireEvent.click(screen.getByRole("button", { name: "Forward" }));
    expect(screen.getByRole("status", { name: "Current path" })).toHaveTextContent("/c/second");
  });

  it.each([
    ["embedded under a host basename", "/omnigent", () => basenamedRouting("/omnigent")],
    [
      "served under a base path",
      "/proxy/6767",
      () => {
        (window as ShellWindow).__OMNIGENT_BASE_PATH__ = "/proxy/6767";
        return undefined;
      },
    ],
  ])("stops at a same-origin page outside the app when %s", (_mount, root, setUpMount) => {
    (window as ShellWindow).omnigentDesktop = { kind: "electron" };
    // The desktop shell redirects the workspace root into the app.
    const navigation = installNavigation(["/", `${root}/c/first`, `${root}-other/c/first`], 1);
    renderButtons(["/c/first"], setUpMount());

    expect(screen.getByRole("button", { name: "Back" })).toBeDisabled();
    // A sibling path that only shares the mount's prefix is outside the app too.
    expect(screen.getByRole("button", { name: "Forward" })).toBeDisabled();

    navigation.paths[2] = `${root}/c/second`;
    act(() => navigation.moveTo(1));
    expect(screen.getByRole("button", { name: "Forward" })).toBeEnabled();
  });
});
