// Cmd/Ctrl+digit jumps to the Nth pinned session: 1–9 → indices 0–8, 0 → 10th.
// Requires Cmd/Ctrl, no Alt/Shift; fires inside text fields; out-of-range and
// already-active are no-ops; only out-of-range leaves the native event alone.

import { renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PINNED_HOTKEY_DIGITS, usePinnedSessionHotkeys } from "./usePinnedSessionHotkeys";

const navigate = vi.fn();
vi.mock("@/lib/routing", () => ({
  useNavigate: () => navigate,
}));

// The shortcut is desktop-only (Cmd+digit collides with browser tab-switching),
// so the hook is gated on the Electron shell. Default the mock to "native" and
// flip it per-test for the browser case.
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
): KeyboardEvent {
  const e = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true, ...mods });
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

  it("is inert in a plain browser (not the Electron shell)", () => {
    isNativeShell.mockReturnValue(false);
    renderHook(() => usePinnedSessionHotkeys(ids, undefined));
    const e = press("1");
    expect(navigate).not.toHaveBeenCalled();
    // Leave the browser's own Cmd+1 tab-switch alone.
    expect(e.defaultPrevented).toBe(false);
  });
});
