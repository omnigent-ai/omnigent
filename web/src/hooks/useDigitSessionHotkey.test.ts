// Cmd/Ctrl+Alt+digit jumps to the Nth visible session. Requires Cmd/Ctrl AND
// Alt (no Shift); matches on KeyboardEvent.code so macOS Option-glyphs don't
// break it; fires inside text fields; out-of-range / already-active are no-ops.

import { renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useDigitSessionHotkey } from "./useDigitSessionHotkey";

const navigate = vi.fn();
vi.mock("@/lib/routing", () => ({
  useNavigate: () => navigate,
}));

function press(
  code: string,
  mods: Partial<Pick<KeyboardEvent, "metaKey" | "ctrlKey" | "altKey" | "shiftKey" | "key">> = {
    metaKey: true,
    altKey: true,
  },
  target: HTMLElement = document.body,
): KeyboardEvent {
  const e = new KeyboardEvent("keydown", {
    code,
    key: "",
    bubbles: true,
    cancelable: true,
    ...mods,
  });
  target.dispatchEvent(e);
  return e;
}

beforeEach(() => {
  navigate.mockClear();
  document.body.innerHTML = "";
});
afterEach(() => {
  document.body.innerHTML = "";
});

describe("useDigitSessionHotkey", () => {
  const ids = ["a", "b", "c"];

  it("Cmd+Alt+1 opens the first session", () => {
    renderHook(() => useDigitSessionHotkey(ids, undefined));
    press("Digit1");
    expect(navigate).toHaveBeenCalledWith("/c/a");
  });

  it("Cmd+Alt+3 opens the third session", () => {
    renderHook(() => useDigitSessionHotkey(ids, undefined));
    press("Digit3");
    expect(navigate).toHaveBeenCalledWith("/c/c");
  });

  it("Ctrl+Alt+1 also works (Windows/Linux)", () => {
    renderHook(() => useDigitSessionHotkey(ids, undefined));
    press("Digit1", { ctrlKey: true, altKey: true });
    expect(navigate).toHaveBeenCalledWith("/c/a");
  });

  it("matches on .code even when Option rewrites .key (macOS)", () => {
    renderHook(() => useDigitSessionHotkey(ids, undefined));
    press("Digit2", { metaKey: true, altKey: true, key: "™" }); // Option+2 glyph
    expect(navigate).toHaveBeenCalledWith("/c/b");
  });

  it("ignores plain Cmd+digit (browser owns it — that's why we need Alt)", () => {
    renderHook(() => useDigitSessionHotkey(ids, undefined));
    const e = press("Digit1", { metaKey: true });
    expect(navigate).not.toHaveBeenCalled();
    expect(e.defaultPrevented).toBe(false);
  });

  it("ignores Alt+digit with no Cmd/Ctrl", () => {
    renderHook(() => useDigitSessionHotkey(ids, undefined));
    press("Digit1", { altKey: true });
    expect(navigate).not.toHaveBeenCalled();
  });

  it("ignores Shift", () => {
    renderHook(() => useDigitSessionHotkey(ids, undefined));
    press("Digit1", { metaKey: true, altKey: true, shiftKey: true });
    expect(navigate).not.toHaveBeenCalled();
  });

  it("fires while a text field is focused", () => {
    renderHook(() => useDigitSessionHotkey(ids, undefined));
    const ta = document.createElement("textarea");
    document.body.appendChild(ta);
    press("Digit2", { metaKey: true, altKey: true }, ta);
    expect(navigate).toHaveBeenCalledWith("/c/b");
  });

  it("out-of-range index is a no-op and leaves the event alone", () => {
    renderHook(() => useDigitSessionHotkey(ids, undefined));
    const e = press("Digit5");
    expect(navigate).not.toHaveBeenCalled();
    expect(e.defaultPrevented).toBe(false);
  });

  it("does not navigate to the already-active session but still prevents default", () => {
    renderHook(() => useDigitSessionHotkey(ids, "a"));
    const e = press("Digit1");
    expect(navigate).not.toHaveBeenCalled();
    expect(e.defaultPrevented).toBe(true);
  });

  it("does nothing when the list is empty", () => {
    renderHook(() => useDigitSessionHotkey([], undefined));
    press("Digit1");
    expect(navigate).not.toHaveBeenCalled();
  });
});
