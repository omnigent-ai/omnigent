import { cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { isModelPickerHotkey, useModelPickerHotkey } from "./useModelPickerHotkey";

afterEach(() => {
  cleanup();
  document.body.innerHTML = "";
});

function press(init: KeyboardEventInit): KeyboardEvent {
  const e = new KeyboardEvent("keydown", { bubbles: true, cancelable: true, ...init });
  window.dispatchEvent(e);
  return e;
}

describe("isModelPickerHotkey", () => {
  it("matches Ctrl+Shift+M on every platform, by physical code", () => {
    expect(
      isModelPickerHotkey(
        new KeyboardEvent("keydown", { code: "KeyM", ctrlKey: true, shiftKey: true }),
      ),
    ).toBe(true);
  });

  it("rejects the Cmd chord (⌘⇧M is Chrome's profile switcher)", () => {
    expect(
      isModelPickerHotkey(
        new KeyboardEvent("keydown", { code: "KeyM", metaKey: true, shiftKey: true }),
      ),
    ).toBe(false);
    // Cmd held alongside Ctrl must not match either.
    expect(
      isModelPickerHotkey(
        new KeyboardEvent("keydown", {
          code: "KeyM",
          ctrlKey: true,
          metaKey: true,
          shiftKey: true,
        }),
      ),
    ).toBe(false);
  });

  it("requires Shift and rejects Alt", () => {
    // Bare Ctrl+M is not the shortcut.
    expect(isModelPickerHotkey(new KeyboardEvent("keydown", { code: "KeyM", ctrlKey: true }))).toBe(
      false,
    );
    // Ctrl+Alt+Shift+M must not match (Alt is the minimize-all family / AltGr).
    expect(
      isModelPickerHotkey(
        new KeyboardEvent("keydown", { code: "KeyM", ctrlKey: true, shiftKey: true, altKey: true }),
      ),
    ).toBe(false);
  });

  it("ignores AltGraph (intl layouts reporting Ctrl+Alt)", () => {
    const e = new KeyboardEvent("keydown", { code: "KeyM", ctrlKey: true, shiftKey: true });
    e.getModifierState = () => true; // AltGraph active
    expect(isModelPickerHotkey(e)).toBe(false);
  });

  it("rejects other keys with the chord", () => {
    expect(
      isModelPickerHotkey(
        new KeyboardEvent("keydown", { code: "KeyK", ctrlKey: true, shiftKey: true }),
      ),
    ).toBe(false);
  });
});

describe("useModelPickerHotkey", () => {
  it("opens on Ctrl+Shift+M and prevents the browser default", () => {
    const onOpen = vi.fn();
    renderHook(() => useModelPickerHotkey(onOpen));

    const e = press({ code: "KeyM", ctrlKey: true, shiftKey: true });

    expect(onOpen).toHaveBeenCalledTimes(1);
    expect(e.defaultPrevented).toBe(true);
  });

  it("stops propagation so the composer's own key handler doesn't also see it", () => {
    const onOpen = vi.fn();
    renderHook(() => useModelPickerHotkey(onOpen));

    const e = new KeyboardEvent("keydown", {
      bubbles: true,
      cancelable: true,
      code: "KeyM",
      ctrlKey: true,
      shiftKey: true,
    });
    const stop = vi.spyOn(e, "stopPropagation");
    window.dispatchEvent(e);

    expect(onOpen).toHaveBeenCalledTimes(1);
    expect(stop).toHaveBeenCalled();
  });

  it("ignores auto-repeat", () => {
    const onOpen = vi.fn();
    renderHook(() => useModelPickerHotkey(onOpen));

    press({ code: "KeyM", ctrlKey: true, shiftKey: true, repeat: true });

    expect(onOpen).not.toHaveBeenCalled();
  });

  it("does nothing when disabled", () => {
    const onOpen = vi.fn();
    renderHook(() => useModelPickerHotkey(onOpen, false));

    const e = press({ code: "KeyM", ctrlKey: true, shiftKey: true });

    expect(onOpen).not.toHaveBeenCalled();
    expect(e.defaultPrevented).toBe(false);
  });

  it.each([
    ["a terminal", "xterm"],
    ["the Monaco editor", "monaco-editor"],
  ])("bails when focus sits inside %s", (_label, className) => {
    const onOpen = vi.fn();
    renderHook(() => useModelPickerHotkey(onOpen));

    const surface = document.createElement("div");
    surface.className = className;
    const input = document.createElement("input");
    surface.appendChild(input);
    document.body.appendChild(surface);
    input.focus();
    expect(document.activeElement).toBe(input);

    press({ code: "KeyM", ctrlKey: true, shiftKey: true });

    expect(onOpen).not.toHaveBeenCalled();
  });

  it("unbinds on unmount", () => {
    const onOpen = vi.fn();
    const { unmount } = renderHook(() => useModelPickerHotkey(onOpen));
    unmount();

    press({ code: "KeyM", ctrlKey: true, shiftKey: true });

    expect(onOpen).not.toHaveBeenCalled();
  });
});
