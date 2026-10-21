import { cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { isFocusComposerHotkey, useFocusComposerHotkey } from "./useFocusComposerHotkey";

afterEach(() => {
  cleanup();
  document.body.innerHTML = "";
});

function press(init: KeyboardEventInit): KeyboardEvent {
  const e = new KeyboardEvent("keydown", { bubbles: true, cancelable: true, ...init });
  window.dispatchEvent(e);
  return e;
}

describe("isFocusComposerHotkey", () => {
  it("matches Ctrl+Shift+L on every platform, by physical code", () => {
    expect(
      isFocusComposerHotkey(
        new KeyboardEvent("keydown", { code: "KeyL", ctrlKey: true, shiftKey: true }),
      ),
    ).toBe(true);
  });

  it("rejects the Cmd chord", () => {
    expect(
      isFocusComposerHotkey(
        new KeyboardEvent("keydown", { code: "KeyL", metaKey: true, shiftKey: true }),
      ),
    ).toBe(false);
    // Cmd held alongside Ctrl must not match either.
    expect(
      isFocusComposerHotkey(
        new KeyboardEvent("keydown", {
          code: "KeyL",
          ctrlKey: true,
          metaKey: true,
          shiftKey: true,
        }),
      ),
    ).toBe(false);
  });

  it("requires Shift and rejects Alt", () => {
    // Bare Ctrl+L is the browser's address-bar focus — not our shortcut.
    expect(
      isFocusComposerHotkey(new KeyboardEvent("keydown", { code: "KeyL", ctrlKey: true })),
    ).toBe(false);
    expect(
      isFocusComposerHotkey(
        new KeyboardEvent("keydown", { code: "KeyL", ctrlKey: true, shiftKey: true, altKey: true }),
      ),
    ).toBe(false);
  });

  it("ignores AltGraph (intl layouts reporting Ctrl+Alt)", () => {
    const e = new KeyboardEvent("keydown", { code: "KeyL", ctrlKey: true, shiftKey: true });
    e.getModifierState = () => true; // AltGraph active
    expect(isFocusComposerHotkey(e)).toBe(false);
  });

  it("rejects other keys with the chord", () => {
    expect(
      isFocusComposerHotkey(
        new KeyboardEvent("keydown", { code: "KeyM", ctrlKey: true, shiftKey: true }),
      ),
    ).toBe(false);
  });
});

describe("useFocusComposerHotkey", () => {
  it("focuses on Ctrl+Shift+L and prevents the browser default", () => {
    const onFocus = vi.fn();
    renderHook(() => useFocusComposerHotkey(onFocus));

    const e = press({ code: "KeyL", ctrlKey: true, shiftKey: true });

    expect(onFocus).toHaveBeenCalledTimes(1);
    expect(e.defaultPrevented).toBe(true);
  });

  it("ignores auto-repeat", () => {
    const onFocus = vi.fn();
    renderHook(() => useFocusComposerHotkey(onFocus));

    press({ code: "KeyL", ctrlKey: true, shiftKey: true, repeat: true });

    expect(onFocus).not.toHaveBeenCalled();
  });

  it("does nothing when disabled", () => {
    const onFocus = vi.fn();
    renderHook(() => useFocusComposerHotkey(onFocus, false));

    const e = press({ code: "KeyL", ctrlKey: true, shiftKey: true });

    expect(onFocus).not.toHaveBeenCalled();
    expect(e.defaultPrevented).toBe(false);
  });

  it.each([
    ["a terminal", "xterm"],
    ["the Monaco editor", "monaco-editor"],
  ])("bails when focus sits inside %s", (_label, className) => {
    const onFocus = vi.fn();
    renderHook(() => useFocusComposerHotkey(onFocus));

    const surface = document.createElement("div");
    surface.className = className;
    const input = document.createElement("input");
    surface.appendChild(input);
    document.body.appendChild(surface);
    input.focus();
    expect(document.activeElement).toBe(input);

    press({ code: "KeyL", ctrlKey: true, shiftKey: true });

    expect(onFocus).not.toHaveBeenCalled();
  });

  it("unbinds on unmount", () => {
    const onFocus = vi.fn();
    const { unmount } = renderHook(() => useFocusComposerHotkey(onFocus));
    unmount();

    press({ code: "KeyL", ctrlKey: true, shiftKey: true });

    expect(onFocus).not.toHaveBeenCalled();
  });
});
