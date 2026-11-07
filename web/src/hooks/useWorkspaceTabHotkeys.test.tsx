// ⌃digit (Alt+digit off macOS) picks the Nth fixed workspace tab; other
// modifiers, AltGraph and a terminal's Alt+digit pass through.

import { renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useWorkspaceTabHotkeys, workspaceTabIndex } from "./useWorkspaceTabHotkeys";

function keydown(init: KeyboardEventInit): KeyboardEvent {
  return new KeyboardEvent("keydown", { bubbles: true, cancelable: true, ...init });
}

afterEach(() => document.body.replaceChildren());

describe("workspaceTabIndex", () => {
  it("reads Control+digit on macOS and Alt+digit elsewhere", () => {
    expect(workspaceTabIndex(keydown({ code: "Digit1", ctrlKey: true }), true)).toBe(0);
    expect(workspaceTabIndex(keydown({ code: "Digit9", altKey: true }), false)).toBe(8);
    expect(workspaceTabIndex(keydown({ code: "Digit1", altKey: true }), true)).toBe(-1);
    // Ctrl+digit opens sidebar sessions off macOS.
    expect(workspaceTabIndex(keydown({ code: "Digit1", ctrlKey: true }), false)).toBe(-1);
  });

  it("rejects Cmd, Shift, AltGraph, 0 and other keys", () => {
    expect(workspaceTabIndex(keydown({ code: "Digit1", ctrlKey: true, metaKey: true }), true)).toBe(
      -1,
    );
    expect(
      workspaceTabIndex(keydown({ code: "Digit1", altKey: true, shiftKey: true }), false),
    ).toBe(-1);
    const altGraph = keydown({ code: "Digit1", altKey: true });
    vi.spyOn(altGraph, "getModifierState").mockReturnValue(true);
    expect(workspaceTabIndex(altGraph, false)).toBe(-1);
    expect(workspaceTabIndex(keydown({ code: "Digit0", altKey: true }), false)).toBe(-1);
    expect(workspaceTabIndex(keydown({ code: "KeyA", altKey: true }), false)).toBe(-1);
  });
});

describe("useWorkspaceTabHotkeys", () => {
  it("claims the key only when a tab was shown", () => {
    const select = { current: vi.fn((index: number) => index < 2) };
    renderHook(() => useWorkspaceTabHotkeys(select, false));
    const shown = keydown({ code: "Digit2", altKey: true });
    document.body.dispatchEvent(shown);
    expect(select.current).toHaveBeenLastCalledWith(1);
    expect(shown.defaultPrevented).toBe(true);

    const missing = keydown({ code: "Digit5", altKey: true });
    document.body.dispatchEvent(missing);
    expect(missing.defaultPrevented).toBe(false);
  });

  it.each([
    { platform: "macOS", isMac: true, chord: { ctrlKey: true } },
    { platform: "other platforms", isMac: false, chord: { altKey: true } },
  ])("picks a tab from a field that stops its keys on $platform", ({ isMac, chord }) => {
    const select = { current: vi.fn(() => true) };
    renderHook(() => useWorkspaceTabHotkeys(select, isMac));
    const input = document.createElement("input");
    input.addEventListener("keydown", (event) => event.stopPropagation());
    document.body.append(input);
    input.focus();
    const event = keydown({ code: "Digit1", ...chord });
    input.dispatchEvent(event);
    expect(select.current).toHaveBeenCalledWith(0);
    expect(event.defaultPrevented).toBe(true);
    input.remove();
  });

  it("picks a tab with Control+digit from a focused terminal on macOS", () => {
    const select = { current: vi.fn(() => true) };
    renderHook(() => useWorkspaceTabHotkeys(select, true));
    const terminal = document.createElement("div");
    terminal.className = "xterm";
    const input = document.createElement("textarea");
    // The terminal sends ⌃3 to the shell as Escape and stops the event.
    const toTerminal = vi.fn((event: KeyboardEvent) => event.stopPropagation());
    input.addEventListener("keydown", toTerminal);
    terminal.append(input);
    document.body.append(terminal);
    input.focus();
    const first = keydown({ code: "Digit3", ctrlKey: true });
    input.dispatchEvent(first);
    // Holding the chord repeats it; the repeats mustn't reach the terminal either.
    const repeat = new KeyboardEvent("keydown", {
      code: "Digit3",
      ctrlKey: true,
      repeat: true,
      bubbles: true,
      cancelable: true,
    });
    input.dispatchEvent(repeat);
    expect(select.current).toHaveBeenCalledExactlyOnceWith(2);
    expect(first.defaultPrevented).toBe(true);
    expect(repeat.defaultPrevented).toBe(true);
    expect(toTerminal).not.toHaveBeenCalled();
    terminal.remove();
  });

  it("leaves Alt+digit to a focused terminal off macOS", () => {
    const select = { current: vi.fn(() => true) };
    renderHook(() => useWorkspaceTabHotkeys(select, false));
    const terminal = document.createElement("div");
    terminal.className = "xterm";
    const input = document.createElement("textarea");
    terminal.append(input);
    document.body.append(terminal);
    input.focus();
    input.dispatchEvent(keydown({ code: "Digit1", altKey: true }));
    expect(select.current).not.toHaveBeenCalled();
  });
});
