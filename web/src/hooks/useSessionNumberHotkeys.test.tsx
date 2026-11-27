// Numeric pinned-session jump to the Nth pinned session: 1–9 → indices 0–8,
// 0 → 10th. Platform-aware chord — plain Cmd/Ctrl+digit in the Electron shell,
// Cmd/Ctrl+Alt+digit in the browser (matched on e.code there, since Alt rewrites
// e.key). Only ⌘ fires on macOS, only Ctrl on Win/Linux. Fires inside text
// fields; out-of-range and the session the window's URL shows are no-ops.

import { renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SESSION_HOTKEY_DIGITS, useSessionNumberHotkeys } from "./useSessionNumberHotkeys";

const navigate = vi.fn();
// The app's mount when embedded; identity standalone.
const rebasePath = vi.fn((path: string) => path);
vi.mock("@/lib/routing", () => ({
  useNavigate: () => navigate,
  useRebasePath: () => rebasePath,
}));

// The chord is platform-aware: plain Cmd+digit in the Electron shell, but
// Cmd+Alt+digit in the browser (where plain Cmd+digit is the native tab-switch).
// Default the mock to "native" and flip it per-test for the browser case.
const isNativeShell = vi.fn(() => true);
vi.mock("@/lib/nativeBridge", () => ({
  isNativeShell: () => isNativeShell(),
}));

/** Dispatch a digit keydown bubbling to window; returns the event so callers
 *  can assert on preventDefault. */
function press(
  key: string,
  mods: Partial<Pick<KeyboardEvent, "metaKey" | "ctrlKey" | "altKey" | "shiftKey">> = {
    metaKey: true,
  },
  target: HTMLElement = document.body,
  code = "",
): KeyboardEvent {
  const e = new KeyboardEvent("keydown", { key, code, bubbles: true, cancelable: true, ...mods });
  target.dispatchEvent(e);
  return e;
}

/** Render on macOS (⌘) by default, with the window at *path*; isMac=false is Win/Linux. */
function render(ids: readonly string[], path = "/", isMac = true) {
  window.history.replaceState(null, "", path);
  return renderHook(() => useSessionNumberHotkeys(ids, isMac));
}

beforeEach(() => {
  navigate.mockClear();
  isNativeShell.mockReturnValue(true);
  document.body.innerHTML = "";
});
afterEach(() => {
  document.body.innerHTML = "";
  window.history.replaceState(null, "", "/");
  delete window.__OMNIGENT_BASE_PATH__;
  rebasePath.mockImplementation((path) => path);
});

describe("useSessionNumberHotkeys", () => {
  const ids = ["a", "b", "c"];

  it("exposes the nine digits 1–9", () => {
    expect(SESSION_HOTKEY_DIGITS).toEqual(["1", "2", "3", "4", "5", "6", "7", "8", "9"]);
  });

  it("Cmd+1 opens the first pinned session", () => {
    render(ids);
    press("1");
    expect(navigate).toHaveBeenCalledWith("/c/a");
  });

  it("Cmd+3 opens the third pinned session", () => {
    render(ids);
    press("3");
    expect(navigate).toHaveBeenCalledWith("/c/c");
  });

  it("leaves Cmd+0 to the menu's Actual Size, even with a tenth session", () => {
    const ten = ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j"];
    render(ten);
    const event = press("0");
    expect(navigate).not.toHaveBeenCalled();
    expect(event.defaultPrevented).toBe(false);
  });

  it("Cmd+9 opens the ninth pinned session", () => {
    const ten = ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j"];
    render(ten);
    press("9");
    expect(navigate).toHaveBeenCalledWith("/c/i");
  });

  it("Ctrl+1 works on Windows/Linux", () => {
    render(ids, "/", false);
    press("1", { ctrlKey: true });
    expect(navigate).toHaveBeenCalledWith("/c/a");
  });

  it("ignores Ctrl+1 on macOS (only ⌘ fires there)", () => {
    render(ids, "/", true);
    press("1", { ctrlKey: true });
    expect(navigate).not.toHaveBeenCalled();
  });

  it("ignores a bare digit with no Cmd/Ctrl", () => {
    render(ids);
    press("1", {});
    expect(navigate).not.toHaveBeenCalled();
  });

  it("ignores Alt+digit (reserved for message navigation discipline)", () => {
    render(ids);
    press("1", { metaKey: true, altKey: true });
    expect(navigate).not.toHaveBeenCalled();
  });

  it("ignores Shift+digit", () => {
    render(ids);
    press("1", { metaKey: true, shiftKey: true });
    expect(navigate).not.toHaveBeenCalled();
  });

  it("fires while a text field is focused", () => {
    render(ids);
    const ta = document.createElement("textarea");
    document.body.appendChild(ta);
    press("2", { metaKey: true }, ta);
    expect(navigate).toHaveBeenCalledWith("/c/b");
  });

  it("does nothing when no pinned session exists at that index", () => {
    render(ids);
    const e = press("5"); // only 3 pinned
    expect(navigate).not.toHaveBeenCalled();
    expect(e.defaultPrevented).toBe(false); // leaves the native event alone
  });

  it("does not navigate when the digit points at the session the window shows", () => {
    render(ids, "/c/a");
    const e = press("1");
    expect(navigate).not.toHaveBeenCalled();
    expect(e.defaultPrevented).toBe(true); // but still suppresses native tab-switch
  });

  it("goes back to the session the window just left, before the router renders the next page", () => {
    // The router still renders the session while the new page loads; the URL has moved on.
    render(ids, "/c/a");
    // No re-render: the press itself reads where the window is.
    window.history.pushState(null, "", "/");
    press("1");
    expect(navigate).toHaveBeenCalledWith("/c/a");
  });

  it("knows the session under an embedding host's mount", () => {
    rebasePath.mockImplementation((path) => `/mount${path}`);
    render(ids, "/mount/c/a");
    press("1");
    expect(navigate).not.toHaveBeenCalled();
  });

  it("prevents the browser's native tab-switch when it navigates", () => {
    render(ids);
    const e = press("1");
    expect(e.defaultPrevented).toBe(true);
  });

  it("only maps the first nine: a tenth pinned session has no shortcut", () => {
    const ten = ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j"];
    render(ten);
    for (const digit of ["0", ...SESSION_HOTKEY_DIGITS]) press(digit);
    expect(navigate).toHaveBeenCalledTimes(SESSION_HOTKEY_DIGITS.length);
    expect(navigate).not.toHaveBeenCalledWith("/c/j");
  });

  it("does nothing when the list is empty", () => {
    render([]);
    press("1");
    expect(navigate).not.toHaveBeenCalled();
  });

  it("browser: leaves plain Cmd+digit to the native tab-switch", () => {
    isNativeShell.mockReturnValue(false);
    render(ids);
    const e = press("1", { metaKey: true }, document.body, "Digit1");
    expect(navigate).not.toHaveBeenCalled();
    // Plain Cmd+1 is the browser's own tab-switch — left alone.
    expect(e.defaultPrevented).toBe(false);
  });

  it("browser: Cmd+Alt+Digit1 opens the first pinned session", () => {
    isNativeShell.mockReturnValue(false);
    render(ids);
    // Alt rewrites e.key on macOS (⌥1 → "¡"), so the hook matches e.code.
    const e = press("¡", { metaKey: true, altKey: true }, document.body, "Digit1");
    expect(navigate).toHaveBeenCalledWith("/c/a");
    expect(e.defaultPrevented).toBe(true);
  });

  it("browser: Ctrl+Alt+Digit3 opens the third (Windows/Linux)", () => {
    isNativeShell.mockReturnValue(false);
    render(ids, "/", false);
    press("3", { ctrlKey: true, altKey: true }, document.body, "Digit3");
    expect(navigate).toHaveBeenCalledWith("/c/c");
  });

  it("browser: ignores AltGr chords (AltGr+digit composes characters on intl layouts)", () => {
    // AltGr reports as Ctrl+Alt on Windows/Linux — typing AltGr+2 must
    // compose the character (event untouched), not navigate to a session.
    isNativeShell.mockReturnValue(false);
    render(ids, "/", false);
    const altGraph = vi
      .spyOn(KeyboardEvent.prototype, "getModifierState")
      .mockImplementation((keyArg) => keyArg === "AltGraph");
    const e = press("²", { ctrlKey: true, altKey: true }, document.body, "Digit2");
    expect(navigate).not.toHaveBeenCalled();
    expect(e.defaultPrevented).toBe(false);
    altGraph.mockRestore();
  });

  it("browser: leaves Cmd+Alt+Digit0 alone, even with a tenth session", () => {
    isNativeShell.mockReturnValue(false);
    const ten = ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j"];
    render(ten);
    const event = press("º", { metaKey: true, altKey: true }, document.body, "Digit0");
    expect(navigate).not.toHaveBeenCalled();
    expect(event.defaultPrevented).toBe(false);
  });
});
