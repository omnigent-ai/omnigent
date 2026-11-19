import { renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { openTerminalLink } from "@/components/blocks/TerminalSession";
import { resetDesktopCommandsForTests } from "@/lib/desktopCommands";
import {
  requestHistoryNavigation,
  resetHistoryNavigationForTests,
  useHistoryNavigation,
  useHistoryNavigationAvailability,
} from "./useHistoryNavigation";

const navigate = vi.fn();
let navigationType: "POP" | "PUSH" | "REPLACE" = "POP";
vi.mock("@/lib/routing", () => ({
  useNavigate: () => navigate,
  useLocation: () => ({ pathname: "/" }),
  useNavigationType: () => navigationType,
}));
let embedded = false;
vi.mock("@/lib/embedded", () => ({ useIsEmbedded: () => embedded }));

type CommandListener = (event: { command: string; browserViewId: string | null }) => void;
let sendCommand: CommandListener = () => {};

function installDesktop(): void {
  Object.assign(window, {
    omnigentDesktop: {
      kind: "electron",
      onDesktopCommand: (callback: CommandListener) => {
        sendCommand = callback;
        return () => {};
      },
      setDesktopCommandState: () => {},
    },
  });
}

function pressBracket(code: "BracketLeft" | "BracketRight", target: HTMLElement = document.body) {
  const event = new KeyboardEvent("keydown", {
    code,
    metaKey: true,
    bubbles: true,
    cancelable: true,
  });
  target.dispatchEvent(event);
  return event;
}

function setHistoryIndex(idx: number): void {
  window.history.replaceState({ idx }, "");
}

beforeEach(() => {
  navigate.mockClear();
  setHistoryIndex(1);
  installDesktop();
});

afterEach(() => {
  embedded = false;
  navigationType = "POP";
  resetHistoryNavigationForTests();
  resetDesktopCommandsForTests();
  Reflect.deleteProperty(window, "omnigentDesktop");
  document.body.innerHTML = "";
});

describe("useHistoryNavigation", () => {
  it("steps back and forward with Cmd+[ and Cmd+]", () => {
    renderHook(() => useHistoryNavigation(true));
    expect(pressBracket("BracketLeft").defaultPrevented).toBe(true);
    pressBracket("BracketRight");
    expect(navigate.mock.calls).toEqual([[-1], [1]]);
  });

  it("goes back after a session link followed from a terminal", () => {
    setHistoryIndex(0);
    renderHook(() => useHistoryNavigation(true));
    openTerminalLink(new MouseEvent("click"), `${window.location.origin}/c/conv_next`);
    pressBracket("BracketLeft");
    expect(navigate).toHaveBeenCalledWith(-1);
  });

  it("does not go back past the first in-app entry", () => {
    setHistoryIndex(0);
    renderHook(() => useHistoryNavigation(true));
    pressBracket("BracketLeft");
    requestHistoryNavigation(-1);
    expect(navigate).not.toHaveBeenCalled();
  });

  it("embedded, does not go back past the page the app opened on", () => {
    embedded = true;
    // The host's own pages are earlier entries in the same history.
    setHistoryIndex(4);
    renderHook(() => useHistoryNavigation(true));
    pressBracket("BracketLeft");
    expect(navigate).not.toHaveBeenCalled();

    setHistoryIndex(5);
    pressBracket("BracketLeft");
    expect(navigate).toHaveBeenCalledWith(-1);
  });

  it("takes the steps the Back and Forward buttons ask for", () => {
    renderHook(() => useHistoryNavigation(true));
    requestHistoryNavigation(-1);
    requestHistoryNavigation(1);
    expect(navigate.mock.calls).toEqual([[-1], [1]]);
  });

  it("leaves bracket chords to a focused terminal or code editor", () => {
    renderHook(() => useHistoryNavigation(true));
    document.body.innerHTML = '<div class="xterm"><textarea></textarea></div>';
    const terminal = document.querySelector("textarea")!;
    terminal.focus();
    pressBracket("BracketLeft", terminal);
    expect(navigate).not.toHaveBeenCalled();
  });

  it("maps the mouse back/forward buttons and the Go menu in the desktop app", () => {
    renderHook(() => useHistoryNavigation(true));
    window.dispatchEvent(new MouseEvent("mouseup", { button: 3 }));
    window.dispatchEvent(new MouseEvent("mouseup", { button: 4 }));
    sendCommand({ command: "go-back", browserViewId: null });
    sendCommand({ command: "go-forward", browserViewId: null });
    expect(navigate.mock.calls).toEqual([[-1], [1], [-1], [1]]);
  });

  it("leaves Cmd+[ / Cmd+] and the mouse buttons to the browser outside the desktop app", () => {
    Reflect.deleteProperty(window, "omnigentDesktop");
    renderHook(() => useHistoryNavigation(true));
    expect(pressBracket("BracketLeft").defaultPrevented).toBe(false);
    pressBracket("BracketRight");
    window.dispatchEvent(new MouseEvent("mouseup", { button: 3 }));
    expect(navigate).not.toHaveBeenCalled();
  });
});

describe("useHistoryNavigationAvailability", () => {
  function step(idx: number, type: typeof navigationType, view: { rerender: () => void }) {
    setHistoryIndex(idx);
    navigationType = type;
    view.rerender();
  }

  // The buttons render inside the app shell, which binds useHistoryNavigation.
  const renderInShell = () =>
    renderHook(() => {
      useHistoryNavigation(true);
      return useHistoryNavigationAvailability();
    });

  it("tracks which entries Back and Forward can reach", () => {
    setHistoryIndex(3);
    navigationType = "PUSH";
    const view = renderInShell();
    expect(view.result.current).toEqual({ canGoBack: true, canGoForward: false });

    step(1, "POP", view);
    expect(view.result.current).toEqual({ canGoBack: true, canGoForward: true });

    // A push from an earlier entry drops the entries after it.
    step(2, "PUSH", view);
    expect(view.result.current).toEqual({ canGoBack: true, canGoForward: false });

    step(0, "POP", view);
    expect(view.result.current).toEqual({ canGoBack: false, canGoForward: true });
  });

  it("keeps Forward accurate across navigations made while the buttons are unmounted", () => {
    setHistoryIndex(5);
    navigationType = "PUSH";
    const shell = renderHook(() => useHistoryNavigation(true));
    let buttons = renderHook(() => useHistoryNavigationAvailability());
    step(2, "POP", shell);
    buttons.rerender();
    expect(buttons.result.current.canGoForward).toBe(true);

    // Settings hides the buttons; pushing it from entry 2 drops entries 4 and 5.
    buttons.unmount();
    step(3, "PUSH", shell);
    step(2, "POP", shell);
    buttons = renderHook(() => useHistoryNavigationAvailability());
    expect(buttons.result.current).toEqual({ canGoBack: true, canGoForward: true });

    step(3, "POP", shell);
    buttons.rerender();
    expect(buttons.result.current).toEqual({ canGoBack: true, canGoForward: false });
  });

  it("ignores navigation requests that are not a single step", () => {
    renderInShell();
    for (const detail of [-3, 0, 2, "back", null]) {
      window.dispatchEvent(new CustomEvent("omnigent:history-navigation", { detail }));
    }
    expect(navigate).not.toHaveBeenCalled();
  });

  it("embedded, offers Back only after the page the app opened on", () => {
    embedded = true;
    setHistoryIndex(4);
    navigationType = "POP";
    const view = renderInShell();
    expect(view.result.current.canGoBack).toBe(false);

    step(5, "PUSH", view);
    expect(view.result.current.canGoBack).toBe(true);
  });
});
