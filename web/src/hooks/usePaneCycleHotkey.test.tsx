import { renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, onTestFinished, vi } from "vitest";

import { usePaneCycleHotkey } from "./usePaneCycleHotkey";

const PANES = ["a", "b", "c"];

function press(
  init: KeyboardEventInit = { ctrlKey: true, altKey: true, shiftKey: true },
  code = "BracketRight",
): KeyboardEvent {
  const event = new KeyboardEvent("keydown", { code, bubbles: true, cancelable: true, ...init });
  document.body.dispatchEvent(event);
  return event;
}

function setup(focusedKey = "a", enabled = true, isMac = false) {
  const onFocusPane = vi.fn();
  const result = renderHook(() =>
    usePaneCycleHotkey({ enabled, paneKeys: PANES, focusedKey, onFocusPane }, isMac),
  );
  return { onFocusPane, ...result };
}

afterEach(() => vi.restoreAllMocks());

describe("usePaneCycleHotkey", () => {
  it.each([
    ["a", "BracketRight", "b"],
    ["c", "BracketRight", "a"],
    ["a", "BracketLeft", "c"],
    ["b", "BracketLeft", "a"],
  ])("from %s, Ctrl+Alt+Shift+%s focuses %s", (focusedKey, code, expected) => {
    const { onFocusPane } = setup(focusedKey);
    press({ ctrlKey: true, altKey: true, shiftKey: true }, code);
    expect(onFocusPane).toHaveBeenCalledExactlyOnceWith(expected);
  });

  it("uses Cmd instead of Ctrl on macOS", () => {
    const { onFocusPane } = setup("a", true, true);
    press({ ctrlKey: true, altKey: true, shiftKey: true });
    press({ metaKey: true, altKey: true, shiftKey: true });
    expect(onFocusPane).toHaveBeenCalledExactlyOnceWith("b");
  });

  it("ignores disabled, unshifted, incomplete, repeated, in-dialog, and AltGraph chords", () => {
    const disabled = setup("a", false);
    press();
    expect(disabled.onFocusPane).not.toHaveBeenCalled();

    const active = setup();
    // Without Shift this is the workspace-sidebar toggle.
    press({ ctrlKey: true, altKey: true });
    press({ ctrlKey: true, shiftKey: true });
    // A held chord is still claimed, so it never reaches a focused terminal.
    expect(
      press({ ctrlKey: true, altKey: true, shiftKey: true, repeat: true }).defaultPrevented,
    ).toBe(true);
    press({ ctrlKey: true, altKey: true, shiftKey: true }, "KeyP");
    const dialog = document.body.appendChild(document.createElement("div"));
    dialog.setAttribute("role", "dialog");
    onTestFinished(() => dialog.remove());
    dialog.appendChild(document.createElement("button")).focus();
    expect(press().defaultPrevented).toBe(false);
    dialog.remove();
    vi.spyOn(KeyboardEvent.prototype, "getModifierState").mockImplementation(
      (key) => key === "AltGraph",
    );
    press();
    expect(active.onFocusPane).not.toHaveBeenCalled();
  });

  it("claims the chord before a focused terminal can consume it", () => {
    setup();
    const terminal = document.createElement("textarea");
    document.body.append(terminal);
    const terminalHandler = vi.fn();
    terminal.addEventListener("keydown", terminalHandler);
    const event = new KeyboardEvent("keydown", {
      code: "BracketRight",
      ctrlKey: true,
      altKey: true,
      shiftKey: true,
      bubbles: true,
      cancelable: true,
    });
    terminal.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(true);
    expect(terminalHandler).not.toHaveBeenCalled();
    terminal.remove();
  });

  it("unbinds on unmount", () => {
    const { onFocusPane, unmount } = setup();
    unmount();
    press();
    expect(onFocusPane).not.toHaveBeenCalled();
  });
});
