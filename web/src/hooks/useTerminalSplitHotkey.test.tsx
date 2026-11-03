import { renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, onTestFinished, vi } from "vitest";

import type { TerminalSplitAxis } from "@/lib/terminalSplitLayout";
import { useTerminalSplitHotkey } from "./useTerminalSplitHotkey";

const SPLIT_RIGHT = { code: "Backslash", ctrlKey: true, altKey: true, shiftKey: true };
const SPLIT_DOWN = { code: "Minus", ctrlKey: true, altKey: true };

function press(init: KeyboardEventInit): KeyboardEvent {
  const event = new KeyboardEvent("keydown", { bubbles: true, cancelable: true, ...init });
  document.body.dispatchEvent(event);
  return event;
}

function setup(axis: TerminalSplitAxis, isMac = false) {
  const onSplit = vi.fn();
  const result = renderHook(() => useTerminalSplitHotkey(axis, onSplit, isMac));
  return { onSplit, ...result };
}

afterEach(() => vi.restoreAllMocks());

describe("useTerminalSplitHotkey", () => {
  it.each([
    ["columns", SPLIT_RIGHT, SPLIT_DOWN],
    ["rows", SPLIT_DOWN, SPLIT_RIGHT],
  ] as const)("binds %s to its own chord only", (axis, own, other) => {
    const { onSplit } = setup(axis);
    press(other);
    expect(onSplit).not.toHaveBeenCalled();
    press(own);
    expect(onSplit).toHaveBeenCalledOnce();
  });

  it("uses Cmd instead of Ctrl on macOS", () => {
    const { onSplit } = setup("rows", true);
    press(SPLIT_DOWN);
    press({ code: "Minus", metaKey: true, altKey: true });
    expect(onSplit).toHaveBeenCalledOnce();
  });

  it("ignores view-toggle, incomplete, repeated, in-dialog, and AltGraph chords", () => {
    const right = setup("columns");
    const down = setup("rows");
    // Without Shift this is the Chat/Terminal toggle.
    press({ code: "Backslash", ctrlKey: true, altKey: true });
    press({ ...SPLIT_DOWN, shiftKey: true });
    press({ ...SPLIT_RIGHT, altKey: false });
    press({ ...SPLIT_DOWN, ctrlKey: false });
    // A held chord is still claimed, so it never reaches a focused terminal.
    expect(press({ ...SPLIT_RIGHT, repeat: true }).defaultPrevented).toBe(true);
    const dialog = document.body.appendChild(document.createElement("div"));
    dialog.setAttribute("role", "dialog");
    onTestFinished(() => dialog.remove());
    dialog.appendChild(document.createElement("button")).focus();
    expect(press(SPLIT_RIGHT).defaultPrevented).toBe(false);
    dialog.remove();
    vi.spyOn(KeyboardEvent.prototype, "getModifierState").mockImplementation(
      (key) => key === "AltGraph",
    );
    press(SPLIT_RIGHT);
    press(SPLIT_DOWN);
    expect(right.onSplit).not.toHaveBeenCalled();
    expect(down.onSplit).not.toHaveBeenCalled();
  });

  it("claims the chord before a focused terminal can consume it", () => {
    setup("columns");
    const terminal = document.createElement("textarea");
    document.body.append(terminal);
    const terminalHandler = vi.fn();
    terminal.addEventListener("keydown", terminalHandler);
    const event = new KeyboardEvent("keydown", { bubbles: true, cancelable: true, ...SPLIT_RIGHT });
    terminal.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(true);
    expect(terminalHandler).not.toHaveBeenCalled();
    terminal.remove();
  });

  it("unbinds on unmount", () => {
    const { onSplit, unmount } = setup("columns");
    unmount();
    press(SPLIT_RIGHT);
    expect(onSplit).not.toHaveBeenCalled();
  });
});
