import { cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { isNewBrowserHotkey, useNewBrowserHotkey } from "./useNewBrowserHotkey";

afterEach(cleanup);

function event(init: KeyboardEventInit): KeyboardEvent {
  return new KeyboardEvent("keydown", { bubbles: true, cancelable: true, ...init });
}

function press(init: KeyboardEventInit, target: HTMLElement = document.body): KeyboardEvent {
  const keyboardEvent = event(init);
  target.dispatchEvent(keyboardEvent);
  return keyboardEvent;
}

describe("isNewBrowserHotkey", () => {
  it("uses Cmd on macOS and Ctrl on other platforms", () => {
    expect(isNewBrowserHotkey(event({ code: "KeyB", metaKey: true, altKey: true }), true)).toBe(
      true,
    );
    expect(isNewBrowserHotkey(event({ code: "KeyB", ctrlKey: true, altKey: true }), true)).toBe(
      false,
    );
    expect(isNewBrowserHotkey(event({ code: "KeyB", ctrlKey: true, altKey: true }), false)).toBe(
      true,
    );
  });

  it("requires Alt and rejects Shift, AltGraph, and unrelated keys", () => {
    expect(isNewBrowserHotkey(event({ code: "KeyB", metaKey: true }), true)).toBe(false);
    expect(
      isNewBrowserHotkey(
        event({ code: "KeyB", metaKey: true, altKey: true, shiftKey: true }),
        true,
      ),
    ).toBe(false);
    const altGraph = event({ code: "KeyB", ctrlKey: true, altKey: true });
    vi.spyOn(altGraph, "getModifierState").mockReturnValue(true);
    expect(isNewBrowserHotkey(altGraph, false)).toBe(false);
    expect(isNewBrowserHotkey(event({ code: "KeyN", metaKey: true, altKey: true }), true)).toBe(
      false,
    );
  });
});

describe("useNewBrowserHotkey", () => {
  it("opens a browser tab and claims Ctrl+Alt+B", () => {
    const onOpen = vi.fn();
    renderHook(() => useNewBrowserHotkey(onOpen, true, false));

    const keyboardEvent = press({ code: "KeyB", ctrlKey: true, altKey: true });

    expect(onOpen).toHaveBeenCalledOnce();
    expect(keyboardEvent.defaultPrevented).toBe(true);
  });

  it("ignores repeat and leaves editor-owned keystrokes alone", () => {
    const onOpen = vi.fn();
    renderHook(() => useNewBrowserHotkey(onOpen, true, false));

    press({ code: "KeyB", ctrlKey: true, altKey: true, repeat: true });
    for (const className of ["monaco-editor", "xterm"]) {
      const editor = document.createElement("div");
      editor.className = className;
      editor.tabIndex = 0;
      document.body.appendChild(editor);
      editor.focus();
      const editorEvent = press({ code: "KeyB", ctrlKey: true, altKey: true }, editor);
      expect(editorEvent.defaultPrevented).toBe(false);
      editor.remove();
    }

    expect(onOpen).not.toHaveBeenCalled();
  });

  it("does nothing when disabled", () => {
    const onOpen = vi.fn();
    renderHook(() => useNewBrowserHotkey(onOpen, false, false));

    const keyboardEvent = press({ code: "KeyB", ctrlKey: true, altKey: true });

    expect(onOpen).not.toHaveBeenCalled();
    expect(keyboardEvent.defaultPrevented).toBe(false);
  });
});
