import { renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { resetDesktopCommandsForTests } from "@/lib/desktopCommands";
import { useHistoryNavigation } from "./useHistoryNavigation";

const navigate = vi.fn();
vi.mock("@/lib/routing", () => ({ useNavigate: () => navigate }));
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
});

afterEach(() => {
  embedded = false;
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

  it("does not go back past the first in-app entry", () => {
    setHistoryIndex(0);
    renderHook(() => useHistoryNavigation(true));
    pressBracket("BracketLeft");
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

  it("leaves bracket chords to a focused terminal or code editor", () => {
    renderHook(() => useHistoryNavigation(true));
    document.body.innerHTML = '<div class="xterm"><textarea></textarea></div>';
    const terminal = document.querySelector("textarea")!;
    terminal.focus();
    pressBracket("BracketLeft", terminal);
    expect(navigate).not.toHaveBeenCalled();
  });

  it("maps the mouse back/forward buttons and the Go menu in the desktop app", () => {
    installDesktop();
    renderHook(() => useHistoryNavigation(true));
    window.dispatchEvent(new MouseEvent("mouseup", { button: 3 }));
    window.dispatchEvent(new MouseEvent("mouseup", { button: 4 }));
    sendCommand({ command: "go-back", browserViewId: null });
    sendCommand({ command: "go-forward", browserViewId: null });
    expect(navigate.mock.calls).toEqual([[-1], [1], [-1], [1]]);
  });

  it("leaves the mouse buttons to the browser outside the desktop app", () => {
    renderHook(() => useHistoryNavigation(true));
    window.dispatchEvent(new MouseEvent("mouseup", { button: 3 }));
    expect(navigate).not.toHaveBeenCalled();
  });
});
