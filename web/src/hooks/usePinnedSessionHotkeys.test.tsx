// Numeric pinned-session jump to the Nth pinned session: 1–9 → indices 0–8,
// 0 → 10th. Platform-aware chord — plain Cmd/Ctrl+digit in the Electron shell,
// Cmd/Ctrl+Alt+digit in the browser (matched on e.code there, since Alt rewrites
// e.key). Fires inside text fields; out-of-range and already-active are no-ops.

import { renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PINNED_HOTKEY_DIGITS, usePinnedSessionHotkeys } from "./usePinnedSessionHotkeys";

const navigate = vi.fn();
vi.mock("@/lib/routing", () => ({
  useNavigate: () => navigate,
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

beforeEach(() => {
  navigate.mockClear();
  isNativeShell.mockReturnValue(true);
  document.body.innerHTML = "";
});
afterEach(() => {
  document.body.innerHTML = "";
});

describe("usePinnedSessionHotkeys", () => {
  const ids = ["a", "b", "c"];

  it("exposes ten digits mapping 1–9 then 0", () => {
    expect(PINNED_HOTKEY_DIGITS).toEqual(["1", "2", "3", "4", "5", "6", "7", "8", "9", "0"]);
  });

  it("Cmd+1 opens the first pinned session", () => {
    renderHook(() => usePinnedSessionHotkeys(ids, undefined));
    press("1");
    expect(navigate).toHaveBeenCalledWith("/c/a");
  });

  it("Cmd+3 opens the third pinned session", () => {
    renderHook(() => usePinnedSessionHotkeys(ids, undefined));
    press("3");
    expect(navigate).toHaveBeenCalledWith("/c/c");
  });

  it("Cmd+0 opens the tenth pinned session", () => {
    const ten = ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j"];
    renderHook(() => usePinnedSessionHotkeys(ten, undefined));
    press("0");
    expect(navigate).toHaveBeenCalledWith("/c/j");
  });

  it("Cmd+9 opens the ninth pinned session", () => {
    const ten = ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j"];
    renderHook(() => usePinnedSessionHotkeys(ten, undefined));
    press("9");
    expect(navigate).toHaveBeenCalledWith("/c/i");
  });

  it("Ctrl+1 also works (Windows/Linux)", () => {
    renderHook(() => usePinnedSessionHotkeys(ids, undefined));
    press("1", { ctrlKey: true });
    expect(navigate).toHaveBeenCalledWith("/c/a");
  });

  it("ignores a bare digit with no Cmd/Ctrl", () => {
    renderHook(() => usePinnedSessionHotkeys(ids, undefined));
    press("1", {});
    expect(navigate).not.toHaveBeenCalled();
  });

  it("ignores Alt+digit (reserved for message navigation discipline)", () => {
    renderHook(() => usePinnedSessionHotkeys(ids, undefined));
    press("1", { metaKey: true, altKey: true });
    expect(navigate).not.toHaveBeenCalled();
  });

  it("ignores Shift+digit", () => {
    renderHook(() => usePinnedSessionHotkeys(ids, undefined));
    press("1", { metaKey: true, shiftKey: true });
    expect(navigate).not.toHaveBeenCalled();
  });

  it("fires while a text field is focused", () => {
    renderHook(() => usePinnedSessionHotkeys(ids, undefined));
    const ta = document.createElement("textarea");
    document.body.appendChild(ta);
    press("2", { metaKey: true }, ta);
    expect(navigate).toHaveBeenCalledWith("/c/b");
  });

  it("does nothing when no pinned session exists at that index", () => {
    renderHook(() => usePinnedSessionHotkeys(ids, undefined));
    const e = press("5"); // only 3 pinned
    expect(navigate).not.toHaveBeenCalled();
    expect(e.defaultPrevented).toBe(false); // leaves the native event alone
  });

  it("does not navigate when the digit points at the already-active session", () => {
    renderHook(() => usePinnedSessionHotkeys(ids, "a"));
    const e = press("1");
    expect(navigate).not.toHaveBeenCalled();
    expect(e.defaultPrevented).toBe(true); // but still suppresses native tab-switch
  });

  it("prevents the browser's native tab-switch when it navigates", () => {
    renderHook(() => usePinnedSessionHotkeys(ids, undefined));
    const e = press("1");
    expect(e.defaultPrevented).toBe(true);
  });

  it("only maps the first ten: an 11th pinned session has no shortcut", () => {
    const eleven = ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k"];
    renderHook(() => usePinnedSessionHotkeys(eleven, undefined));
    // No digit maps to index 10, so "k" is unreachable; 0 still lands on the 10th.
    press("0");
    expect(navigate).toHaveBeenCalledWith("/c/j");
  });

  it("does nothing when the list is empty", () => {
    renderHook(() => usePinnedSessionHotkeys([], undefined));
    press("1");
    expect(navigate).not.toHaveBeenCalled();
  });

  it("browser: leaves plain Cmd+digit to the native tab-switch", () => {
    isNativeShell.mockReturnValue(false);
    renderHook(() => usePinnedSessionHotkeys(ids, undefined));
    const e = press("1", { metaKey: true }, document.body, "Digit1");
    expect(navigate).not.toHaveBeenCalled();
    // Plain Cmd+1 is the browser's own tab-switch — left alone.
    expect(e.defaultPrevented).toBe(false);
  });

  it("browser: Cmd+Alt+Digit1 opens the first pinned session", () => {
    isNativeShell.mockReturnValue(false);
    renderHook(() => usePinnedSessionHotkeys(ids, undefined));
    // Alt rewrites e.key on macOS (⌥1 → "¡"), so the hook matches e.code.
    const e = press("¡", { metaKey: true, altKey: true }, document.body, "Digit1");
    expect(navigate).toHaveBeenCalledWith("/c/a");
    expect(e.defaultPrevented).toBe(true);
  });

  it("browser: Ctrl+Alt+Digit3 opens the third (Windows/Linux)", () => {
    isNativeShell.mockReturnValue(false);
    renderHook(() => usePinnedSessionHotkeys(ids, undefined));
    press("3", { ctrlKey: true, altKey: true }, document.body, "Digit3");
    expect(navigate).toHaveBeenCalledWith("/c/c");
  });

  it("browser: ignores AltGr chords (AltGr+digit composes characters on intl layouts)", () => {
    // AltGr reports as Ctrl+Alt on Windows/Linux — typing AltGr+2 must
    // compose the character (event untouched), not navigate to a session.
    isNativeShell.mockReturnValue(false);
    renderHook(() => usePinnedSessionHotkeys(ids, undefined));
    const altGraph = vi
      .spyOn(KeyboardEvent.prototype, "getModifierState")
      .mockImplementation((keyArg) => keyArg === "AltGraph");
    const e = press("\u00b2", { ctrlKey: true, altKey: true }, document.body, "Digit2");
    expect(navigate).not.toHaveBeenCalled();
    expect(e.defaultPrevented).toBe(false);
    altGraph.mockRestore();
  });

  it("browser: Cmd+Alt+Digit0 opens the tenth pinned session", () => {
    isNativeShell.mockReturnValue(false);
    const ten = ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j"];
    renderHook(() => usePinnedSessionHotkeys(ten, undefined));
    press("º", { metaKey: true, altKey: true }, document.body, "Digit0");
    expect(navigate).toHaveBeenCalledWith("/c/j");
  });
});
